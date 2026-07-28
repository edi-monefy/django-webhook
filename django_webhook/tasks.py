import logging
from datetime import timedelta

from celery import current_app as app
from celery import states
from celery.utils.time import get_exponential_backoff_interval
from django.db.models import F
from django.utils import timezone
from requests import Session
from requests.exceptions import RequestException

from django_webhook.constants import RETRYING, TERMINAL_STATES
from django_webhook.models import Webhook, WebhookEvent

from .http import prepare_request
from .settings import failed_retention_days, get_settings, succeeded_retention_days

logger = logging.getLogger(__name__)


@app.task(
    bind=True,
    max_retries=5,
    default_retry_delay=60,
    retry_backoff=True,
    retry_backoff_max=60 * 60,
    retry_jitter=False,
)
def fire_webhook(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    self,
    webhook_id: int,
    payload: str,
    topic=None,  # pylint: disable=unused-argument
    object_type=None,  # pylint: disable=unused-argument
    webhook_event_id=None,
):
    """
    Deliver ``payload`` to ``webhook_id``.

    When ``webhook_event_id`` is given, that pre-created :class:`WebhookEvent`
    row is updated as the delivery progresses; the row is created by the
    dispatcher before enqueueing so an enqueue failure is never a silent loss.
    When it is absent (``STORE_EVENTS`` off, or a legacy caller) the delivery is
    sent without recording.

    All three ways a delivery can fail to be confirmed — connection failure,
    timeout, and error response — are handled identically: record the failure,
    then retry with exponential backoff up to ``MAX_RETRIES``. The row is
    ``RETRYING`` while attempts remain and only ``FAILURE`` once they are spent,
    so a delivery still in flight is never mistaken for one that gave up.
    """
    settings = get_settings()
    self.max_retries = settings["MAX_RETRIES"]

    webhook = Webhook.objects.filter(id=webhook_id).first()
    if webhook is None or not webhook.active:
        logging.warning(
            "Webhook: %s is missing/inactive and will not be fired.", webhook_id
        )
        if webhook_event_id is not None:
            _update_event(
                webhook_event_id,
                status=states.FAILURE,
                error="webhook inactive or deleted; not delivered",
            )
        return

    req = prepare_request(webhook, payload)  # type: ignore
    timeout = settings["REQUEST_TIMEOUT"]
    _record_attempt(webhook_event_id)

    try:
        response = Session().send(req, timeout=timeout)
        response.raise_for_status()
    except RequestException as ex:
        # ``ex.response`` is absent for connection failures and timeouts.
        status_code = getattr(ex.response, "status_code", None)
        logging.warning(
            "Webhook request failed webhook_id=%s status_code=%s attempt=%s",
            webhook_id,
            status_code,
            self.request.retries + 1,
        )
        exhausted = self.request.retries >= self.max_retries
        if webhook_event_id is not None:
            _update_event(
                webhook_event_id,
                status=states.FAILURE if exhausted else RETRYING,
                error=f"delivery failed: {ex!r} status_code={status_code}",
            )
        raise self.retry(exc=ex, countdown=retry_countdown(self))

    if webhook_event_id is not None:
        _update_event(
            webhook_event_id, status=states.SUCCESS, delivered_at=timezone.now()
        )


def retry_countdown(task):
    """
    Seconds to wait before ``task``'s next retry, growing exponentially and
    capped at its ``retry_backoff_max``.
    """
    return get_exponential_backoff_interval(
        factor=task.default_retry_delay,
        retries=task.request.retries,
        maximum=getattr(task, "retry_backoff_max", 60 * 60),
        full_jitter=getattr(task, "retry_jitter", False),
    )


def _record_attempt(webhook_event_id):
    if webhook_event_id is None:
        return
    WebhookEvent.objects.filter(id=webhook_event_id).update(
        attempts=F("attempts") + 1, last_attempt_at=timezone.now()
    )


def _update_event(webhook_event_id, *, status, error=None, delivered_at=None):
    fields = {"status": status, "error": error}
    if delivered_at is not None:
        fields["delivered_at"] = delivered_at
    WebhookEvent.objects.filter(id=webhook_event_id).update(**fields)


def resend_webhook_event(webhook_event_id):
    """
    Public programmatic re-send for a single recorded delivery. Returns ``True``
    if the delivery was re-enqueued.
    """
    event = WebhookEvent.objects.filter(id=webhook_event_id).first()
    if event is None:
        return False
    return event.resend()


def resend_webhook_events(queryset):
    """
    Re-send a filtered batch of recorded deliveries. Accepts any
    :class:`WebhookEvent` queryset. Returns the number re-enqueued.
    """
    return queryset.resend()


@app.task(
    autoretry_for=(Exception,),
    max_retries=3,
    default_retry_delay=60,
    retry_backoff=True,
    retry_backoff_max=60 * 60,
    retry_jitter=False,
)
def clear_webhook_events():
    """
    Purge old webhook events.

    Succeeded deliveries are purged on their own window. Failed and ``INVALID``
    deliveries share a second, independent one so that neither an unrecovered
    failure nor an unproducible payload is silently discarded on a timer; both
    are retained forever unless ``FAILED_EVENTS_RETENTION_DAYS`` is set.

    Rows left non-terminal by a crashed worker (e.g. the dispatcher created the
    ``PENDING`` row but ``fire_webhook`` never completed — broker restart, OOM,
    deploy) are first reaped into ``FAILURE`` so they gain a terminal state and a
    cleanup path instead of accumulating with no way to prune.
    """
    now = timezone.now()

    succeeded_days = succeeded_retention_days()
    failed_days = failed_retention_days()

    deleted = 0
    if succeeded_days is not None:
        cutoff = now - timedelta(days=succeeded_days)

        reaped = (
            WebhookEvent.objects.exclude(status__in=TERMINAL_STATES)
            .filter(created__lt=cutoff)
            .update(
                status=states.FAILURE,
                error="abandoned: no terminal status within the retention window",
            )
        )
        if reaped:
            logging.warning("Reaped %s abandoned webhook events into FAILURE", reaped)

        qs = WebhookEvent.objects.succeeded().filter(created__lt=cutoff)
        logging.info(
            "Clearing %s succeeded webhook events older than %s", qs.count(), cutoff
        )
        deleted += qs.delete()[0]

    if failed_days is not None:
        cutoff = now - timedelta(days=failed_days)
        qs = WebhookEvent.objects.unrecovered().filter(created__lt=cutoff)
        logging.info(
            "Clearing %s failed/invalid webhook events older than %s",
            qs.count(),
            cutoff,
        )
        deleted += qs.delete()[0]
    else:
        logging.info(
            "FAILED_EVENTS_RETENTION_DAYS is unset; failed and invalid "
            "deliveries are retained."
        )

    return deleted
