# Django Webhooks ![badge](https://github.com/edi-monefy/django-webhook/actions/workflows/ci.yml/badge.svg?event=push)

A plug-and-play Django app for sending outgoing webhooks on model changes.

> Published on PyPI as [`django-webhook2`](https://pypi.org/project/django-webhook2/). This is a
> reliability-focused fork of [`django-webhook`](https://github.com/danihodovic/django-webhook) by
> Dani Hodovic. The Python import path is unchanged (`django_webhook`), so it remains a drop-in
> replacement — install with `pip install django-webhook2`.

Django has a built-in signal system which allows programmers to schedule functions to be executed on
model changes. django-webhook leverages the signal system together with Celery to send HTTP requests
when models change.

Suppose we have a User model
```python
class User(models.Model):
    name = models.CharField(max_length=50)
    age = models.PositiveIntegerField()
```

If a webhook is configured, any time the above model is created, updated or deleted django-webhook
will send an outgoing HTTP request to a third party:

```
POST HTTP/1.1
host: webhook.site
user-agent: python-urllib3/2.0.3
django-webhook-uuid: 5e2ee3ba-905e-4360-94bf-18ef21c0e844
django-webhook-signature-v1:
django-webhook-request-timestamp: 1697818014

{
  "event_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
  "occurred_at": "2023-10-20T18:06:54+00:00",
  "topic": "users.User/create",
  "object": {
    "id": 3,
    "name": "Dani Doo",
    "age": 30
  },
  "object_type": "users.User",
  "webhook_uuid": "5e2ee3ba-905e-4360-94bf-18ef21c0e844"
}
```

### 🔥 Features
- Automatically sends webhooks on model changes
- Dispatch only after the database transaction commits — a rollback never publishes
- Event production is isolated from the writer: a broker error can never fail `save()`
- Leverages Celery for processing
- Webhook authentication using HMAC
- Retries every failure mode (connection, timeout, error response) with exponential backoff
- Configurable request timeout
- A delivery still being retried is never reported as failed
- Per-event id and occurrence time in the envelope for safe dedup/ordering
- Manual re-send of recorded deliveries, from the admin or programmatically
- A payload that cannot be produced is recorded and never delivered, never degraded
- Independent retention windows for succeeded and failed deliveries
- Pluggable per-model payload serializers
- Public emission API for set-based writes (`QuerySet.update`, `bulk_create`, `bulk_update`)
- Admin integration (configurable admin site)
- Audit log with past webhook events
- Protection from replay attacks
- Allows rotating webhook secrets

### 📖 Documentation

https://django-webhook2.readthedocs.io


### Contributors
<a href="https://github.com/danihodovic/django-webhook/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=danihodovic/django-webhook" />
</a>

Made with [contrib.rocks](https://contrib.rocks).
