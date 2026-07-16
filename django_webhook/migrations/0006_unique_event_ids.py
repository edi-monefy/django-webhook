import uuid

from django.db import migrations


def assign_unique_event_ids(apps, schema_editor):
    """
    ``0005`` adds ``event_id`` with a callable default, which Django evaluates
    once for the backfill — so every pre-existing row shares one identical
    ``event_id``. Assign a fresh UUID per existing row to restore the
    "unique per event" guarantee. New rows already get a per-insert default.
    """
    WebhookEvent = apps.get_model("django_webhook", "WebhookEvent")
    db = schema_editor.connection.alias
    manager = WebhookEvent.objects.using(db)

    batch = []
    for event in manager.only("id").iterator():
        event.event_id = uuid.uuid4()
        batch.append(event)
        if len(batch) >= 1000:
            manager.bulk_update(batch, ["event_id"])
            batch = []
    if batch:
        manager.bulk_update(batch, ["event_id"])


class Migration(migrations.Migration):
    dependencies = [
        ("django_webhook", "0005_webhookevent_event_id_occurred_at_error"),
    ]

    operations = [
        migrations.RunPython(assign_unique_event_ids, migrations.RunPython.noop),
    ]
