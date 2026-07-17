import pytest
import responses as responses_lib
from pytest_factoryboy import register

from django_webhook.dispatch import _query_webhooks_cached
from django_webhook.test_factories import (
    WebhookEventFactory,
    WebhookFactory,
    WebhookSecretFactory,
    WebhookTopicFactory,
)

register(WebhookFactory)
register(WebhookEventFactory)
register(WebhookTopicFactory)
register(WebhookSecretFactory)


@pytest.fixture
def responses():
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


@pytest.fixture(autouse=True)
def _clear_webhook_lookup_cache():
    # The subscription-lookup cache is a process-global with a 1-minute TTL.
    # Clear it between tests so a cached result never leaks across the DB resets
    # that pytest-django performs.
    _query_webhooks_cached.cache_clear()
    yield
    _query_webhooks_cached.cache_clear()
