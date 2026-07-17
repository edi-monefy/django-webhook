# Configuration & extension points

All configuration lives in a single `DJANGO_WEBHOOK` dict in your settings. Every value below is
optional and has a safe default.

```python
DJANGO_WEBHOOK = dict(
    # Models to send webhooks for (required).
    MODELS=["core.Product", "users.User"],

    # --- Transactional integrity ---
    # Enqueue deliveries only after the surrounding transaction commits. On by
    # default; turn off only if your project never wraps writes in transactions.
    DISPATCH_ON_COMMIT=True,

    # --- Delivery ---
    # Outbound request timeout, in seconds. Never infinite.
    REQUEST_TIMEOUT=10,
    # Maximum delivery retries per event.
    MAX_RETRIES=5,

    # --- Audit log & retention ---
    STORE_EVENTS=True,
    # Legacy setting; used as the default retention window for *succeeded* deliveries.
    EVENTS_RETENTION_DAYS=30,
    # Optional explicit windows. Failed deliveries are retained forever unless a
    # window is set, so an unrecovered failure is never silently discarded.
    SUCCEEDED_EVENTS_RETENTION_DAYS=None,
    FAILED_EVENTS_RETENTION_DAYS=None,

    # --- Payload ---
    # Global JSON encoder (dotted path or class).
    PAYLOAD_ENCODER_CLASS="django.core.serializers.json.DjangoJSONEncoder",
    # Global serializer override (dotted path to a callable(instance) -> dict).
    SERIALIZER_CLASS=None,
    # Per-model serializer overrides.
    MODEL_SERIALIZERS={"core.Product": "core.webhooks.serialize_product"},

    # --- Admin ---
    # Dotted path to the AdminSite to register on, or None / "none" to skip.
    ADMIN_SITE="django.contrib.admin.site",

    # --- Performance ---
    USE_CACHE=True,
    # Seconds the subscription lookup is cached, per process. There is no
    # invalidation, so a webhook/topic change takes effect only after this
    # window; raise it to reduce DB load, lower it to react faster.
    CACHE_TIMEOUT=60,
    # Opt-in: collapse repeated emissions of the same (subscription, topic,
    # object) within one commit window into a single delivery.
    COALESCE_EVENTS=False,
)
```

## Custom serializers

By default the payload contains every concrete field of the model — including `auto_now` /
`auto_now_add` timestamps — plus many-to-many relations as lists of primary keys. To control what an
event carries for a given model, supply a serializer: any callable taking an instance and returning a
JSON-serializable dict.

> **Many-to-many on the signal path.** m2m reflects the *persisted* relations at emission time. Django
> assigns m2m relations after `save()` (in statements that fire no signal) and cascade-deletes them
> before `delete()`, so signal-driven `create`/`delete` payloads carry an empty m2m list. A populated
> m2m only appears when you emit explicitly via `emit_events(...)` on a fully-related, saved instance.

```python
# core/webhooks.py
def serialize_product(product):
    return {
        "id": product.id,
        "name": product.name,
        "category": product.category.slug,   # derived / related data
    }
```

```python
DJANGO_WEBHOOK = dict(
    MODELS=["core.Product"],
    MODEL_SERIALIZERS={"core.Product": "core.webhooks.serialize_product"},
)
```

A serializer that raises degrades to a minimal snapshot with the error recorded on the event — it
never breaks the write.

## Emitting events yourself

Model signals only cover `save()` / `delete()`. For set-based writes — `QuerySet.update()`,
`bulk_create()`, `bulk_update()` — emit events explicitly. `emit_events` dispatches directly and
never re-broadcasts `post_save`, so unrelated receivers are not re-triggered.

```python
from django_webhook.api import emit_event, emit_events

# One instance
emit_event(product, "update")

# A set of instances (any operation)
emit_events(products, "update")
```

### Emitting for exactly the rows a set-based write affected

The write is yours; the package only emits. To emit for exactly the rows a `QuerySet.update()`
touched — without a racy read-then-write — capture the affected rows atomically in the update
itself with `RETURNING`, then hand them to `emit_events`:

```python
from django.db import connection
from django_webhook.api import emit_events

with connection.cursor() as cursor:
    cursor.execute(
        "UPDATE core_product SET is_archived = true "
        "WHERE active = false RETURNING id"
    )
    affected_ids = [row[0] for row in cursor.fetchall()]

emit_events(Product.objects.filter(id__in=affected_ids), "update")
```

The guard (`WHERE active = false`) stays in the statement, so concurrent writers cannot slip
between a select and an update. Backends without `RETURNING` can use `SELECT ... FOR UPDATE`
inside a transaction instead. django-webhook does not perform the write — it only publishes the
rows you give it.

## Re-sending failed deliveries

Recorded deliveries can be re-sent from the Django admin (select rows → *Re-send selected webhook
deliveries*) or programmatically:

```python
from django_webhook.api import resend_webhook_event, resend_webhook_events
from django_webhook.models import WebhookEvent

resend_webhook_event(event_id)
resend_webhook_events(WebhookEvent.objects.failed())
```

## Populating topics

Topics are reconciled from `MODELS` at startup, but that step no-ops when the database is
unavailable (e.g. before migrations). For a reliable, idempotent sync — in a deploy step or after
migrations — run:

```sh
./manage.py webhook_sync_topics
```

## Testing

Once dispatch is deferred to commit (the default), a naive test that asserts "no event was sent"
passes whether or not the code is correct, because deferred callbacks never run inside a test's
transaction. Use the shipped helpers instead:

```python
from django_webhook.test_utils import (
    create_webhook,
    capture_webhook_deliveries,
    run_on_commit_callbacks,
)

def test_product_webhook(db):
    create_webhook(topics=["core.Product/create"])
    with capture_webhook_deliveries() as deliveries:
        Product.objects.create(name="test")
    assert len(deliveries) == 1
```

## Public API

Everything a project legitimately needs is exported from `django_webhook.api`: `emit_event`,
`emit_events`, `find_webhooks`, `sync_topics`, `serialize_instance`, `prepare_request`,
`sign_payload`, `resend_webhook_event`, `resend_webhook_events`, the models, and `get_settings`.
Import it from application code (not from a settings module), since it pulls in Django models.
