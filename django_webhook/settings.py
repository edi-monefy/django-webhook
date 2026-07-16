from django.core.serializers.json import DjangoJSONEncoder
from django.utils.module_loading import import_string

# The default admin site the package registers its models on. Set to ``None``
# (or the string ``"none"``) to disable automatic admin registration entirely.
DEFAULT_ADMIN_SITE = "django.contrib.admin.site"

defaults = dict(
    PAYLOAD_ENCODER_CLASS=DjangoJSONEncoder,
    STORE_EVENTS=True,
    # Retention. ``EVENTS_RETENTION_DAYS`` is kept for backwards compatibility and
    # acts as the default window for *succeeded* deliveries. Failed deliveries are
    # retained independently and, by default, never purged so that an unrecovered
    # failure is never silently discarded.
    EVENTS_RETENTION_DAYS=30,
    SUCCEEDED_EVENTS_RETENTION_DAYS=None,  # None -> fall back to EVENTS_RETENTION_DAYS
    FAILED_EVENTS_RETENTION_DAYS=None,  # None -> never purge failed deliveries
    USE_CACHE=True,
    # How long (seconds) the subscription lookup is cached per process. There is
    # no invalidation on webhook changes, so a change takes effect only after
    # this window elapses — raise it to cut DB load, lower it to react faster.
    CACHE_TIMEOUT=60,
    # Dispatch events only after the surrounding DB transaction commits. On by
    # default; only turn off for projects that never wrap writes in transactions.
    DISPATCH_ON_COMMIT=True,
    # Outbound HTTP request timeout in seconds. Never infinite by default.
    REQUEST_TIMEOUT=10,
    # Maximum delivery retries per event.
    MAX_RETRIES=5,
    # Where to register the admin models. A dotted path to an ``AdminSite``
    # instance, or ``None``/``"none"`` to skip registration.
    ADMIN_SITE=DEFAULT_ADMIN_SITE,
    # Global default payload serializer: a dotted path to a callable taking a
    # model instance and returning a JSON-serializable dict. ``None`` uses the
    # built-in serializer.
    SERIALIZER_CLASS=None,
    # Per-model serializer overrides: {"app.Model": "dotted.path.to.callable"}.
    MODEL_SERIALIZERS={},
    # Opt-in: collapse multiple emissions of the same (subscription, topic,
    # object) within a single commit window into one delivery.
    COALESCE_EVENTS=False,
)


def get_settings():
    # pylint: disable=redefined-outer-name,import-outside-toplevel
    from django.conf import settings

    user_defined_settings = getattr(settings, "DJANGO_WEBHOOK", {})
    webhook_settings = {**defaults, **user_defined_settings}

    encoder_cls = webhook_settings["PAYLOAD_ENCODER_CLASS"]
    if isinstance(encoder_cls, str):
        webhook_settings["PAYLOAD_ENCODER_CLASS"] = import_string(encoder_cls)

    return webhook_settings


def succeeded_retention_days():
    """
    Retention window (in days) for succeeded deliveries. Falls back to the
    legacy ``EVENTS_RETENTION_DAYS`` when not set explicitly.
    """
    settings = get_settings()
    days = settings.get("SUCCEEDED_EVENTS_RETENTION_DAYS")
    if days is None:
        days = settings.get("EVENTS_RETENTION_DAYS")
    return days


def failed_retention_days():
    """
    Retention window (in days) for failed deliveries. ``None`` means failed
    deliveries are never purged (the safe default).
    """
    return get_settings().get("FAILED_EVENTS_RETENTION_DAYS")
