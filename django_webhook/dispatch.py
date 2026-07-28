"""
Event dispatch.

This is the single seam through which every webhook event is produced, whether
it originates from a model signal or from the public emission API. It guarantees
the package's core invariants:

* **Transactional integrity.** Deliveries are enqueued only after the
  originating transaction commits, and never if it rolls back. Configurable via
  ``DJANGO_WEBHOOK["DISPATCH_ON_COMMIT"]`` (default on). With no transaction
  open, dispatch happens immediately.
* **The writer is never harmed.** Enqueueing runs after commit and is fully
  isolated: a broker or database error degrades to a recorded delivery failure
  instead of propagating into the caller's ``save()``. A payload that cannot be
  produced is recorded ``INVALID`` and never sent — or, under
  ``STRICT_PAYLOAD``, raises at emission so the bug is fixed rather than shipped.
* **Event identity.** Every event carries a unique ``event_id`` and an
  ``occurred_at`` captured at emission, both stable across retries.
* **Direct dispatch.** Emission builds and enqueues deliveries directly. It
  never re-broadcasts ``post_save``/``post_delete``, so unrelated receivers are
  not re-triggered.
"""

import json
import logging
import uuid
from datetime import timedelta

from celery import states
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.utils import timezone

from . import tasks
from .models import INVALID, Webhook, WebhookEvent
from .serializers import (
    PayloadError,
    check_encodable,
    encode_payload,
    serialize_instance,
)
from .settings import get_settings
from .util import cache

logger = logging.getLogger(__name__)

CREATE = "create"
UPDATE = "update"
DELETE = "delete"


# --------------------------------------------------------------------------- #
# Public emission API
# --------------------------------------------------------------------------- #
def emit_event(instance, operation, *, occurred_at=None):
    """
    Emit a webhook event for a single ``instance`` and ``operation`` (one of
    ``"create"``, ``"update"``, ``"delete"``).

    Dispatches directly — it does not re-send model signals — and is safe to
    call inside a transaction: the delivery fires on commit.
    """
    emit_events([instance], operation, occurred_at=occurred_at)


def emit_events(instances, operation, *, occurred_at=None):
    """
    Emit a webhook event for each instance in ``instances`` (all for the same
    ``operation``). Instances may span models. See :func:`emit_event`.

    This is the supported entry point for set-based writes — ``QuerySet.update``,
    ``bulk_create``, ``bulk_update`` — which produce no model signals.
    """
    settings = get_settings()
    if occurred_at is None:
        occurred_at = timezone.now()

    for instance in instances:
        model_label = instance._meta.label
        topic = f"{model_label}/{operation}"
        pk = getattr(instance, "pk", None)
        # Defer to the commit of the database the instance was written to, so a
        # write to a secondary database dispatches on that database's commit.
        state = instance._state  # pylint: disable=protected-access
        using = state.db or DEFAULT_DB_ALIAS
        # Serialize *now*, in the writer's call stack, so the snapshot reflects
        # the state at the moment of change, and so a payload bug is attributable
        # to the instance that caused it. Also correct on delete, where
        # ``instance.pk`` is cleared before the on-commit callback would run.
        data, payload_error = serialize_instance(instance, model_label)
        if payload_error is None:
            payload_error = check_encodable(data, settings["PAYLOAD_ENCODER_CLASS"])
        _schedule(
            topic, model_label, pk, data, occurred_at, payload_error, settings, using
        )


# --------------------------------------------------------------------------- #
# Scheduling: on-commit deferral + opt-in coalescing
# --------------------------------------------------------------------------- #
def _schedule(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    topic,
    model_label,
    pk,
    data,
    occurred_at,
    payload_error,
    settings,
    using=DEFAULT_DB_ALIAS,
):
    def run():
        _deliver(topic, model_label, pk, data, occurred_at, payload_error, settings)

    if not settings["DISPATCH_ON_COMMIT"]:
        run()
        return

    connection = connections[using]
    if settings["COALESCE_EVENTS"] and connection.in_atomic_block:
        # Collapse repeated emissions of the same (topic, object) within this
        # commit window into a single delivery. Only meaningful inside a
        # transaction; in autocommit each emission is its own window.
        key = (topic, model_label, pk)
        _coalesce_buffer(connection, using)[key] = run
        return

    transaction.on_commit(run, using=using)


