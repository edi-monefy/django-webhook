from datetime import date, datetime, timedelta

import pytest
from celery import states
from django.core.serializers.json import DjangoJSONEncoder
from django.test import override_settings
from django.utils import timezone

from django_webhook.models import WebhookEvent
from django_webhook.tasks import clear_webhook_events
from django_webhook.test_factories import (
    WebhookEventFactory,
    WebhookFactory,
    WebhookTopicFactory,
)
from tests.model_data import TEST_USER
from tests.models import User

pytestmark = pytest.mark.django_db


@override_settings(
    DJANGO_WEBHOOK=dict(
        STORE_EVENTS=True,
        PAYLOAD_ENCODER_CLASS=DjangoJSONEncoder,
        MODELS=["tests.User"],
        USE_CACHE=False,
    )
)
def test_creates_events_when_enabled(responses, django_capture_on_commit_callbacks):
    webhook = WebhookFactory(
        active=True, topics=[WebhookTopicFactory(name="tests.User/create")]
    )
    responses.post(webhook.url)

    with django_capture_on_commit_callbacks(execute=True):
        User.objects.create(
            name="Dani",
            email="dani@doo.com",
            join_date=date(1970, 1, 1),
            last_active=datetime(2000, 1, 1, 12, 0, 0),
        )
    assert WebhookEvent.objects.count() == 1
    event = WebhookEvent.objects.get()
    assert event.webhook == webhook

    stored = dict(event.object)
    # Identity fields are present and consistent with the row.
    assert event.occurred_at is not None
    assert stored.pop("event_id") == str(event.event_id)
    assert stored.pop("occurred_at") == event.occurred_at.isoformat()
    assert stored == {
        "topic": "tests.User/create",
        "object": TEST_USER,
        "object_type": "tests.User",
        "webhook_uuid": str(webhook.uuid),
    }
    assert event.object_type == "tests.User"
    assert event.topic == "tests.User/create"
    assert event.status == "SUCCESS"
    assert event.url == webhook.url
    assert event.occurred_at is not None
    assert event.error is None


@override_settings(
    DJANGO_WEBHOOK=dict(
        STORE_EVENTS=False,
        PAYLOAD_ENCODER_CLASS=DjangoJSONEncoder,
        MODELS=["tests.User"],
        USE_CACHE=False,
    )
)
def test_does_not_create_events_when_disabled(
    responses, django_capture_on_commit_callbacks
):
    webhook = WebhookFactory(topics=[WebhookTopicFactory(name="tests.User/create")])
    responses.post(webhook.url)

    with django_capture_on_commit_callbacks(execute=True):
        User.objects.create(
            name="Dani",
            email="dani@doo.com",
            join_date=date(1970, 1, 1),
            last_active=datetime(2000, 1, 1, 12, 0, 0),
        )
    assert WebhookEvent.objects.count() == 0


def _age(event, days):
    WebhookEvent.objects.filter(id=event.id).update(
        created=timezone.now() - timedelta(days=days)
    )


@override_settings(
    DJANGO_WEBHOOK=dict(
        STORE_EVENTS=True,
        MODELS=["tests.User"],
        USE_CACHE=False,
        EVENTS_RETENTION_DAYS=1,
    )
)
def test_clear_purges_old_succeeded_but_keeps_failed_by_default():
    # Succeeded, recent -> kept
    recent_success = WebhookEventFactory(status=states.SUCCESS)
    # Succeeded, old -> purged
    old_success = WebhookEventFactory(status=states.SUCCESS)
    _age(old_success, 5)
    # Failed, old -> retained (no FAILED_EVENTS_RETENTION_DAYS set) so an
    # unrecovered failure is never silently discarded
    old_failure = WebhookEventFactory(status=states.FAILURE)
    _age(old_failure, 5)

    clear_webhook_events.delay()

    remaining = set(WebhookEvent.objects.values_list("id", flat=True))
    assert remaining == {recent_success.id, old_failure.id}


@override_settings(
    DJANGO_WEBHOOK=dict(
        STORE_EVENTS=True,
        MODELS=["tests.User"],
        USE_CACHE=False,
        SUCCEEDED_EVENTS_RETENTION_DAYS=30,
        FAILED_EVENTS_RETENTION_DAYS=1,
    )
)
def test_clear_purges_failed_when_window_configured():
    old_failure = WebhookEventFactory(status=states.FAILURE)
    _age(old_failure, 5)
    recent_failure = WebhookEventFactory(status=states.FAILURE)

    clear_webhook_events.delay()

    remaining = set(WebhookEvent.objects.values_list("id", flat=True))
    assert remaining == {recent_failure.id}


@override_settings(
    DJANGO_WEBHOOK=dict(
        STORE_EVENTS=True,
        MODELS=["tests.User"],
        USE_CACHE=False,
        EVENTS_RETENTION_DAYS=1,
    )
)
def test_clear_reaps_abandoned_non_terminal_rows():
    # A recent PENDING row is in-flight and left alone; one older than the window
    # was abandoned by a crashed worker and is reaped into FAILURE (gaining a
    # terminal state + cleanup path), not deleted.
    recent_pending = WebhookEventFactory(status=states.PENDING)
    old_pending = WebhookEventFactory(status=states.PENDING)
    _age(old_pending, 5)

    clear_webhook_events.delay()

    recent_pending.refresh_from_db()
    old_pending.refresh_from_db()
    assert recent_pending.status == states.PENDING
    assert old_pending.status == states.FAILURE
    assert old_pending.error is not None and "abandoned" in old_pending.error
