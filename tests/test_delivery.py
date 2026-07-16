"""
Delivery reliability: every failure mode retries and records, requests carry a
timeout, and deliveries reach a terminal recorded status.
"""

import pytest
from celery import states
from celery.exceptions import MaxRetriesExceededError, Retry
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout

from django_webhook.models import WebhookEvent
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
