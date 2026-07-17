"""
Test support.

On-commit dispatch makes naive assertions silently vacuous: a deferred callback
never runs inside a test's transaction, so a test that asserts "no event was
sent" passes whether or not the code is correct. This module ships the correct
primitives so each project does not have to rediscover the trap:

* :func:`create_webhook` — construct a subscription (webhook, topics, secret).
* :func:`capture_webhook_deliveries` — run the on-commit callbacks produced in
  the block and collect the deliveries that would be enqueued, so assertions are
  made against real dispatch rather than a vacuous no-op.
* :func:`run_on_commit_callbacks` — execute pending on-commit callbacks so eager
  Celery tasks actually deliver synchronously within a test.
"""

from contextlib import contextmanager

from django.db import DEFAULT_DB_ALIAS, connections

from . import tasks as tasks_module
from .models import Webhook, WebhookSecret, WebhookTopic


def create_webhook(
    url="https://example.com/webhook",
    topics=(),
    secret="test-secret-token",
    active=True,
):
    """
    Create and return a :class:`~django_webhook.models.Webhook` wired up with
    the given ``topics`` (topic names, created if missing) and an optional
    ``secret``.
    """
    webhook = Webhook.objects.create(url=url, active=active)
    for name in topics:
        topic, _ = WebhookTopic.objects.get_or_create(name=name)
        webhook.topics.add(topic)
    if secret:
        WebhookSecret.objects.create(webhook=webhook, token=secret)
    return webhook


def run_on_commit_callbacks(using=DEFAULT_DB_ALIAS):
    """
    Execute (and drain) the connection's pending ``on_commit`` callbacks. Use
    this after a write when you need the deferred webhook dispatch to actually
    happen inside a test transaction.
    """
    connection = connections[using]
    while connection.run_on_commit:
        callbacks = connection.run_on_commit
        connection.run_on_commit = []
        for entry in callbacks:
            func = entry[1]  # (sids, func) or (sids, func, robust)
            func()


@contextmanager
def capture_webhook_deliveries(using=DEFAULT_DB_ALIAS, execute_on_commit=True):
    """
    Context manager yielding a list that is populated with the deliveries
    dispatched within the block. Each entry is a dict with ``webhook_id``,
    ``payload`` and any keyword arguments (``topic``, ``object_type``,
    ``webhook_event_id``).

    With ``execute_on_commit`` (the default), deferred on-commit callbacks are
    run on exit so the capture reflects real dispatch. The underlying HTTP send
    is stubbed out — this asserts *what would be delivered*, not delivery.
    """
    captured = []
    original = tasks_module.fire_webhook

    class _Recorder:  # pylint: disable=too-few-public-methods
        def delay(self, webhook_id, payload, **kwargs):
            captured.append(dict(webhook_id=webhook_id, payload=payload, **kwargs))

    tasks_module.fire_webhook = _Recorder()
    try:
        yield captured
        if execute_on_commit:
            run_on_commit_callbacks(using)
    finally:
        tasks_module.fire_webhook = original
