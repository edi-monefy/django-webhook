import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta

import pytest
from django.db.models.signals import post_delete, post_save
from django.test import override_settings
from django.utils import timezone
from freezegun import freeze_time
from pytest_django.asserts import assertNumQueries

from django_webhook.signals import SignalListener
from django_webhook.test_factories import (
    WebhookFactory,
    WebhookSecretFactory,
    WebhookTopicFactory,
)
from tests.model_data import TEST_JOIN_DATE, TEST_LAST_ACTIVE, TEST_USER
from tests.models import Country, User

pytestmark = pytest.mark.django_db


def split_envelope(body):
    """
    Split a delivered envelope into its stable part and its per-event identity
    part (event_id + occurred_at).
    """
    envelope = json.loads(body)
    event_id = envelope.pop("event_id")
    occurred_at = envelope.pop("occurred_at")
    return envelope, event_id, occurred_at


@freeze_time("2012-01-14 03:21:34")
def test_create(responses, django_capture_on_commit_callbacks):
    uuid_str = "54c10b6e-42e7-4edc-a047-a53c7ff80c77"
    webhook = WebhookFactory(
        topics=[WebhookTopicFactory(name="tests.User/create")],
        secrets=[],
        uuid=uuid_str,
    )
    secret = WebhookSecretFactory(webhook=webhook, token="very-secret-token")
    responses.post(webhook.url)

    with django_capture_on_commit_callbacks(execute=True):
        User.objects.create(
            name="Dani",
            email="dani@doo.com",
            join_date=TEST_JOIN_DATE,
            last_active=TEST_LAST_ACTIVE,
        )
    assert len(responses.calls) == 1
    req = responses.calls[0].request
    now = timezone.now()
    assert req.headers["Django-Webhook-Request-Timestamp"] == "1326511294"
    assert req.headers["Django-Webhook-UUID"] == str(webhook.uuid)

    envelope, event_id, occurred_at = split_envelope(req.body)
    assert envelope == {
        "topic": "tests.User/create",
        "object": TEST_USER,
        "object_type": "tests.User",
        "webhook_uuid": "54c10b6e-42e7-4edc-a047-a53c7ff80c77",
    }
    # The event id is a real per-event id, not the subscription's constant uuid.
    assert uuid.UUID(event_id)
    assert event_id != str(webhook.uuid)
    assert occurred_at == "2012-01-14T03:21:34+00:00"

    hmac_msg = f"{int(now.timestamp())}:{req.body.decode()}".encode()
    assert (
        req.headers["Django-Webhook-Signature-v1"]
        == hmac.new(
            key=secret.token.encode(), msg=hmac_msg, digestmod=hashlib.sha256
        ).hexdigest()
    )


def test_update(responses, django_capture_on_commit_callbacks):
    user = User.objects.create(
        name="Dani",
        email="dani@doo.com",
        join_date=TEST_JOIN_DATE,
        last_active=TEST_LAST_ACTIVE,
    )
    webhook = WebhookFactory(
        topics=[WebhookTopicFactory(name="tests.User/update")],
    )
    responses.post(webhook.url)

    with django_capture_on_commit_callbacks(execute=True):
        user.name = "Adin"
        user.save()
    assert len(responses.calls) == 1
    envelope, _, _ = split_envelope(responses.calls[0].request.body)
    expected_object = TEST_USER.copy()
    expected_object["name"] = "Adin"
    assert envelope == {
        "topic": "tests.User/update",
        "object": expected_object,
        "object_type": "tests.User",
        "webhook_uuid": str(webhook.uuid),
    }


def test_delete(responses, django_capture_on_commit_callbacks):
    user = User.objects.create(
        name="Dani",
        email="dani@doo.com",
        join_date=TEST_JOIN_DATE,
        last_active=TEST_LAST_ACTIVE,
    )
    webhook = WebhookFactory(
        topics=[WebhookTopicFactory(name="tests.User/delete")],
    )
    responses.post(webhook.url)

    with django_capture_on_commit_callbacks(execute=True):
        user.delete()
    assert len(responses.calls) == 1
    envelope, _, _ = split_envelope(responses.calls[0].request.body)
    assert envelope == {
        "topic": "tests.User/delete",
        "object": TEST_USER,
        "object_type": "tests.User",
        "webhook_uuid": str(webhook.uuid),
    }


def test_filters_topic_by_type(responses, django_capture_on_commit_callbacks):
    webhook = WebhookFactory(
        topics=[WebhookTopicFactory(name="tests.User/update")],
    )
    responses.post(webhook.url)
    with django_capture_on_commit_callbacks(execute=True):
        user = User.objects.create(
            name="Dani",
            email="dani@doo.com",
            join_date=TEST_JOIN_DATE,
            last_active=TEST_LAST_ACTIVE,
        )
    assert len(responses.calls) == 0
    with django_capture_on_commit_callbacks(execute=True):
        user.save()
    assert len(responses.calls) == 1


