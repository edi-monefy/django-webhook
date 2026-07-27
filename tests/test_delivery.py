"""
Delivery reliability: every failure mode retries and records, requests carry a
timeout, and deliveries reach a terminal recorded status.
"""

import pytest
from celery import states
from celery.exceptions import MaxRetriesExceededError, Retry
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout

from django_webhook.models import WebhookDeliveryAttempt, WebhookEvent
from django_webhook.tasks import fire_webhook
from django_webhook.test_factories import (
    WebhookEventFactory,
    WebhookFactory,
    WebhookTopicFactory,
)

pytestmark = pytest.mark.django_db


def _webhook():
    return WebhookFactory(
        topics=[WebhookTopicFactory(name="tests.User/create")],
        secrets__token="a-very-secret-token",
    )


def _pending_event(webhook):
    return WebhookEventFactory(
        webhook=webhook,
        status=states.PENDING,
        url=webhook.url,
        object={"hello": "world"},
        error=None,
    )


@pytest.mark.parametrize(
    "failure",
    [RequestsConnectionError("dead"), Timeout("hung"), None],
    ids=["connection-error", "timeout", "error-response"],
)
def test_all_failure_modes_retry_and_reach_terminal_failure(
    settings, responses, failure
):
    # Connection failure, timeout and error response must behave identically:
    # record the failure and retry, ending in a terminal FAILURE —
    # never stranded PENDING.
    settings.DJANGO_WEBHOOK = dict(
        MODELS=["tests.User"], USE_CACHE=False, MAX_RETRIES=1
    )
    webhook = _webhook()
    event = _pending_event(webhook)

    if failure is None:
        responses.post(webhook.url, status=500)
    else:
        responses.post(webhook.url, body=failure)

    # Eager mode surfaces the retry as celery.exceptions.Retry; the point is
    # that all three failure modes take the record-then-retry path identically.
    with pytest.raises((Retry, MaxRetriesExceededError)):
        fire_webhook.delay(
            webhook.id, payload='{"hello": "world"}', webhook_event_id=event.id
        )

    event.refresh_from_db()
    assert event.status == states.FAILURE
    assert event.error
    assert WebhookEvent.objects.filter(status=states.PENDING).count() == 0


def test_request_carries_configured_timeout(settings, mocker):
    # A hung subscriber must not pin a worker forever: requests carry a timeout
    # with a non-infinite default.
    settings.DJANGO_WEBHOOK = dict(
        MODELS=["tests.User"], USE_CACHE=False, REQUEST_TIMEOUT=7
    )
    webhook = _webhook()
    event = _pending_event(webhook)

    send = mocker.patch("django_webhook.tasks.Session.send")
    send.return_value.raise_for_status.return_value = None

    fire_webhook.delay(webhook.id, payload="{}", webhook_event_id=event.id)

    assert send.call_args.kwargs["timeout"] == 7
    event.refresh_from_db()
    assert event.status == states.SUCCESS


def test_success_marks_terminal_success(settings, responses):
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.User"], USE_CACHE=False)
    webhook = _webhook()
    event = _pending_event(webhook)
    responses.post(webhook.url, status=200)

    fire_webhook.delay(webhook.id, payload="{}", webhook_event_id=event.id)

    event.refresh_from_db()
    assert event.status == states.SUCCESS


def test_success_clears_a_prior_failure_error(settings, responses):
    # A delivery that failed once then succeeds on retry must not keep the stale
    # failure note (would read as SUCCESS + error=...).
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.User"], USE_CACHE=False)
    webhook = _webhook()
    event = WebhookEventFactory(
        webhook=webhook,
        status=states.FAILURE,
        url=webhook.url,
        object={"hello": "world"},
        error="delivery failed: earlier attempt status_code=500",
    )
    responses.post(webhook.url, status=200)

    fire_webhook.delay(webhook.id, payload="{}", webhook_event_id=event.id)

    event.refresh_from_db()
    assert event.status == states.SUCCESS
    assert event.error is None


def test_inactive_webhook_reaches_terminal_state(settings, responses):
    # An inactive subscription is not a delivery attempt, but the row must still
    # leave PENDING so it is not mistaken for in-flight.
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.User"], USE_CACHE=False)
    webhook = WebhookFactory(active=False)
    event = _pending_event(webhook)

    fire_webhook.delay(webhook.id, payload="{}", webhook_event_id=event.id)

    event.refresh_from_db()
    assert event.status == states.FAILURE
    assert "inactive" in event.error
    assert len(responses.calls) == 0


