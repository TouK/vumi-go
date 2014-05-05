# -*- test-case-name: go.vumitools.contact.tests.test_migrations -*-
from vumi.persist.model import ModelMigrator


class ContactMigrator(ModelMigrator):

    def migrate_from_unversioned(self, mdata):

        mdata.copy_values(
            'name', 'surname', 'email_address', 'msisdn',
            'dob', 'twitter_handle', 'facebook_id', 'bbm_pin',
            'gtalk_id', 'created_at')
        mdata.copy_dynamic_values(
            'extras-', 'subscription-')
        mdata.copy_indexes('user_account_bin', 'groups_bin')

        # Add stuff that's new in this version
        mdata.set_value('$VERSION', 1)
        mdata.set_value('mxit_id', None)
        mdata.set_value('wechat_id', None)

        return mdata

    def migrate_from_1(self, mdata):

        mdata.copy_values(
            'name', 'surname', 'email_address', 'dob', 'twitter_handle',
            'facebook_id', 'bbm_pin', 'created_at')

        mdata.copy_dynamic_values(
            'extras-', 'subscription-')
        mdata.copy_indexes('user_account_bin', 'groups_bin')

        # Add stuff that's new in this version
        mdata.set_value('$VERSION', 2)

        for field in ('msisdn', 'gtalk_id', 'mxit_id', 'wechat_id'):
            mdata.set_value(
                field, mdata.old_data[field], index=('%s_bin' % (field,)))

        return mdata