def test_multiple_topic_types(responses, django_capture_on_commit_callbacks):
    user = User.objects.create(
        name="Dani",
        email="dani@doo.com",
        join_date=TEST_JOIN_DATE,
        last_active=TEST_LAST_ACTIVE,
    )
    webhook = WebhookFactory(
        topics=[
            WebhookTopicFactory(name="tests.User/create"),
            WebhookTopicFactory(name="tests.User/update"),
            WebhookTopicFactory(name="tests.User/delete"),
        ],
    )
    responses.post(webhook.url)
    with django_capture_on_commit_callbacks(execute=True):
        user.delete()
    assert len(responses.calls) == 1
    envelope, _, _ = split_envelope(responses.calls[0].request.body)
    assert envelope["topic"] == "tests.User/delete"


def test_multiple_topic_models(responses, django_capture_on_commit_callbacks):
    User.objects.create(
        name="Dani",
        email="dani@doo.com",
        join_date=TEST_JOIN_DATE,
        last_active=TEST_LAST_ACTIVE,
    )
    country = Country.objects.create(name="Sweden")
    webhook = WebhookFactory(
        topics=[
            WebhookTopicFactory(name="tests.User/update"),
            WebhookTopicFactory(name="tests.Country/update"),
        ],
    )
    responses.post(webhook.url)
    with django_capture_on_commit_callbacks(execute=True):
        country.save()

    envelope, _, _ = split_envelope(responses.calls[0].request.body)
    assert envelope == {
        "topic": "tests.Country/update",
        "object": {"id": 1, "name": "Sweden"},
        "object_type": "tests.Country",
        "webhook_uuid": str(webhook.uuid),
    }


def test_does_not_fire_inactive_webhooks(responses, django_capture_on_commit_callbacks):
    country = Country.objects.create(name="Sweden")
    webhook = WebhookFactory(
        active=False,
        topics=[
            WebhookTopicFactory(name="tests.Country/update"),
        ],
    )
    responses.post(webhook.url)
    with django_capture_on_commit_callbacks(execute=True):
        country.save()
    assert len(responses.calls) == 0


@override_settings(
    DJANGO_WEBHOOK=dict(
        MODELS=["tests.Country"],
        USE_CACHE=True,
        # Run dispatch inline and skip event rows so the assertions below count
        # only the subscription-lookup query this test is about.
        DISPATCH_ON_COMMIT=False,
        STORE_EVENTS=False,
    )
)
def test_caches_webhook_query_calls(mocker):
    mocker.patch("django_webhook.tasks.fire_webhook")
    country = Country.objects.create(name="Yugoslavia")
    WebhookFactory(
        topics=[
            WebhookTopicFactory(name="tests.Country/update"),
        ],
    )

    now = datetime.now()
    with freeze_time(now):
        # First save call caches the query for webhooks
        country.save()
        with assertNumQueries(1):  # pylint: disable=not-context-manager
            # The second save doesn't query webhooks again, only updates Country
            country.save()

    # Move time forward and assert that the cache was busted
    with freeze_time(now + timedelta(minutes=1, seconds=1)):
        with assertNumQueries(2):  # pylint: disable=not-context-manager
            country.save()


@override_settings(
    DJANGO_WEBHOOK=dict(
        MODELS=["tests.Country"],
        USE_CACHE=True,
        DISPATCH_ON_COMMIT=False,
        STORE_EVENTS=False,
        # Raised well past the 60s default; read per call.
        CACHE_TIMEOUT=3600,
    )
)
def test_cache_timeout_is_configurable(mocker):
    mocker.patch("django_webhook.tasks.fire_webhook")
    country = Country.objects.create(name="Yugoslavia")
    WebhookFactory(topics=[WebhookTopicFactory(name="tests.Country/update")])

    now = datetime.now()
    with freeze_time(now):
        country.save()  # caches the lookup

    # Past the old 60s window but within the raised timeout -> still cached.
    with freeze_time(now + timedelta(minutes=5)):
        with assertNumQueries(1):  # pylint: disable=not-context-manager
            country.save()


def test_signal_listener_uid():
    assert (
        SignalListener(signal=post_save, signal_name="post_save", model_cls=Country).uid
        == "django_webhook_tests.Country_post_save"
    )
    assert (
        SignalListener(
            signal=post_delete, signal_name="post_delete", model_cls=Country
        ).uid
        == "django_webhook_tests.Country_post_delete"
    )
