import json
import logging
import uuid

from celery import states
from django.core import validators
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models.fields import DateTimeField
from django.db.utils import OperationalError, ProgrammingError

from django_webhook.settings import get_settings

from .validators import validate_topic_model

topic_regex = r"\w+\.\w+\/[create|update|delete]"

STATES = [
    (states.PENDING, states.PENDING),
    (states.FAILURE, states.FAILURE),
    (states.SUCCESS, states.SUCCESS),
]


class Webhook(models.Model):
    url = models.URLField()
    topics = models.ManyToManyField(
        "django_webhook.WebhookTopic",
        related_name="webhooks",
        related_query_name="webhook",
    )
    active = models.BooleanField(default=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False)
    created = DateTimeField(auto_now_add=True)
    modified = DateTimeField(auto_now=True)

    def __str__(self):
        return f"id={self.id} active={self.active}"


class WebhookTopic(models.Model):  # type: ignore
    name = models.CharField(
        max_length=250,
        unique=True,
        validators=[
            validators.RegexValidator(
                topic_regex, message="Topic must match: " + topic_regex
            ),
            validate_topic_model,
        ],
    )

    def __str__(self):
        return self.name


class WebhookSecret(models.Model):
    webhook = models.ForeignKey(
        Webhook,
        on_delete=models.CASCADE,
        related_name="secrets",
        related_query_name="secret",
        editable=False,
    )
    token = models.CharField(
        max_length=100,
        validators=[validators.MinLengthValidator(12)],
    )
    created = DateTimeField(auto_now_add=True)


class WebhookEventQuerySet(models.QuerySet):
    def failed(self):
        return self.filter(status=states.FAILURE)

    def succeeded(self):
        return self.filter(status=states.SUCCESS)

    def resend(self):
        """
        Re-enqueue every delivery in this queryset with a single status update
        (plus one task per event). Rows whose subscription was deleted are
        skipped. Returns the number re-fired.
        """
        resendable = list(self.exclude(webhook_id__isnull=True))
        if not resendable:
            return 0
        WebhookEvent.objects.filter(id__in=[e.id for e in resendable]).update(
            status=states.PENDING, error=None
        )
        for event in resendable:
            event._enqueue_delivery()  # type: ignore[attr-defined]  # pylint: disable=protected-access
        return len(resendable)


class WebhookEvent(models.Model):
    webhook = models.ForeignKey(
        Webhook,
        on_delete=models.SET_NULL,
        null=True,
        editable=False,
        related_name="events",
        related_query_name="event",
    )
    # Unique per-event identifier carried in the envelope. Distinct from the
    # subscription's ``Webhook.uuid`` and stable across retries of this event.
    event_id = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    object = models.JSONField(
        max_length=1000,
        encoder=DjangoJSONEncoder,
        editable=False,
    )
    object_type = models.CharField(max_length=50, null=True, editable=False)
    status = models.CharField(
        max_length=40,
        default=states.PENDING,
        choices=STATES,
        editable=False,
    )
    created = DateTimeField(auto_now_add=True)
    # The time the change occurred, set at emission (not at send). Stable across
    # retries so subscribers can order and dedup redeliveries.
    occurred_at = DateTimeField(null=True, editable=False)
    url = models.URLField(editable=False)
    topic = models.CharField(max_length=250, null=True, editable=False)
    # Captures the reason a delivery failed to be produced or confirmed.
    error = models.TextField(null=True, blank=True, editable=False)

    objects = WebhookEventQuerySet.as_manager()

    def __str__(self):
        return f"id={self.id} status={self.status} topic={self.topic}"

    def resend(self):
        """
        Re-enqueue this delivery for sending. Resets the row to ``PENDING`` and
        fires the delivery task against the stored payload.
        """
        if self.webhook_id is None:
            logging.warning(
                "Cannot resend WebhookEvent id=%s: its subscription was deleted",
                self.id,
            )
            return False

        WebhookEvent.objects.filter(id=self.id).update(
            status=states.PENDING, error=None
        )
        self._enqueue_delivery()
        return True

    def _enqueue_delivery(self):
        # Lazy: tasks imports models at load time, so a top-level import cycles.
        from .tasks import fire_webhook  # pylint: disable=import-outside-toplevel

        payload = json.dumps(self.object, cls=get_settings()["PAYLOAD_ENCODER_CLASS"])
        fire_webhook.delay(
            self.webhook_id,
            payload,
            topic=self.topic,
            object_type=self.object_type,
            webhook_event_id=self.id,
        )


def topics_from_settings():
    """
    The set of topic names implied by ``DJANGO_WEBHOOK["MODELS"]``:
    ``<model>/create``, ``<model>/update``, ``<model>/delete`` for each model.
    """
    # Lazy: dispatch imports models at load time, so a top-level import cycles.
    # pylint: disable=import-outside-toplevel
    from django_webhook.dispatch import CREATE, DELETE, UPDATE

    allowed_topics = set()
    for model in get_settings().get("MODELS") or []:
        allowed_topics.update(
            {f"{model}/{CREATE}", f"{model}/{UPDATE}", f"{model}/{DELETE}"}
        )
    return allowed_topics


def sync_topics(*, prune=True):
    """
    Idempotently reconcile the ``WebhookTopic`` table with the configured
    models: create any missing topics and (optionally) delete topics no longer
    implied by settings. Safe to run at any point in the project lifecycle —
    e.g. from the ``webhook_sync_topics`` management command.

    Returns ``(created, deleted)`` counts.
    """
    allowed_topics = topics_from_settings()
    if not allowed_topics:
        return (0, 0)

    deleted = 0
    if prune:
        stale = WebhookTopic.objects.exclude(name__in=allowed_topics)
        deleted = stale.count()
        if deleted:
            logging.info("Pruning %s stale WebhookTopics", deleted)
            stale.delete()

    existing = set(
        WebhookTopic.objects.filter(name__in=allowed_topics).values_list(
            "name", flat=True
        )
    )
    to_create = allowed_topics - existing
    if to_create:
        # ignore_conflicts guards against a concurrent sync (e.g. two workers
        # booting at once) racing on the unique name constraint.
        WebhookTopic.objects.bulk_create(
            [WebhookTopic(name=name) for name in sorted(to_create)],
            ignore_conflicts=True,
        )
        logging.info("Adding topics: %s", sorted(to_create))
    return (len(to_create), deleted)


def populate_topics_from_settings():
    """
    Startup helper: reconcile topics, but silently no-op when the database is
    not yet available (e.g. before migrations, or during test DB setup). For a
    reliable sync use :func:`sync_topics` / the ``webhook_sync_topics`` command.
    """
    try:
        Webhook.objects.count()
    except (OperationalError, ProgrammingError) as ex:
        if "Connection refused" in ex.args[0]:
            return
        if "could not translate host name" in ex.args[0]:
            return
        if "no such table" in ex.args[0]:
            return
        if "relation" in ex.args[0] and "does not exist" in ex.args[0]:
            return
        raise ex

    sync_topics()