def _coalesce_buffer(connection, using):
    # A per-transaction dict of {coalesce_key: delivery} flushed by a single
    # on_commit callback. Reuse the current buffer only while *our* flush is
    # still pending in this transaction. Testing ``run_on_commit`` truthiness is
    # not enough: a rolled-back transaction discards our flush, yet an unrelated
    # on_commit callback can keep the list non-empty — which would let us reuse a
    # stale buffer and never register a flush, silently dropping deliveries. So
    # look for our flush by identity instead.
    # pylint: disable=protected-access
    state = getattr(connection, "_django_webhook_coalesce", None)
    if state is not None and any(
        entry[1] is state["flush"] for entry in connection.run_on_commit
    ):
        return state["buffer"]

    buffer: dict = {}

    def flush():
        connection._django_webhook_coalesce = None
        for deliver in list(buffer.values()):
            deliver()

    connection._django_webhook_coalesce = {"buffer": buffer, "flush": flush}
    transaction.on_commit(flush, using=using)
    return buffer


# --------------------------------------------------------------------------- #
# Delivery production (runs after commit; fully isolated)
# --------------------------------------------------------------------------- #
def _deliver(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    topic, model_label, pk, data, occurred_at, payload_error, settings
):
    """
    Build and enqueue a delivery for every matching subscription. Each
    subscription is isolated: a failure to produce or enqueue one delivery is
    recorded and never raised. ``data`` is the already-serialized snapshot
    captured at emission.
    """
    try:
        webhook_ids = find_webhooks(topic)
    except Exception:  # pylint: disable=broad-except
        # A subscriber lookup failure must not surface to the writer.
        logger.exception("Webhook subscriber lookup failed for topic %s", topic)
        return

    encoder_cls = settings["PAYLOAD_ENCODER_CLASS"]

    for webhook_id, webhook_uuid, webhook_url in webhook_ids:
        _deliver_one(
            webhook_id=webhook_id,
            webhook_uuid=webhook_uuid,
            webhook_url=webhook_url,
            topic=topic,
            model_label=model_label,
            pk=pk,
            data=data,
            occurred_at=occurred_at,
            payload_error=payload_error,
            encoder_cls=encoder_cls,
            settings=settings,
        )


def _deliver_one(  # pylint: disable=too-many-arguments,too-many-locals
    *,
    webhook_id,
    webhook_uuid,
    webhook_url,
    topic,
    model_label,
    pk,
    data,
    occurred_at,
    payload_error,
    encoder_cls,
    settings,
):
    # pylint: disable=broad-except
    store_events = settings["STORE_EVENTS"]
    if payload_error is not None:
        _record_invalid(
            webhook_id, webhook_url, topic, model_label, pk, occurred_at, payload_error
        )
        return

    event = None
    try:
        event_id = uuid.uuid4()
        envelope = dict(
            event_id=str(event_id),
            occurred_at=occurred_at.isoformat() if occurred_at else None,
            object=data,
            topic=topic,
            object_type=model_label,
            webhook_uuid=str(webhook_uuid),
        )
        try:
            payload, encode_error = encode_payload(envelope, encoder_cls)
        except PayloadError as ex:
            encode_error = str(ex)
            payload = None
        if encode_error is not None:
            _record_invalid(
                webhook_id,
                webhook_url,
                topic,
                model_label,
                pk,
                occurred_at,
                encode_error,
            )
            return

        if store_events:
            # Store the encoded-then-parsed envelope (plain JSON primitives) so
            # the WebhookEvent.object JSONField's own encoder never re-encounters
            # a value the payload encoder had to handle specially.
            event = WebhookEvent.objects.create(
                webhook_id=webhook_id,
                event_id=event_id,
                object=json.loads(payload),
                object_type=model_label,
                object_pk=None if pk is None else str(pk),
                status=states.PENDING,
                occurred_at=occurred_at,
                url=webhook_url or "",
                topic=topic,
            )

        # Referenced through the module (not a bound name) so tests and the
        # capture helper can swap out ``tasks.fire_webhook`` at call time.
        tasks.fire_webhook.delay(
            webhook_id,
            payload,
            topic=topic,
            object_type=model_label,
            webhook_event_id=event.id if event is not None else None,
        )
    except Exception as ex:
        # The delivery could not even be enqueued (e.g. broker outage). Mark the
        # already-created row failed (never a stranded PENDING) or, if the row
        # itself could not be created, record a standalone failure — rather than
        # raising into the writer.
        logger.exception(
            "Failed to enqueue webhook delivery for webhook_id=%s topic=%s",
            webhook_id,
            topic,
        )
        if event is not None:
            # Only claim an enqueue failure if the row is still PENDING. Under
            # CELERY_TASK_ALWAYS_EAGER the task runs inline inside .delay(), so a
            # delivery error surfaces here having already recorded its own
            # terminal status — don't clobber that with a misleading message.
            WebhookEvent.objects.filter(id=event.id, status=states.PENDING).update(
                status=states.FAILURE, error=f"enqueue error: {ex!r}"
            )
        else:
            _record_enqueue_failure(webhook_id, topic, model_label, occurred_at, ex)


