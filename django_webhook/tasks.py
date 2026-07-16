import logging
from datetime import timedelta

from celery import current_app as app
from celery import states
from django.utils import timezone
from requests import Session
from requests.exceptions import RequestException

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
    row is updated to its terminal status; the row is created by the dispatcher
    before enqueueing so an enqueue failure is never a silent loss. When it is
    absent (``STORE_EVENTS`` off, or a legacy caller) the delivery is sent
    without recording.

    All three ways a delivery can fail to be confirmed — connection failure,
    timeout, and error response — are handled identically: record the failure,
    then retry with backoff up to ``MAX_RETRIES``. The row always reaches a
    terminal recorded state.
    """
    settings = get_settings()
    self.max_retries = settings["MAX_RETRIES"]

    webhook = Webhook.objects.filter(id=webhook_id).first()
    if webhook is None or not webhook.active:
        logging.warning(
            "Webhook: %s is missing/inactive and will not be fired.", webhook_id
        )
        if webhook_event_id is not None:
            # Not a delivery failure: the subscription opted out. Leave a
            # terminal, non-retryable marker rather than a stranded PENDING row.
            _update_event(
                webhook_event_id,
                status=states.FAILURE,
                error="webhook inactive or deleted; not delivered",
            )
        return

    req = prepare_request(webhook, payload)  # type: ignore
    timeout = settings["REQUEST_TIMEOUT"]

    try:
        response = Session().send(req, timeout=timeout)
        response.raise_for_status()
    except RequestException as ex:
        # ``ex.response`` is present only for error responses; it is absent for
        # connection failures and timeouts. Guard it so those two — the most
        # common real-world failures — retry and record like any other.
        status_code = getattr(ex.response, "status_code", None)
        logging.warning(
            "Webhook request failed webhook_id=%s status_code=%s",
            webhook_id,
            status_code,
        )
        if webhook_event_id is not None:
            _update_event(
                webhook_event_id,
                status=states.FAILURE,
                error=f"delivery failed: {ex!r} status_code={status_code}",
            )
        raise self.retry(exc=ex)

    if webhook_event_id is not None:
        _update_event(webhook_event_id, status=states.SUCCESS)


def _update_event(webhook_event_id, *, status, error=None):
    # error tracks the reason the row is not (yet) successfully delivered, so it
    # is set on failure and cleared on success. A prior attempt's failure note
    # must not survive a later successful retry. Any production-time degradation
    # (e.g. a value the encoder had to coerce) is still visible in the stored
    # payload itself, so clearing the note here loses nothing that was omitted.
    WebhookEvent.objects.filter(id=webhook_event_id).update(status=status, error=error)


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

    Succeeded and failed deliveries are purged on independent windows so that a
    delivery that failed and was never re-sent is not silently discarded on a
    timer. Failed deliveries are retained forever unless
    ``FAILED_EVENTS_RETENTION_DAYS`` is set explicitly.

    Rows left non-terminal by a crashed worker (e.g. the dispatcher created the
    ``PENDING`` row but ``fire_webhook`` never completed — broker restart, OOM,
    deploy) are first reaped into ``FAILURE`` so they gain a terminal state and a
    cleanup path (failed retention) instead of accumulating with no way to prune.
    """
    now = timezone.now()

    succeeded_days = succeeded_retention_days()
    failed_days = failed_retention_days()

    deleted = 0
    if succeeded_days is not None:
        cutoff = now - timedelta(days=succeeded_days)

        # Reap abandoned in-flight rows (anything not SUCCESS/FAILURE) older than
        # the window into FAILURE. A row is only non-terminal between creation
        # and its first delivery attempt, so any that old was left behind.
        reaped = (
            WebhookEvent.objects.exclude(status__in=[states.SUCCESS, states.FAILURE])
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
        qs = WebhookEvent.objects.failed().filter(created__lt=cutoff)
        logging.info(
            "Clearing %s failed webhook events older than %s", qs.count(), cutoff
        )
        deleted += qs.delete()[0]
    else:
        logging.info(
            "FAILED_EVENTS_RETENTION_DAYS is unset; failed deliveries are retained."
        )

    return deleted
