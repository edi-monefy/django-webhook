"""
Integration & configuration: configurable admin registration (E1), idempotent
topic sync outside startup (E2), and shipped test utilities (E4).
"""

import pytest
from django.contrib.admin.sites import AdminSite
from django.core.management import call_command
from django.test import override_settings

from django_webhook.admin import _resolve_admin_site, register_admin
from django_webhook.models import Webhook, WebhookEvent, WebhookTopic, sync_topics
from django_webhook.test_utils import (
    capture_webhook_deliveries,
    create_webhook,
    run_on_commit_callbacks,
)
from tests.models import User

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------- #
# E1 — configurable admin registration
# --------------------------------------------------------------------------- #
def test_admin_registers_on_custom_site():
    custom = AdminSite()
    with override_settings(
        DJANGO_WEBHOOK=dict(MODELS=["tests.User"], ADMIN_SITE=custom)
    ):
        register_admin()
    # pylint: disable=protected-access
    assert Webhook in custom._registry
    assert WebhookEvent in custom._registry


def test_admin_registration_can_be_disabled():
    with override_settings(DJANGO_WEBHOOK=dict(MODELS=["tests.User"], ADMIN_SITE=None)):
        assert _resolve_admin_site() is None
        register_admin()  # must be a no-op, not an error
    with override_settings(
        DJANGO_WEBHOOK=dict(MODELS=["tests.User"], ADMIN_SITE="none")
    ):
        assert _resolve_admin_site() is None


def test_admin_registration_noop_without_admin_app(mocker):
    # An API-only project that doesn't install django.contrib.admin must not
    # crash at startup on the default ADMIN_SITE — it simply has nowhere to
    # register. (Regression: register_admin() used to import admin.site eagerly.)
    mocker.patch("django_webhook.admin.apps.is_installed", return_value=False)
    with override_settings(DJANGO_WEBHOOK=dict(MODELS=["tests.User"])):
        assert _resolve_admin_site() is None
        register_admin()  # must not raise


# --------------------------------------------------------------------------- #
# E2 — idempotent topic sync outside startup
# --------------------------------------------------------------------------- #
@override_settings(DJANGO_WEBHOOK=dict(MODELS=["tests.User", "tests.Country"]))
def test_sync_topics_is_idempotent():
    created, deleted = sync_topics()
    assert created == 6 and deleted == 0
    names = set(WebhookTopic.objects.values_list("name", flat=True))
    assert names == {
        "tests.User/create",
        "tests.User/update",
        "tests.User/delete",
        "tests.Country/create",
        "tests.Country/update",
        "tests.Country/delete",
    }
    # Running again creates nothing new.
    created, deleted = sync_topics()
    assert created == 0 and deleted == 0


@override_settings(DJANGO_WEBHOOK=dict(MODELS=["tests.User"]))
def test_sync_topics_prunes_unless_disabled():
    WebhookTopic.objects.create(name="tests.Country/create")  # stray
    _, deleted = sync_topics()
    assert deleted == 1
    assert not WebhookTopic.objects.filter(name="tests.Country/create").exists()

    WebhookTopic.objects.create(name="tests.Country/create")
    _, deleted = sync_topics(prune=False)
    assert deleted == 0
    assert WebhookTopic.objects.filter(name="tests.Country/create").exists()


@override_settings(DJANGO_WEBHOOK=dict(MODELS=["tests.User"]))
def test_management_command_syncs_topics():
    call_command("webhook_sync_topics")
    assert WebhookTopic.objects.filter(name="tests.User/create").exists()


# --------------------------------------------------------------------------- #
# E4 — test utilities
# --------------------------------------------------------------------------- #
@override_settings(DJANGO_WEBHOOK=dict(MODELS=["tests.User"], USE_CACHE=False))
def test_create_webhook_helper():
    webhook = create_webhook(
        url="https://example.com/hook", topics=["tests.User/create"]
    )
    assert webhook.secrets.count() == 1
    assert list(webhook.topics.values_list("name", flat=True)) == ["tests.User/create"]


@override_settings(DJANGO_WEBHOOK=dict(MODELS=["tests.User"], USE_CACHE=False))
def test_capture_webhook_deliveries_runs_on_commit():
    create_webhook(topics=["tests.User/create"])
    with capture_webhook_deliveries() as deliveries:
        User.objects.create(
            name="Dani",
            email="d@d.com",
            join_date="1970-01-01",
            last_active="2000-01-01T00:00:00Z",
        )
    # Without the helper running the on-commit callback this list would be
    # vacuously empty — the exact trap spec E4 warns about.
    assert len(deliveries) == 1
    assert deliveries[0]["topic"] == "tests.User/create"


@override_settings(DJANGO_WEBHOOK=dict(MODELS=["tests.User"], USE_CACHE=False))
def test_run_on_commit_callbacks_helper(responses):
    webhook = create_webhook(topics=["tests.User/create"])
    responses.post(webhook.url)
    User.objects.create(
        name="Dani",
        email="d@d.com",
        join_date="1970-01-01",
        last_active="2000-01-01T00:00:00Z",
    )
    assert len(responses.calls) == 0  # deferred to commit
    run_on_commit_callbacks()
    assert len(responses.calls) == 1