def _record_invalid(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    webhook_id, webhook_url, topic, model_label, pk, occurred_at, error
):
    """
    Record an event whose payload could not be produced, and send nothing. No
    payload is stored: a placeholder would be indistinguishable from real data
    at the subscriber if it were ever re-sent.
    """
    # pylint: disable=broad-except
    logger.error(
        "Webhook payload could not be produced for %s pk=%s topic=%s: %s",
        model_label,
        pk,
        topic,
        error,
    )
    if not get_settings()["STORE_EVENTS"]:
        return
    try:
        WebhookEvent.objects.create(
            webhook_id=webhook_id,
            object={},
            object_type=model_label,
            object_pk=None if pk is None else str(pk),
            status=INVALID,
            occurred_at=occurred_at,
            url=webhook_url or "",
            topic=topic,
            error=error,
        )
    except Exception:
        logger.exception("Failed to record invalid webhook payload")


def _record_enqueue_failure(webhook_id, topic, model_label, occurred_at, ex):
    # pylint: disable=broad-except
    settings = get_settings()
    if not settings["STORE_EVENTS"]:
        return
    try:
        url = (
            Webhook.objects.filter(id=webhook_id).values_list("url", flat=True).first()
        )
        WebhookEvent.objects.create(
            webhook_id=webhook_id,
            object={},
            object_type=model_label,
            status=states.FAILURE,
            occurred_at=occurred_at,
            url=url or "",
            topic=topic,
            error=f"enqueue error: {ex!r}",
        )
    except Exception:
        logger.exception("Failed to record webhook enqueue failure")


# --------------------------------------------------------------------------- #
# Subscription lookup (public)
# --------------------------------------------------------------------------- #
def find_webhooks(topic: str):
    """
    Return ``(id, uuid)`` pairs for every active subscription to ``topic``.

    Cached for ``DJANGO_WEBHOOK["CACHE_TIMEOUT"]`` seconds unless
    ``DJANGO_WEBHOOK["USE_CACHE"]`` is off, so a burst of writes does not hammer
    the database.
    """
    if get_settings()["USE_CACHE"]:
        return _query_webhooks_cached(topic)
    return _query_webhooks(topic)


# No invalidation: a webhook/topic change is picked up only after CACHE_TIMEOUT.
@cache(ttl=lambda: timedelta(seconds=get_settings()["CACHE_TIMEOUT"]))
def _query_webhooks_cached(topic: str):
    return _query_webhooks(topic)


def _query_webhooks(topic: str):
    return list(
        Webhook.objects.filter(active=True, topics__name=topic).values_list(
            "id", "uuid", "url"
        )
    )
