# pylint: disable=import-outside-toplevel

from django.apps import AppConfig


class WebhooksConfig(AppConfig):
    name = "django_webhook"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        # pylint: disable=unused-import
        import django_webhook.checks
        from django_webhook.admin import register_admin
        from django_webhook.models import populate_topics_from_settings
        from django_webhook.signals import connect_signals

        connect_signals()
        register_admin()
        # Best-effort topic population at startup. It silently no-ops when the DB
        # is unavailable (e.g. before migrations); use the ``webhook_sync_topics``
        # management command for a reliable, idempotent sync.
        populate_topics_from_settings()
