"""
Manual re-send of recorded deliveries: programmatic, batch and via the
admin action.
"""

import pytest
from celery import states
from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from pytest_django.asserts import assertNumQueries

from django_webhook.admin import WebhookEventAdmin
from django_webhook.models import INVALID, RETRYING, WebhookEvent, WebhookTopic
from django_webhook.tasks import resend_webhook_event, resend_webhook_events
from django_webhook.test_factories import WebhookEventFactory, WebhookFactory

pytestmark = pytest.mark.django_db


def _failed_event(responses, status_on_resend=200):
    topic, _ = WebhookTopic.objects.get_or_create(name="tests.User/create")
    webhook = WebhookFactory(topics=[topic], secrets__token="a-very-secret-token")
    responses.post(webhook.url, status=status_on_resend)
    return WebhookEventFactory(
        webhook=webhook,
        status=states.FAILURE,
        url=webhook.url,
        object={"object": {"id": 1}, "topic": "tests.User/create"},
        error="delivery failed earlier",
    )


@pytest.mark.usefixtures("responses")
def test_resend_single_event_programmatic(settings, responses):
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.User"], USE_CACHE=False)
    event = _failed_event(responses)

    assert resend_webhook_event(event.id) is True

    assert len(responses.calls) == 1
    event.refresh_from_db()
    assert event.status == states.SUCCESS
    assert event.error is None  # cleared on a successful re-send


def test_resend_batch(settings, responses):
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.User"], USE_CACHE=False)
    e1 = _failed_event(responses)
    e2 = _failed_event(responses)

    count = resend_webhook_events(
        WebhookEvent.objects.filter(id__in=[e1.id, e2.id]).failed()
    )
    assert count == 2
    assert len(responses.calls) == 2


def test_batch_resend_uses_single_update(settings, mocker):
    # A batch re-send must not issue one UPDATE per event (no N+1 writes): it
    # resets the whole selection in a single statement, then enqueues each.
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.User"], USE_CACHE=False)
    mocker.patch("django_webhook.tasks.fire_webhook")
    topic, _ = WebhookTopic.objects.get_or_create(name="tests.User/create")
    webhook = WebhookFactory(topics=[topic])
    events = [
        WebhookEventFactory(webhook=webhook, status=states.FAILURE) for _ in range(5)
    ]
    qs = WebhookEvent.objects.filter(id__in=[e.id for e in events])

    # 1 SELECT (fetch the rows) + 1 UPDATE (bulk reset), regardless of count.
    with assertNumQueries(2):  # pylint: disable=not-context-manager
        count = qs.resend()
    assert count == 5


def test_resend_increments_resends_and_accumulates_attempts(settings, responses):
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.User"], USE_CACHE=False)
    event = _failed_event(responses)
    WebhookEvent.objects.filter(id=event.id).update(attempts=3)

    assert resend_webhook_event(event.id) is True

    event.refresh_from_db()
    assert event.resends == 1
    assert event.attempts == 4


def test_cannot_resend_a_delivery_still_in_flight(settings, responses):
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.User"], USE_CACHE=False)
    event = _failed_event(responses)
    WebhookEvent.objects.filter(id=event.id).update(status=RETRYING)
    event.refresh_from_db()

    assert event.resend() is False
    assert len(responses.calls) == 0
    event.refresh_from_db()
    assert event.status == RETRYING
    assert event.resends == 0


def test_cannot_resend_an_invalid_payload(settings, responses):
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.User"], USE_CACHE=False)
    event = _failed_event(responses)
    WebhookEvent.objects.filter(id=event.id).update(status=INVALID)
    event.refresh_from_db()

    assert event.resend() is False
    assert len(responses.calls) == 0


def test_batch_resend_skips_non_resendable(settings, responses):
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.User"], USE_CACHE=False)
    resendable = _failed_event(responses)
    in_flight = _failed_event(responses)
    unproducible = _failed_event(responses)
    WebhookEvent.objects.filter(id=in_flight.id).update(status=RETRYING)
    WebhookEvent.objects.filter(id=unproducible.id).update(status=INVALID)

    count = WebhookEvent.objects.filter(
        id__in=[resendable.id, in_flight.id, unproducible.id]
    ).resend()

    assert count == 1
    assert len(responses.calls) == 1


def test_failed_queryset_excludes_in_flight_and_invalid(settings, responses):
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.User"], USE_CACHE=False)
    failed = _failed_event(responses)
    in_flight = _failed_event(responses)
    unproducible = _failed_event(responses)
    WebhookEvent.objects.filter(id=in_flight.id).update(status=RETRYING)
    WebhookEvent.objects.filter(id=unproducible.id).update(status=INVALID)

    assert set(WebhookEvent.objects.failed().values_list("id", flat=True)) == {
        failed.id
    }
    assert set(WebhookEvent.objects.unrecovered().values_list("id", flat=True)) == {
        failed.id,
        unproducible.id,
    }


def test_resend_missing_subscription_is_noop(settings, responses):
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.User"], USE_CACHE=False)
    event = WebhookEventFactory(webhook=None, status=states.FAILURE)
    assert event.resend() is False
    assert len(responses.calls) == 0


def test_admin_resend_action(settings, responses, rf):
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.User"], USE_CACHE=False)
    event = _failed_event(responses)

    admin_instance = WebhookEventAdmin(WebhookEvent, AdminSite())
    request = rf.post("/admin/")
    # Messages framework needs a storage backend on the request.
    setattr(request, "session", {})
    setattr(request, "_messages", FallbackStorage(request))

    admin_instance.resend_selected(request, WebhookEvent.objects.filter(id=event.id))

    assert len(responses.calls) == 1
    event.refresh_from_db()
    assert event.status == states.SUCCESS
