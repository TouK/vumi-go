import json
import math

from decimal import Decimal

from twisted.python import log
from twisted.internet import defer
from twisted.internet.threads import deferToThread
from twisted.web.resource import Resource
from twisted.web.server import NOT_DONE_YET

from go.billing import settings as app_settings
from go.billing.models import MessageCost
from go.billing.utils import JSONEncoder, JSONDecoder, BillingError
from go.billing.tasks import create_low_credit_notification


def spawn_celery_task_via_thread(t, *args, **kw):
    """
    Issue a task to a Celery worker using deferToThread.

    :param Task t:
        The Celery task to issue.
    :param list args:
        Postional arguments for the Celery task.
    :param dict kw:
        Keyword arguments for the Celery task.
    """
    return deferToThread(t.delay, *args, **kw)


def pluck(data, keys):
    return (data[k] for k in keys)


class BaseResource(Resource):
    """Base class for the APIs ``Resource``s"""

    _connection_pool = None  # The txpostgres connection pool

    def __init__(self, connection_pool):
        Resource.__init__(self)
        self._connection_pool = connection_pool

    def _handle_error(self, error, request, *args, **kwargs):
        """Log the error and return an HTTP 500 response"""
        log.err(error)
        request.setResponseCode(500)  # Internal Server Error
        request.write(error.getErrorMessage())
        request.finish()

    def _handle_bad_request(self, request, *args, **kwargs):
        """Handle a bad request"""
        request.setResponseCode(400)  # Bad Request
        request.finish()

    def _render_to_json(self, result, request, *args, **kwargs):
        """Render the ``result`` as a JSON string.

        If the result is ``None`` return an HTTP 404 response.

        """
        if result is not None:
            data = json.dumps(result, cls=JSONEncoder)
            request.setResponseCode(200)  # OK
            request.setHeader('Content-Type', 'application/json')
            request.write(data)
        else:
            request.setResponseCode(404)  # Not Found
        request.finish()

    def _parse_json(self, request):
        """Return the POSTed data as a JSON object.

        If the *Content-Type* is anything other than *application/json*
        return ``None``.

        """
        content_type = request.getHeader('Content-Type')
        if request.method == 'POST' and content_type == 'application/json':
            content = request.content.read()
            return json.loads(content, cls=JSONDecoder)
        return None


