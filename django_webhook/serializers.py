"""
Payload serialization.

The serializer turns a model instance into a JSON-serializable ``dict``. Three
concerns live here:

* A complete default snapshot (spec C1): every concrete field, including
  ``editable=False`` fields such as ``auto_now`` / ``auto_now_add`` timestamps,
  which ``django.forms.model_to_dict`` silently drops.
* Reachable many-to-many serialization (spec C4): related rows are included as
  lists of primary keys rather than being filtered out before they can be
  serialized.
* A documented extension point (spec C3): projects can supply a per-model or a
  global serializer without patching the package.

Encoding (spec C5 / A2) degrades safely: if the configured encoder cannot
represent a value, the value is coerced to a visible representation and the
delivery is flagged, rather than being silently omitted or raising into the
writer's ``save()``.
"""

import json
import logging

from django.utils.module_loading import import_string

from .settings import get_settings

logger = logging.getLogger(__name__)


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
    # value_from_object returns the related instances; publish their pks. An
    # unsaved or just-deleted instance has no related rows to read.
    if instance.pk is None:
        return []
    try:
        related = field.value_from_object(instance)
    except (ValueError, field.related_model.DoesNotExist):  # pragma: no cover
        return []
    return [obj.pk for obj in related]


def get_serializer(model_label):
    """
    Resolve the serializer callable for ``model_label``.

    Resolution order (spec C3):

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
    Serialize ``instance`` using the resolved serializer, degrading to a minimal
    snapshot if the serializer raises so a delivery is never lost outright.

    Returns ``(data, error)`` where ``error`` is ``None`` on success or a short
    message describing why the full snapshot could not be produced.
    """
    if model_label is None:
        model_label = instance._meta.label
    serializer = get_serializer(model_label)
    try:
        return serializer(instance), None
    except Exception as ex:  # pylint: disable=broad-except
        logger.exception("Webhook serializer failed for %s", model_label)
        fallback = {"pk": getattr(instance, "pk", None)}
        return fallback, f"serializer error: {ex!r}"


class SafeFallbackEncoder(json.JSONEncoder):
    """
    Last-resort encoder used only after the configured encoder has failed. It
    never raises: any value it cannot represent is coerced to its ``repr`` so
    the value is surfaced rather than silently dropped (spec C5, principle #3).
    """

    def default(self, o):
        try:
            return super().default(o)
        except TypeError:
            return repr(o)


def encode_payload(envelope, encoder_cls):
    """
    Encode ``envelope`` to a JSON string.

    Returns ``(payload_str, error)``. On the happy path ``error`` is ``None``.
    If the configured encoder cannot represent a value the payload is re-encoded
    with :class:`SafeFallbackEncoder` (so no value is silently omitted) and
    ``error`` describes the failure. This keeps encoder failure from propagating
    into the caller's ``save()`` (spec A2).
    """
    try:
        return json.dumps(envelope, cls=encoder_cls), None
    except Exception as ex:  # pylint: disable=broad-except
        logger.exception("Webhook payload encoding failed; using safe fallback")
        return json.dumps(envelope, cls=SafeFallbackEncoder), f"encoding error: {ex!r}"
