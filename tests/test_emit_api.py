"""
Public emission API (D1), no signal re-broadcast (D2), atomic set-based write
helper (D3) and opt-in coalescing (D4).
"""

import json

import pytest
from django.db import transaction
from django.db.models.signals import post_save
from django.test import override_settings

from django_webhook.api import emit_event, emit_events, update_and_emit
from django_webhook.test_factories import WebhookFactory, WebhookTopicFactory
from tests.models import Article

pytestmark = pytest.mark.django_db


def _article_webhook(responses, operation="update"):
    webhook = WebhookFactory(
        topics=[WebhookTopicFactory(name=f"tests.Article/{operation}")],
    )
    responses.post(webhook.url)
    return webhook


@override_settings(DJANGO_WEBHOOK=dict(MODELS=["tests.Article"], USE_CACHE=False))
def test_emit_events_for_set(responses, django_capture_on_commit_callbacks):
    # A set-based write path (no signals) can still emit for each affected row.
    _article_webhook(responses)
    a1 = Article.objects.create(title="one")
    a2 = Article.objects.create(title="two")

    with django_capture_on_commit_callbacks(execute=True):
        emit_events([a1, a2], "update")

    assert len(responses.calls) == 2
    titles = {json.loads(c.request.body)["object"]["title"] for c in responses.calls}
    assert titles == {"one", "two"}


@override_settings(DJANGO_WEBHOOK=dict(MODELS=["tests.Article"], USE_CACHE=False))
def test_emit_does_not_rebroadcast_post_save(
    responses, django_capture_on_commit_callbacks
):
    # Emitting must dispatch directly, never re-send post_save (spec D2), or it
    # would re-trigger every unrelated receiver on the model.
    _article_webhook(responses)
    article = Article.objects.create(title="one")

    received = []

    def spy(sender, **kwargs):  # pylint: disable=unused-argument
        received.append(kwargs.get("instance"))

    post_save.connect(spy, sender=Article, dispatch_uid="d2-spy")
    try:
        with django_capture_on_commit_callbacks(execute=True):
            emit_event(article, "update")
    finally:
        post_save.disconnect(sender=Article, dispatch_uid="d2-spy")

    assert not received  # emission did not fire post_save


@pytest.mark.django_db(transaction=True)
@override_settings(DJANGO_WEBHOOK=dict(MODELS=["tests.Article"], USE_CACHE=False))
def test_update_and_emit_captures_affected_rows(responses):
    # UPDATE ... RETURNING emits for exactly the affected rows, atomically, with
    # the post-update values (spec D3).
    _article_webhook(responses)
    Article.objects.create(title="keep")
    Article.objects.create(title="stale")
    Article.objects.create(title="stale")

    affected = update_and_emit(
        Article.objects.filter(title="stale"), operation="update", title="fresh"
    )

    assert len(affected) == 2
    assert all(a.title == "fresh" for a in affected)
    assert len(responses.calls) == 2
    assert all(
        json.loads(c.request.body)["object"]["title"] == "fresh"
        for c in responses.calls
    )


@pytest.mark.django_db(transaction=True)
@override_settings(
    DJANGO_WEBHOOK=dict(MODELS=["tests.Article"], USE_CACHE=False, COALESCE_EVENTS=True)
)
def test_coalescing_collapses_duplicates_in_commit_window(responses):
    # Two emissions for the same (topic, object) within one commit window
    # collapse into a single delivery (spec D4).
    _article_webhook(responses)
    article = Article.objects.create(title="one")

    with transaction.atomic():
        emit_event(article, "update")
        emit_event(article, "update")

    assert len(responses.calls) == 1


@pytest.mark.django_db(transaction=True)
@override_settings(
    DJANGO_WEBHOOK=dict(
        MODELS=["tests.Article"], USE_CACHE=False, COALESCE_EVENTS=False
    )
)
def test_without_coalescing_each_emission_delivers(responses):
    _article_webhook(responses)
    article = Article.objects.create(title="one")

    with transaction.atomic():
        emit_event(article, "update")
        emit_event(article, "update")

    assert len(responses.calls) == 2
