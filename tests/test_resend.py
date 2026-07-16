"""
Manual re-send of recorded deliveries (spec B4): programmatic, batch and via the
admin action.
"""

import pytest
from celery import states
from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage

from django_webhook.admin import WebhookEventAdmin
from django_webhook.models import WebhookEvent, WebhookTopic
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
