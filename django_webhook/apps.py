# pylint: disable=import-outside-toplevel

from django.apps import AppConfig
from django.db.models.signals import post_migrate


def sync_topics_after_migrate(**kwargs):
    from django_webhook.models import sync_topics

    sync_topics()


class WebhooksConfig(AppConfig):
    name = "django_webhook"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        # pylint: disable=unused-import
        import django_webhook.checks
        from django_webhook.admin import register_admin
        from django_webhook.settings import get_settings
        from django_webhook.signals import connect_signals

        connect_signals()
        register_admin()

        if get_settings()["SYNC_TOPICS_ON_MIGRATE"]:
            post_migrate.connect(
                sync_topics_after_migrate,
                sender=self,
                dispatch_uid="django_webhook.sync_topics_after_migrate",
            )
