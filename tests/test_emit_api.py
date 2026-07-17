"""
Public emission API, no signal re-broadcast, and opt-in coalescing.
"""

import json

import pytest
from django.db import transaction
from django.db.models.signals import post_save
from django.test import override_settings

from django_webhook.api import emit_event, emit_events
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
    # Emitting must dispatch directly, never re-send post_save, or it
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
def test_emit_events_for_queryset_after_set_based_write(responses):
    # A consumer owns the set-based write; it captures the affected rows (here
    # via the queryset it updated) and hands them to emit_events. The package
    # only emits — it never performs the write.
    _article_webhook(responses)
    Article.objects.create(title="keep")
    Article.objects.create(title="stale")
    Article.objects.create(title="stale")

    stale = list(Article.objects.filter(title="stale"))
    Article.objects.filter(title="stale").update(title="fresh")
    affected = Article.objects.filter(id__in=[a.id for a in stale])
    emit_events(affected, "update")

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
    # collapse into a single delivery.
    _article_webhook(responses)
    article = Article.objects.create(title="one")

    with transaction.atomic():
        emit_event(article, "update")
        emit_event(article, "update")

    assert len(responses.calls) == 1


@pytest.mark.django_db(transaction=True)
@override_settings(
    DJANGO_WEBHOOK=dict(MODELS=["tests.Article"], USE_CACHE=False, COALESCE_EVENTS=True)
)
def test_coalescing_recovers_after_rollback(responses):
    # After a rolled-back coalesce window, a later transaction whose run_on_commit
    # is non-empty (from an unrelated callback) must still register a fresh flush
    # and deliver — not silently reuse the discarded buffer.
    _article_webhook(responses)
    article = Article.objects.create(title="one")

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with transaction.atomic():
            emit_event(article, "update")
            raise Boom()
    assert len(responses.calls) == 0

    with transaction.atomic():
        transaction.on_commit(lambda: None)  # unrelated: run_on_commit truthy
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


@pytest.mark.django_db(databases=["default", "secondary"], transaction=True)
@override_settings(DJANGO_WEBHOOK=dict(MODELS=["tests.Article"], USE_CACHE=False))
def test_dispatch_defers_to_the_writers_database(responses):
    # A write to a secondary database must dispatch on *that* database's commit,
    # not the default connection's.
    _article_webhook(responses)  # subscription lives on default
    article = Article(title="secondary")

    with transaction.atomic(using="secondary"):
        article.save(using="secondary")
        emit_event(article, "update")
        # Inside the secondary transaction the delivery has not fired yet.
        assert len(responses.calls) == 0
    # Committing the secondary transaction fires it.
    assert len(responses.calls) == 1
