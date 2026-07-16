"""
Serializer behaviour: complete snapshots (C1), reachable m2m (C4), pluggable
per-model / global serializers (C3), and safe encoder degradation (C5 / A2).
"""

import json

import pytest
from django.core.serializers.json import DjangoJSONEncoder

from django_webhook.serializers import (
    SafeFallbackEncoder,
    default_serialize,
    encode_payload,
    get_serializer,
    serialize_instance,
)
from tests.models import Article, Tag

pytestmark = pytest.mark.django_db


def test_default_serialize_includes_auto_now_fields():
    # auto_now / auto_now_add fields are editable=False and are dropped by
    # model_to_dict; the default serializer must keep them (spec C1).
    article = Article.objects.create(title="Hello")
    data = default_serialize(article)
    assert set(data) == {"id", "title", "tags", "created", "updated"}
    assert data["created"] is not None
    assert data["updated"] is not None


def test_default_serialize_includes_m2m_as_pks():
    # Many-to-many serialization is reachable and correct (spec C4).
    article = Article.objects.create(title="Hello")
    t1 = Tag.objects.create(name="a")
    t2 = Tag.objects.create(name="b")
    article.tags.add(t1, t2)

    data = default_serialize(article)
    assert sorted(data["tags"]) == sorted([t1.pk, t2.pk])


def test_default_serialize_m2m_empty_for_unsaved_instance():
    data = default_serialize(Article(title="x"))
    assert data["tags"] == []


def _only_title(instance):
    return {"title": instance.title}


def test_per_model_serializer_override(settings):
    settings.DJANGO_WEBHOOK = dict(
        MODELS=["tests.Article"],
        MODEL_SERIALIZERS={"tests.Article": "tests.test_serializers._only_title"},
    )
    assert get_serializer("tests.Article") is _only_title
    article = Article.objects.create(title="Only me")
    data, error = serialize_instance(article, "tests.Article")
    assert data == {"title": "Only me"}
    assert error is None


def test_global_serializer_used_when_no_per_model(settings):
    settings.DJANGO_WEBHOOK = dict(
        MODELS=["tests.Article"],
        SERIALIZER_CLASS="tests.test_serializers._only_title",
    )
    assert get_serializer("tests.Article") is _only_title


def _raising_serializer(instance):
    raise ValueError("boom")


def test_serializer_failure_degrades_to_minimal_snapshot(settings):
    # A serializer that raises must not lose the delivery: it degrades to a
    # minimal snapshot with a recorded error (spec A2 / principle #3).
    settings.DJANGO_WEBHOOK = dict(
        MODELS=["tests.Article"],
        SERIALIZER_CLASS="tests.test_serializers._raising_serializer",
    )
    article = Article.objects.create(title="x")
    data, error = serialize_instance(article, "tests.Article")
    assert data == {"pk": article.pk}
    assert "serializer error" in error


class _Unencodable:  # pylint: disable=too-few-public-methods
    def __repr__(self):
        return "<unencodable>"


def test_encode_payload_falls_back_without_omitting_values():
    # A value the configured encoder cannot represent is coerced to a visible
    # repr rather than silently dropped, and the failure is reported (spec C5).
    envelope = {"object": {"weird": _Unencodable()}}
    payload, error = encode_payload(envelope, DjangoJSONEncoder)
    assert error is not None and "encoding error" in error
    assert json.loads(payload)["object"]["weird"] == "<unencodable>"


def test_safe_fallback_encoder_never_raises():
    assert (
        json.loads(json.dumps({"x": _Unencodable()}, cls=SafeFallbackEncoder))["x"]
        == "<unencodable>"
    )
