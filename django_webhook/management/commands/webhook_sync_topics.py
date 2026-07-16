from django.core.management.base import BaseCommand

from django_webhook.models import sync_topics


class Command(BaseCommand):
    help = (
        "Reconcile the WebhookTopic table with DJANGO_WEBHOOK['MODELS']. "
        "Idempotent; safe to run repeatedly (e.g. after migrations or deploys)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-prune",
            action="store_true",
            help="Do not delete topics that are no longer implied by settings.",
        )

    def handle(self, *args, **options):
        created, deleted = sync_topics(prune=not options["no_prune"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Synced webhook topics: {created} created, {deleted} deleted."
            )
        )
