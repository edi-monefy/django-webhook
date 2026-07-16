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
`bulk_create()`, `bulk_update()` — emit events explicitly with the public API. It dispatches
directly and never re-broadcasts `post_save`, so unrelated receivers are not re-triggered.

```python
from django_webhook.api import emit_event, emit_events, update_and_emit

# One instance
emit_event(product, "update")

# A set of instances (any operation)
emit_events(products, "update")

# Atomic set-based write + emit for exactly the affected rows (UPDATE ... RETURNING)
update_and_emit(Product.objects.filter(active=False), is_archived=True)
```

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
`emit_events`, `update_and_emit`, `find_webhooks`, `sync_topics`, `serialize_instance`,
`prepare_request`, `sign_payload`, `resend_webhook_event`, `resend_webhook_events`, the models, and
`get_settings`. Import it from application code (not from a settings module), since it pulls in
Django models.
