# Welcome to django-webhook!

A plug-and-play Django app for sending outgoing webhooks on model changes.

Django has a built-in signal system which allows programmers to schedule functions to be executed on
model changes. django-webhook leverages the signal system together with Celery to send HTTP requests
when models change.

Suppose we have a User model:
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

Each event carries a unique `event_id` and the `occurred_at` time of the change (both stable across
retries), so at-least-once redeliveries are safe to dedup and order.

## 🔥 Features
- Automatically sends webhooks on model changes
- Dispatch only after the database transaction commits — a rollback never publishes
- Event production is isolated from the writer: a broker error can never fail `save()`
- Leverages Celery for processing
- Webhook authentication using HMAC
- Retries every failure mode (connection, timeout, error response) with exponential backoff
- Configurable request timeout
- A delivery still being retried is never reported as failed
- Manual re-send of recorded deliveries, from the admin or programmatically
- A payload that cannot be produced is recorded and never delivered, never degraded
- Independent retention windows for succeeded and failed deliveries
- Pluggable per-model payload serializers
- Public emission API for set-based writes (`QuerySet.update`, `bulk_create`, `bulk_update`)
- Admin integration (configurable admin site)
- Audit log with past webhook events
- Protection from replay attacks


### 📜 Table of Contents
```{toctree}
install
configuration
celery
examples
```
