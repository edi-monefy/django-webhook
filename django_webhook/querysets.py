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
        Re-enqueue every delivery in this queryset with a single status update
        (plus one task per event). Rows whose subscription was deleted, whose
        payload was never produced, or whose delivery is still in flight are
        skipped. Returns the number re-fired.
        """
        resendable = list(
            self.exclude(webhook_id__isnull=True).filter(status__in=RESENDABLE_STATES)
        )
        if not resendable:
            return 0
        self.model.objects.filter(id__in=[e.id for e in resendable]).update(
            status=states.PENDING, error=None, resends=models.F("resends") + 1
        )
        for event in resendable:
            event._enqueue_delivery()  # type: ignore[attr-defined]  # pylint: disable=protected-access
        return len(resendable)
