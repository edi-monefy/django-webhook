import logging

from django.apps import apps
from django.contrib import admin, messages
from django.contrib.admin import TabularInline
from django.utils.module_loading import import_string

from django_webhook.models import Webhook, WebhookDeliveryAttempt, WebhookEvent, WebhookSecret

from .forms import WebhookForm
from .settings import DEFAULT_ADMIN_SITE, get_settings

logger = logging.getLogger(__name__)


class WebhookSecretInline(TabularInline):
    model = WebhookSecret
    fields = ("token",)
    extra = 0


class WebhookAdmin(admin.ModelAdmin):
    form = WebhookForm
    list_display = (
        "url",
        "active",
        "uuid",
    )
    list_filter = ("active", "topics")
    search_fields = ("url",)
    filter_horizontal = ("topics",)
    inlines = [WebhookSecretInline]


class WebhookDeliveryAttemptInline(TabularInline):
    model = WebhookDeliveryAttempt
    fields = ("attempt_number", "status", "error", "attempted_at")
    readonly_fields = ("attempt_number", "status", "error", "attempted_at")
    extra = 0

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "url", "status", "topic", "occurred_at", "created", "attempt_count")
    list_filter = ("webhook", "status", "topic")
    search_fields = ("event_id", "url", "status", "topic")
    readonly_fields = (
        "webhook",
        "event_id",
        "url",
        "status",
        "occurred_at",
        "created",
        "topic",
        "object_type",
        "object",
        "error",
    )
    inlines = [WebhookDeliveryAttemptInline]
    actions = ["resend_selected"]

    def has_add_permission(self, request):
        return False

    # Rows remain fully read-only (every field is in ``readonly_fields``), but
    # change permission is left intact so the re-send action and the
    # detail view are reachable.

    def attempt_count(self, obj):
        return obj.attempts.count()
    attempt_count.short_description = "Attempts"

    @admin.action(description="Re-send selected webhook deliveries")
    def resend_selected(self, request, queryset):
        count = queryset.resend()
        self.message_user(
            request,
            f"Re-enqueued {count} webhook deliver{'y' if count == 1 else 'ies'}.",
            level=messages.SUCCESS,
        )


def _resolve_admin_site():
    """
    Resolve the configured admin site, or ``None`` to skip registration.

    ``DJANGO_WEBHOOK["ADMIN_SITE"]`` may be an ``AdminSite`` instance, a dotted
    path to one, or ``None`` / ``"none"`` to opt out entirely.

    The default resolves ``django.contrib.admin.site``, which requires the admin
    app to be installed. A project that doesn't install ``django.contrib.admin``
    (e.g. an API-only service) simply has nothing to register on — that must be a
    silent no-op, never a startup crash.
    """
    site = get_settings().get("ADMIN_SITE")
    if site is None or site == "none":
        return None

    if site == DEFAULT_ADMIN_SITE and not apps.is_installed("django.contrib.admin"):
        return None

    if isinstance(site, str):
        try:
            return import_string(site)
        except ImportError:
            logger.warning(
                "DJANGO_WEBHOOK['ADMIN_SITE']=%r could not be imported; "
                "skipping admin registration.",
                site,
            )
            return None
    return site


def register_admin():
    """
    Register the package's models on the configured admin site. Idempotent and
    safe to call from ``AppConfig.ready``.
    """
    site = _resolve_admin_site()
    if site is None:
        return
    for model, model_admin in (
        (Webhook, WebhookAdmin),
        (WebhookEvent, WebhookEventAdmin),
    ):
        if model in site._registry:  # pylint: disable=protected-access
            site.unregister(model)
        site.register(model, model_admin)
