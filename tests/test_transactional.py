"""
Transactional integrity: dispatch on commit only (A1) and the writer is never
harmed by event production (A2).
"""

import pytest
from celery import states
from django.db import transaction

from django_webhook.models import WebhookEvent
from django_webhook.test_factories import WebhookFactory, WebhookTopicFactory
from tests.model_data import TEST_JOIN_DATE, TEST_LAST_ACTIVE
from tests.models import User


def _make_webhook(responses):
    webhook = WebhookFactory(
        topics=[WebhookTopicFactory(name="tests.User/create")],
    )
    responses.post(webhook.url)
    return webhook


def _create_user():
    return User.objects.create(
        name="Dani",
        email="dani@doo.com",
        join_date=TEST_JOIN_DATE,
        last_active=TEST_LAST_ACTIVE,
    )


@pytest.mark.django_db(transaction=True)
def test_no_dispatch_on_rollback(responses):
    _make_webhook(responses)

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with transaction.atomic():
            _create_user()
            raise Boom()

    # The transaction rolled back, so nothing may be announced (spec A1/#2).
    assert len(responses.calls) == 0
    assert WebhookEvent.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_dispatch_after_commit(responses):
    _make_webhook(responses)
    with transaction.atomic():
        _create_user()
        # Still inside the transaction: not yet delivered.
        assert len(responses.calls) == 0
    # Committed: now delivered.
    assert len(responses.calls) == 1


@pytest.mark.django_db
def test_enqueue_failure_is_recorded_not_raised(
    mocker, responses, django_capture_on_commit_callbacks
):
    # A broker outage at enqueue time must degrade to a recorded, recoverable
    # failure — never an exception into the caller's save() (spec A2).
    _make_webhook(responses)
    mocker.patch(
        "django_webhook.tasks.fire_webhook.delay",
        side_effect=RuntimeError("broker down"),
    )

    with django_capture_on_commit_callbacks(execute=True):
        user = _create_user()  # must not raise

    assert User.objects.filter(id=user.id).exists()
    event = WebhookEvent.objects.get()
    assert event.status == states.FAILURE
    assert event.error is not None and "enqueue error" in event.error


@pytest.mark.django_db
def test_serializer_failure_does_not_break_write(
    settings, responses, django_capture_on_commit_callbacks
):
    # An unserializable value degrades to a delivered-but-flagged event; the
    # write completes normally (spec A2 / principle #3).
    settings.DJANGO_WEBHOOK = dict(
        MODELS=["tests.User"],
        USE_CACHE=False,
        SERIALIZER_CLASS="tests.test_serializers._raising_serializer",
    )
    webhook = WebhookFactory(topics=[WebhookTopicFactory(name="tests.User/create")])
    responses.post(webhook.url)

    with django_capture_on_commit_callbacks(execute=True):
        user = _create_user()  # must not raise

    assert User.objects.filter(id=user.id).exists()
    assert len(responses.calls) == 1  # still delivered (degraded payload)
    event = WebhookEvent.objects.get()
    assert event.error is not None and "serializer error" in event.error
