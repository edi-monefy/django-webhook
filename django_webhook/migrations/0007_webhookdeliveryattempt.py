import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("django_webhook", "0006_unique_event_ids"),
    ]

    operations = [
        migrations.CreateModel(
            name="WebhookDeliveryAttempt",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "event",
                    models.ForeignKey(
                        editable=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attempts",
                        to="django_webhook.webhookevent",
                    ),
                ),
                ("attempt_number", models.PositiveIntegerField(editable=False)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "PENDING"),
                            ("FAILURE", "FAILURE"),
                            ("SUCCESS", "SUCCESS"),
                        ],
                        editable=False,
                        max_length=40,
                    ),
                ),
                ("error", models.TextField(blank=True, editable=False, null=True)),
                ("attempted_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["attempt_number"],
            },
        ),
    ]
