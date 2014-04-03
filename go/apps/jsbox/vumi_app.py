# -*- test-case-name: go.apps.jsbox.tests.test_vumi_app -*-
# -*- coding: utf-8 -*-

"""Vumi application worker for the vumitools API."""

from twisted.internet.defer import inlineCallbacks

from vumi.application.sandbox import JsSandbox, SandboxResource
from vumi.config import ConfigDict
from vumi.message import TransportUserMessage
from vumi import log

from go.vumitools.app_worker import (
    GoApplicationMixin, GoApplicationConfigMixin)


class ConversationConfigResource(SandboxResource):
    """Resource that provides access to conversation config."""

    def handle_get(self, api, command):
        key = command.get("key")
        if key is None:
            return self.reply(command, success=False)
        conversation = self.app_worker.conversation_for_api(api)
        app_config = conversation.config.get("jsbox_app_config", {})
        key_config = app_config.get(key, {})
        value = key_config.get('value')
        return self.reply(command, value=value, success=True)


class JsBoxConfig(JsSandbox.CONFIG_CLASS, GoApplicationConfigMixin):
    jsbox_app_config = ConfigDict(
        "Custom configuration passed to the javascript code.", default={})
    jsbox = ConfigDict(
        "Must have 'javascript' field containing JavaScript code to run.")

    @property
    def javascript(self):
        if not self.jsbox:
            return None
        return self.jsbox['javascript']

    @property
    def sandbox_id(self):
        return self.conversation.user_account.key


class JsBoxApplication(GoApplicationMixin, JsSandbox):
    """
    Application that processes message in a Node.js Javascript Sandbox.

    The Javascript is supplied by a conversation given by the user.

    Configuration parameters:

    :param str worker_name:
        The name of this worker, used for receiving control messages.
    :param dict message_store:
        Message store configuration.
    :param dict api_routing:
        Vumi API command routing information (optional).

    And those from :class:`vumi.application.sandbox.JsSandbox`.
    """

    ALLOWED_ENDPOINTS = None
    CONFIG_CLASS = JsBoxConfig
    worker_name = 'jsbox_application'

    @inlineCallbacks
    def setup_application(self):
        yield super(JsBoxApplication, self).setup_application()
        yield self._go_setup_worker()

    @inlineCallbacks
    def teardown_application(self):
        yield super(JsBoxApplication, self).teardown_application()
        yield self._go_teardown_worker()

    def conversation_for_api(self, api):
        return api.config.conversation

    def user_api_for_api(self, api):
        conv = self.conversation_for_api(api)
        return self.get_user_api(conv.user_account.key)

    def get_config(self, msg):
        return self.get_message_config(msg)

    def infer_delivery_class(self, msg):
        return {
            'smpp': 'sms',
            'sms': 'sms',
            'ussd': 'ussd',
            'twitter': 'twitter',
            'xmpp': 'gtalk',
            'mxit': 'mxit',
            'wechat': 'wechat',
        }.get(msg['transport_type'], 'sms')

    @inlineCallbacks
    def process_message_in_sandbox(self, msg):
        # TODO remove the delivery class inference and injection into the
        # message once we have message address types
        metadata = msg['helper_metadata']
        metadata['delivery_class'] = self.infer_delivery_class(msg)
        config = yield self.get_config(msg)
        if not config.javascript:
            log.warning("No JS for conversation: %s" % (
                config.conversation.key,))
            return
        yield super(JsBoxApplication, self).process_message_in_sandbox(msg)

    def process_command_start(self, user_account_key, conversation_key):
        log.info("Starting javascript sandbox conversation (key: %r)." %
                 (conversation_key,))
        return super(JsBoxApplication, self).process_command_start(
            user_account_key, conversation_key)

    INBOUND_PUSH_TRIGGER = "inbound_push_trigger"

    def mk_inbound_push_trigger(self, to_addr, contact, conversation):
        """
        Construct a dummy inbound message used to trigger a push of
        a new message from a sandbox application.
        """
        msg_options = {
            'transport_name': None,
            'transport_type': None,
            'helper_metadata': {},
            # mark this message as special so that it can be idenitified
            # if it accidentally ends up elsewhere.
            self.INBOUND_PUSH_TRIGGER: True,
        }
        conversation.set_go_helper_metadata(msg_options['helper_metadata'])

        # We reverse the to_addr & from_addr since we're faking input
        # from the client to start the survey.

        # TODO: This generates a fake message id that is then used in
        #       the reply to field of the outbound message. We need to
        #       write special version of the GoOutboundResource that
        #       will set in_reply_to to None on these messages so the
        #       invalid ids don't escape into the rest of the system.

        msg = TransportUserMessage(from_addr=to_addr, to_addr=None,
                                   content=None, **msg_options)
        return msg

    def send_inbound_push_trigger(self, to_addr, contact, conversation):
        log.debug('Starting %r -> %s' % (conversation, to_addr))
        msg = self.mk_inbound_push_message(to_addr, contact, conversation)
        return self.consume_user_message(msg)

    @inlineCallbacks
    def process_command_send_jsbox(self, user_account_key, conversation_key,
                                   batch_id, delivery_class):
        conv = yield self.get_conversation(user_account_key, conversation_key)
        if conv is None:
            log.warning("Cannot find conversation '%s' for user '%s'." % (
                conversation_key, user_account_key))
            return

        for contacts in (yield conv.get_opted_in_contact_bunches(
                delivery_class)):
            for contact in (yield contacts):
                to_addr = contact.addr_for(delivery_class)
                yield self.send_first_dialogue_message(
                    to_addr, contact, conv)
