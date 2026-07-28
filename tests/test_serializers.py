"""
Serializer behaviour: complete snapshots, reachable m2m, pluggable per-model /
global serializers, and payload failures that never produce a deliverable.
"""

import pytest
from django.core.serializers.json import DjangoJSONEncoder

from django_webhook.serializers import (
    PayloadError,
    check_encodable,
    default_serialize,
    encode_payload,
    get_serializer,
    serialize_instance,
)
from tests.models import Article, Tag

pytestmark = pytest.mark.django_db


def test_default_serialize_includes_auto_now_fields():
    # auto_now / auto_now_add fields are editable=False and are dropped by
    # model_to_dict; the default serializer must keep them.
    article = Article.objects.create(title="Hello")
    data = default_serialize(article)
    assert set(data) == {"id", "title", "tags", "created", "updated"}
    assert data["created"] is not None
    assert data["updated"] is not None


def test_default_serialize_includes_m2m_as_pks():
    # Many-to-many serialization is reachable and correct.
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


def test_serializer_failure_produces_no_payload(settings):
    settings.DJANGO_WEBHOOK = dict(
        MODELS=["tests.Article"],
        SERIALIZER_CLASS="tests.test_serializers._raising_serializer",
        STRICT_PAYLOAD=False,
    )
    article = Article.objects.create(title="x")
    data, error = serialize_instance(article, "tests.Article")
    assert data is None
    assert "serializer error" in error


def test_serializer_failure_raises_when_strict(settings):
    settings.DJANGO_WEBHOOK = dict(
        MODELS=["tests.Article"],
        SERIALIZER_CLASS="tests.test_serializers._raising_serializer",
        STRICT_PAYLOAD=True,
    )
    article = Article.objects.create(title="x")
    with pytest.raises(PayloadError):
        serialize_instance(article, "tests.Article")


def test_strict_payload_defaults_to_debug(settings):
    settings.DEBUG = True
    settings.DJANGO_WEBHOOK = dict(
        MODELS=["tests.Article"],
        SERIALIZER_CLASS="tests.test_serializers._raising_serializer",
    )
    article = Article.objects.create(title="x")
    with pytest.raises(PayloadError):
        serialize_instance(article, "tests.Article")


class _Unencodable:  # pylint: disable=too-few-public-methods
    def __repr__(self):
        return "<unencodable>"


def test_check_encodable_reports_unencodable_values(settings):
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.Article"], STRICT_PAYLOAD=False)
    error = check_encodable({"weird": _Unencodable()}, DjangoJSONEncoder)
    assert error is not None and "encoding error" in error


def test_check_encodable_passes_clean_data(settings):
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.Article"], STRICT_PAYLOAD=False)
    assert check_encodable({"title": "ok"}, DjangoJSONEncoder) is None


def test_check_encodable_raises_when_strict(settings):
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.Article"], STRICT_PAYLOAD=True)
    with pytest.raises(PayloadError):
        check_encodable({"weird": _Unencodable()}, DjangoJSONEncoder)


def test_encode_payload_reports_failure_without_a_fallback(settings):
    settings.DJANGO_WEBHOOK = dict(MODELS=["tests.Article"], STRICT_PAYLOAD=False)
    payload, error = encode_payload(
        {"object": {"weird": _Unencodable()}}, DjangoJSONEncoder
    )
    assert payload is None
    assert error is not None and "encoding error" in error