class TransactionResource(BaseResource):
    """Expose a REST interface for a transaction"""

    isLeaf = True

    def __init__(self, connection_pool):
        BaseResource.__init__(self, connection_pool)
        self._notification_mapping = self._create_notification_mapping()

    def _create_notification_mapping(self):
        """
        Constructs a mapping from precentage of credits used to the
        notification percentage immediately above it.

        Only percentages from the lowest percentage to the highest percentage
        (inclusive) are entered in the mapping.
        """
        levels = sorted(
            int(i) for i in app_settings.LOW_CREDIT_NOTIFICATION_PERCENTAGES)

        if not levels:
            return []

        mapping = []
        level_idx = 0

        for i in range(levels[0], levels[-1] + 1):
            mapping.append(levels[level_idx])
            if mapping[i - levels[0]] == i:
                level_idx += 1

        return mapping

    def render_POST(self, request):
        """Handle an HTTP POST request"""
        data = self._parse_json(request)
        if data:
            account_number = data.get('account_number', None)
            message_id = data.get('message_id', None)
            tag_pool_name = data.get('tag_pool_name', None)
            tag_name = data.get('tag_name', None)
            provider = data.get('provider', None)
            message_direction = data.get('message_direction', None)
            session_created = data.get('session_created', None)
            transaction_type = data.get('transaction_type', None)

            if all((account_number, message_id, tag_pool_name, tag_name,
                    message_direction, session_created is not None)):
                d = self.create_transaction(
                    account_number, message_id, tag_pool_name, tag_name,
                    provider, message_direction,
                    session_created, transaction_type)

                d.addCallbacks(self._render_to_json, self._handle_error,
                               callbackArgs=[request], errbackArgs=[request])
            else:
                self._handle_bad_request(request)
        else:
            self._handle_bad_request(request)
        return NOT_DONE_YET

    @defer.inlineCallbacks
    def get_cost(self, account_number, tag_pool_name, provider,
                 message_direction, session_created):
        """Return the message cost"""
        query = """
            SELECT t.account_number, t.tag_pool_name,
                   t.provider, t.message_direction,
                   t.message_cost, t.storage_cost, t.session_cost,
                   t.markup_percent
            FROM (SELECT a.account_number, t.name AS tag_pool_name,
                         c.provider, c.message_direction,
                         c.message_cost, c.storage_cost, c.session_cost,
                         c.markup_percent
                  FROM billing_messagecost c
                  LEFT OUTER JOIN billing_tagpool t ON (c.tag_pool_id = t.id)
                  LEFT OUTER JOIN billing_account a ON (c.account_id = a.id)
                  WHERE
                      (a.account_number = %(account_number)s OR
                       c.account_id IS NULL)
                      AND
                      (t.name = %(tag_pool_name)s OR c.tag_pool_id IS NULL)
                      AND
                      (c.provider = %(provider)s OR c.provider IS NULL)
                      AND
                      (c.message_direction = %(message_direction)s)
            ) as t
            ORDER BY
                t.account_number NULLS LAST,
                t.tag_pool_name NULLS LAST,
                t.provider NULLS LAST
            LIMIT 1
        """

        params = {
            'account_number': account_number,
            'tag_pool_name': tag_pool_name,
            'provider': provider,
            'message_direction': message_direction,
        }

        result = yield self._connection_pool.runQuery(query, params)
        if len(result) > 0:
            message_cost = result[0]
            message_cost['credit_amount'] = MessageCost.calculate_credit_cost(
                message_cost['message_cost'],
                message_cost['storage_cost'],
                message_cost['markup_percent'],
                message_cost['session_cost'],
                session_created=session_created)

            defer.returnValue(message_cost)
        else:
            defer.returnValue(None)

    @defer.inlineCallbacks
    def create_transaction_interaction(self, cursor, account_number,
                                       message_id, tag_pool_name, tag_name,
                                       provider, message_direction,
                                       session_created, transaction_type):
        """Create a new transaction for the given ``account_number``"""
        # Get the message cost
        result = yield self.get_cost(account_number, tag_pool_name, provider,
                                     message_direction, session_created)
        if result is None:
            raise BillingError(
                "Unable to determine %s message cost for account %s"
                " and tag pool %s" % (
                    message_direction, account_number, tag_pool_name))

        message_cost = result.get('message_cost', 0)
        session_cost = result.get('session_cost', 0)
        storage_cost = result.get('storage_cost', 0)
        markup_percent = result.get('markup_percent', 0)
        credit_amount = result.get('credit_amount', 0)

        message_credits = MessageCost.calculate_message_credit_cost(
            message_cost, markup_percent)

        storage_credits = MessageCost.calculate_storage_credit_cost(
            storage_cost, markup_percent)

        session_credits = MessageCost.calculate_session_credit_cost(
            session_cost, markup_percent)

        # Create a new transaction
        query = """
            INSERT INTO billing_transaction
                (account_number, message_id, transaction_type,
                 tag_pool_name, tag_name,
                 provider, message_direction,
                 message_cost, storage_cost,
                 session_created, session_cost, markup_percent,
                 message_credits, storage_credits, session_credits,
                 credit_factor, credit_amount, status, created, last_modified)
            VALUES
                (%(account_number)s, %(message_id)s, %(transaction_type)s,
                 %(tag_pool_name)s, %(tag_name)s,
                 %(provider)s, %(message_direction)s,
                 %(message_cost)s, %(storage_cost)s,
                 %(session_created)s, %(session_cost)s, %(markup_percent)s,
                 %(message_credits)s, %(storage_credits)s, %(session_credits)s,
                 %(credit_factor)s, %(credit_amount)s,
                 'Completed', now(),
                 now())
            RETURNING id, account_number, message_id, transaction_type,
                      tag_pool_name, tag_name,
                      provider, message_direction,
                      message_cost, storage_cost, session_cost,
                      session_created, markup_percent,
                      message_credits, storage_credits, session_credits,
                      credit_factor, credit_amount, status,
                      created, last_modified
        """

        params = {
            'account_number': account_number,
            'message_id': message_id,
            'transaction_type': transaction_type,
            'tag_pool_name': tag_pool_name,
            'tag_name': tag_name,
            'provider': provider,
            'message_direction': message_direction,
            'message_cost': message_cost,
            'storage_cost': storage_cost,
            'session_created': session_created,
            'session_cost': session_cost,
            'markup_percent': markup_percent,
            'message_credits': message_credits,
            'storage_credits': storage_credits,
            'session_credits': session_credits,
            'credit_factor': app_settings.CREDIT_CONVERSION_FACTOR,
            'credit_amount': -credit_amount
        }

        cursor = yield cursor.execute(query, params)
        transaction = yield cursor.fetchone()

        # Update the account's credit balance
        query = """
            UPDATE billing_account
            SET credit_balance = credit_balance - %(credit_amount)s
            WHERE account_number = %(account_number)s
        """

        params = {
            'credit_amount': credit_amount,
            'account_number': account_number
        }

        cursor = yield cursor.execute(query, params)

        # Check the account's credit balance and raise an
        # alert if it has gone below the credit balance threshold
        query = """SELECT credit_balance, last_topup_balance
                   FROM billing_account
                   WHERE account_number = %(account_number)s"""

        params = {'account_number': account_number}
        cursor = yield cursor.execute(query, params)
        result = yield cursor.fetchone()

        if result is None:
            raise BillingError(
                "Unable to find billing account %s while checking"
                " credit balance. Message was %s to/from tag pool %s." % (
                    account_number, message_direction, tag_pool_name))

        credit_balance = result.get('credit_balance')
        last_topup_balance = result.get('last_topup_balance')
        if app_settings.ENABLE_LOW_CREDIT_NOTIFICATION:
            yield self.check_and_notify_low_credit_threshold(
                credit_balance, credit_amount, last_topup_balance,
                account_number)

        defer.returnValue(transaction)

    def check_and_notify_low_credit_threshold(
            self, credit_balance, credit_amount, last_topup_balance,
            account_number):
        """
        Checks the current balance percentage against all those stored within
        the settings. Sends the notification email if it is required. Returns
        the alert percent if email was sent, or ``None`` if no email was sent.

        :param credit_balance: The current balance (after the transaction)
        :param credit_amount: The amount of credits used in the transaction
        :param last_topup_balance: The account credit balance at the last topup
        :param account_number: The account number of the associated account
        """
        level = self.check_all_low_credit_thresholds(
            credit_balance, credit_amount, last_topup_balance)
        if level is not None:
            return spawn_celery_task_via_thread(
                create_low_credit_notification, account_number,
                level, credit_balance)

    def _get_notification_level(self, percentage):
        """
        Fetches the value of the notification level for the given percentage.

        :param int percentage:
            The percentage to get the notification level for

        :return:
            An int representing the current notification level.
        """
        if not self._notification_mapping:
            return None

        minimum = self._notification_mapping[0]
        if percentage < minimum:
            return minimum
        if percentage > self._notification_mapping[-1]:
            return None
        return self._notification_mapping[percentage - minimum]

    def check_all_low_credit_thresholds(
            self, credit_balance, credit_amount, last_topup_balance):
        """
        Checks the current balance percentage against all those stored within
        the settings.

        :param credit_balance:
            The current balance (after the transaction)
        :param credit_amount:
            The amount of credits used in the transaction
        :param last_topup_balance:
            The account credit balance at the last topup

        :return:
            A :class:`Decimal` percentage for the alert threshold crossed
            or ``None`` if no threshold was crossed.
        """
        if not last_topup_balance:
            return None

        def ceil_percent(n):
            return int(math.ceil(n * 100 / last_topup_balance))

        current_percentage = ceil_percent(credit_balance)
        current_notification_level = self._get_notification_level(
            current_percentage)
        previous_percentage = ceil_percent(credit_balance + credit_amount)
        previous_notification_level = self._get_notification_level(
            previous_percentage)

        if current_notification_level != previous_notification_level:
            return Decimal(str(current_notification_level / 100.0))

    @defer.inlineCallbacks
    def create_transaction(self, account_number, message_id, tag_pool_name,
                           tag_name, provider, message_direction,
                           session_created, transaction_type):
        """Create a new transaction for the given ``account_number``"""
        result = yield self._connection_pool.runInteraction(
            self.create_transaction_interaction, account_number, message_id,
            tag_pool_name, tag_name, provider, message_direction,
            session_created, transaction_type)

        defer.returnValue(result)


class Root(BaseResource):
    """The root resource"""

    def __init__(self, connection_pool):
        BaseResource.__init__(self, connection_pool)
        self.putChild('transactions', TransactionResource(connection_pool))

    def getChild(self, name, request):
        if name == '':
            return self
        return Resource.getChild(self, name, request)

    def render_GET(self, request):
        request.setResponseCode(200)  # OK
        return ''
