from celery import states
from django.db import models

from .constants import INVALID, RESENDABLE_STATES


class WebhookEventQuerySet(models.QuerySet):
    def failed(self):
        return self.filter(status=states.FAILURE)

    def succeeded(self):
        return self.filter(status=states.SUCCESS)

    def invalid(self):
        return self.filter(status=INVALID)

    def unrecovered(self):
        """
        Failed deliveries plus unproducible payloads. For retention, which purges
        both on the same window; re-sending uses :meth:`failed` only.
        """
        return self.filter(status__in=[states.FAILURE, INVALID])

    def resend(self):
        """
        Re-enqueue every delivery in this queryset. Rows whose subscription was
        deleted, whose payload was never produced, or whose delivery is still in
        flight are skipped. Returns the number re-fired.

        The pre-filter only narrows the candidates; each row is claimed by
        :meth:`WebhookEvent.resend`, whose status check lives in the UPDATE. A
        batch claim filtered on id alone would let a row that changed status
        after this SELECT be stomped back to PENDING mid-flight, and would let
        two concurrent callers both claim the same row and double-deliver.
        """
        resendable = self.exclude(webhook_id__isnull=True).filter(
            status__in=RESENDABLE_STATES
        )
        return sum(event.resend() for event in resendable)
