"""
Public API surface.

Everything a consuming project legitimately needs is re-exported here and
documented, so projects never have to import private names or copy internals
that then drift on upgrade. Import from ``django_webhook.api``:

    from django_webhook.api import emit_event, emit_events, sync_topics

Note: import this module from application code (e.g. inside a view, task, or
``AppConfig.ready``), not at the top of a settings file — it pulls in Django
models and therefore needs the app registry to be ready.
"""

from .dispatch import CREATE, DELETE, UPDATE, emit_event, emit_events, find_webhooks
from .http import prepare_request, sign_payload
from .models import (
    INVALID,
    RETRYING,
    Webhook,
    WebhookEvent,
    WebhookSecret,
    WebhookTopic,
    sync_topics,
    topics_from_settings,
)
from .serializers import (
    PayloadError,
    check_encodable,
    default_serialize,
    encode_payload,
    get_serializer,
    serialize_instance,
)
from .settings import get_settings
from .tasks import fire_webhook, resend_webhook_event, resend_webhook_events

__all__ = [
    # Emission
    "emit_event",
    "emit_events",
    "CREATE",
    "UPDATE",
    "DELETE",
    # Subscriptions / topics
    "find_webhooks",
    "sync_topics",
    "topics_from_settings",
    # Serialization / signing
    "default_serialize",
    "serialize_instance",
    "get_serializer",
    "check_encodable",
    "encode_payload",
    "PayloadError",
    "prepare_request",
    "sign_payload",
    # Delivery / recovery
    "fire_webhook",
    "resend_webhook_event",
    "resend_webhook_events",
    "RETRYING",
    "INVALID",
    # Models
    "Webhook",
    "WebhookEvent",
    "WebhookSecret",
    "WebhookTopic",
    # Settings
    "get_settings",
]