# ---------------------------------------------------------------------------
# WebhookDeliveryAttempt — one row per task execution
# ---------------------------------------------------------------------------


def test_successful_delivery_creates_one_success_attempt(settings, responses):
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.User"], USE_CACHE=False)
    webhook = _webhook()
    event = _pending_event(webhook)
    responses.post(webhook.url, status=200)

    fire_webhook.delay(webhook.id, payload="{}", webhook_event_id=event.id)

    attempts = list(WebhookDeliveryAttempt.objects.filter(event=event))
    assert len(attempts) == 1
    assert attempts[0].status == states.SUCCESS
    assert attempts[0].attempt_number == 1
    assert attempts[0].error is None


def test_failed_delivery_creates_one_failure_attempt(settings, responses):
    settings.DJANGO_WEBHOOK = dict(
        MODELS=["tests.User"], USE_CACHE=False, MAX_RETRIES=0
    )
    webhook = _webhook()
    event = _pending_event(webhook)
    responses.post(webhook.url, status=500)

    with pytest.raises(Exception):
        fire_webhook.delay(webhook.id, payload="{}", webhook_event_id=event.id)

    attempts = list(WebhookDeliveryAttempt.objects.filter(event=event))
    assert len(attempts) == 1
    assert attempts[0].status == states.FAILURE
    assert attempts[0].attempt_number == 1
    assert attempts[0].error is not None


def test_each_retry_creates_its_own_attempt_row(settings, responses):
    # With CELERY_TASK_EAGER_PROPAGATES each .delay() call is one execution.
    # Three explicit calls simulate the initial delivery + 2 retries.
    settings.DJANGO_WEBHOOK = dict(
        MODELS=["tests.User"], USE_CACHE=False, MAX_RETRIES=0
    )
    webhook = _webhook()
    event = _pending_event(webhook)

    for _ in range(3):
        responses.post(webhook.url, status=500)
        with pytest.raises(Exception):
            fire_webhook.delay(webhook.id, payload="{}", webhook_event_id=event.id)

    attempts = list(
        WebhookDeliveryAttempt.objects.filter(event=event).order_by("attempt_number")
    )
    assert len(attempts) == 3
    assert [a.attempt_number for a in attempts] == [1, 2, 3]
    assert all(a.status == states.FAILURE for a in attempts)


def test_attempt_numbers_continue_sequentially_after_resend(
    settings, responses, mocker
):
    # After a resend the Celery retries counter resets to 0; attempt numbers
    # must still be globally sequential (4, 5, …) not reset to 1.
    settings.DJANGO_WEBHOOK = dict(
        MODELS=["tests.User"], USE_CACHE=False, MAX_RETRIES=0
    )
    webhook = _webhook()
    event = _pending_event(webhook)

    # First delivery → attempt #1 (failure)
    responses.post(webhook.url, status=500)
    with pytest.raises(Exception):
        fire_webhook.delay(webhook.id, payload="{}", webhook_event_id=event.id)

    # Simulate a resend (reset event status to PENDING, re-fire)
    WebhookEvent.objects.filter(id=event.id).update(status=states.PENDING, error=None)
    responses.post(webhook.url, status=200)
    fire_webhook.delay(webhook.id, payload="{}", webhook_event_id=event.id)

    attempts = list(
        WebhookDeliveryAttempt.objects.filter(event=event).order_by("attempt_number")
    )
    assert len(attempts) == 2
    assert attempts[0].attempt_number == 1
    assert attempts[0].status == states.FAILURE
    assert attempts[1].attempt_number == 2
    assert attempts[1].status == states.SUCCESS


def test_inactive_webhook_creates_no_attempt_row(settings, responses):
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.User"], USE_CACHE=False)
    webhook = WebhookFactory(active=False)
    event = _pending_event(webhook)

    fire_webhook.delay(webhook.id, payload="{}", webhook_event_id=event.id)

    assert WebhookDeliveryAttempt.objects.filter(event=event).count() == 0


def test_no_attempt_row_when_store_events_disabled(settings, responses):
    # When STORE_EVENTS is off there is no WebhookEvent row, so
    # webhook_event_id is None and no attempt is recorded.
    settings.DJANGO_WEBHOOK = dict(
        MODELS=["tests.User"], USE_CACHE=False, STORE_EVENTS=False
    )
    webhook = _webhook()
    responses.post(webhook.url, status=200)

    fire_webhook.delay(webhook.id, payload="{}")

    assert WebhookDeliveryAttempt.objects.count() == 0
