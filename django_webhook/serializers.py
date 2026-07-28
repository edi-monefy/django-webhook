"""
Payload serialization.

The serializer turns a model instance into a JSON-serializable ``dict``. Three
concerns live here:

* A complete default snapshot: every concrete field, including ``editable=False``
  fields such as ``auto_now`` / ``auto_now_add`` timestamps, which
  ``django.forms.model_to_dict`` silently drops.
* Reachable many-to-many serialization: related rows are included as lists of
  primary keys rather than being filtered out before they can be serialized.
  Caveat: m2m reflects the *persisted* relations at emission time. On the signal
  path this means m2m is empty for ``create`` (relations are assigned after
  ``save()``, in statements that fire no signal) and for ``delete`` (through
  rows are already cascade-deleted), so a populated m2m only appears via the
  manual :func:`~django_webhook.dispatch.emit_events` API on a fully-related,
  persisted instance.
* A documented extension point: projects can supply a per-model or a global
  serializer without patching the package.

A payload that cannot be produced is never delivered: the event is recorded
``INVALID`` and no request is sent. Under ``STRICT_PAYLOAD`` it raises instead.
"""

import json
import logging

from django.utils.module_loading import import_string

from .settings import get_settings, strict_payload

logger = logging.getLogger(__name__)


class PayloadError(Exception):
    """Raised, under ``STRICT_PAYLOAD``, when a payload cannot be produced."""


def default_serialize(instance):
    """
    Serialize *every* concrete field of ``instance`` plus its many-to-many
    relations (as lists of primary keys).

    Unlike ``model_to_dict`` this does not drop non-editable fields, so
    ``auto_now`` / ``auto_now_add`` timestamps and other ``editable=False``
    columns are present in the payload.
    """
    data = {}
    for field in instance._meta.concrete_fields:
        data[field.name] = field.value_from_object(instance)

    for field in instance._meta.many_to_many:
        data[field.name] = _serialize_m2m(instance, field)

    return data


def _serialize_m2m(instance, field):
    # Publish related rows as a list of primary keys. Fetch only the pks (one
    # query, no full-object hydration); skip unsaved/just-deleted instances.
    if instance.pk is None:
        return []
    return list(getattr(instance, field.name).values_list("pk", flat=True))


def get_serializer(model_label):
    """
    Resolve the serializer callable for ``model_label``.

    Resolution order:

    1. A per-model override in ``DJANGO_WEBHOOK["MODEL_SERIALIZERS"]``.
    2. The global ``DJANGO_WEBHOOK["SERIALIZER_CLASS"]``.
    3. The built-in :func:`default_serialize`.

    A serializer is any callable taking a model instance and returning a
    JSON-serializable dict. Dotted-path strings are imported.
    """
    settings = get_settings()

    per_model = settings.get("MODEL_SERIALIZERS") or {}
    serializer = per_model.get(model_label)
    if serializer is None:
        serializer = settings.get("SERIALIZER_CLASS")
    if serializer is None:
        return default_serialize

    if isinstance(serializer, str):
        serializer = import_string(serializer)
    return serializer


def serialize_instance(instance, model_label=None):
    """
    Serialize ``instance`` using the resolved serializer.

    Returns ``(data, error)``. On failure ``data`` is ``None`` — no placeholder
    snapshot is produced, because a subscriber cannot distinguish one from a
    genuine event. Raises :class:`PayloadError` when ``STRICT_PAYLOAD`` is on.
    """
    if model_label is None:
        model_label = instance._meta.label
    serializer = get_serializer(model_label)
    try:
        return serializer(instance), None
    except Exception as ex:  # pylint: disable=broad-except
        logger.exception("Webhook serializer failed for %s", model_label)
        error = f"serializer error: {ex!r}"
        if strict_payload():
            raise PayloadError(f"{model_label}: {error}") from ex
        return None, error


def check_encodable(data, encoder_cls=None):
    """
    Verify ``data`` can be encoded by the configured payload encoder, returning
    ``None`` when it can or an error message when it cannot. Raises
    :class:`PayloadError` when ``STRICT_PAYLOAD`` is on.

    Called once at emission so an unencodable value is attributed to the
    instance carrying it, rather than surfacing later per-subscription.
    """
    if encoder_cls is None:
        encoder_cls = get_settings()["PAYLOAD_ENCODER_CLASS"]
    try:
        json.dumps(data, cls=encoder_cls)
        return None
    except Exception as ex:  # pylint: disable=broad-except
        logger.exception("Webhook payload encoding failed")
        error = f"encoding error: {ex!r}"
        if strict_payload():
            raise PayloadError(error) from ex
        return error


def encode_payload(envelope, encoder_cls):
    """
    Encode ``envelope`` to a JSON string.

    Returns ``(payload_str, error)``; on failure ``payload_str`` is ``None``.
    """
    try:
        return json.dumps(envelope, cls=encoder_cls), None
    except Exception as ex:  # pylint: disable=broad-except
        logger.exception("Webhook envelope encoding failed")
        error = f"encoding error: {ex!r}"
        if strict_payload():
            raise PayloadError(error) from ex
        return None, error
