from optparse import make_option

from django.core.management.base import BaseCommand, CommandError

from go.base.utils import vumi_api_for_user
from go.base.command_utils import get_user_by_email


class Command(BaseCommand):
    help = "Tell a conversation to start"

    LOCAL_OPTIONS = [
        make_option('--conversation-key',
            dest='conversation_key',
            help='What conversation to load'),
        make_option('--email-address',
            dest='email_address',
            default=False,
            help='The account to start a conversation for.'),
    ]
    option_list = BaseCommand.option_list + tuple(LOCAL_OPTIONS)

    def get_user_api(self, email):
        return vumi_api_for_user(get_user_by_email(email))

    def handle(self, *apps, **options):
        email_address = options['email_address']
        if not email_address:
            raise CommandError('Please provide --email-address.')

        conversation_key = options['conversation_key']
        if not conversation_key:
            raise CommandError('Please provide --conversation-key.')

        user_api = self.get_user_api(email_address)
        conversation = user_api.get_wrapped_conversation(conversation_key)
        if conversation is None:
            raise CommandError('Conversation does not exist.')

        handler = getattr(self, 'start_%s' %
            (conversation.conversation_type,),
            self.default_start_conversation)
        handler(user_api, conversation)

    def default_start_conversation(self, user_api, conversation):
        if conversation.archived() or conversation.running():
            raise CommandError('Conversation already started.')

        conversation.start()
