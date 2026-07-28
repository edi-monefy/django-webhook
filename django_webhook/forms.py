import django
from django import forms

from django_webhook.models import Webhook


class WebhookURLField(forms.URLField):
    """
    A URL field that pins the scheme assumed for scheme-less input.

    Django 5.0 added ``assume_scheme`` and warns when it is left unset, because
    the default flips from http to https in Django 6.0. Pinning it makes the
    scheme this package's choice rather than something a Django upgrade changes
    underneath an existing subscriber. Django 4.x has no such argument and
    always assumes http.
    """

    def __init__(self, **kwargs):
        if django.VERSION >= (5, 0):
            kwargs.setdefault("assume_scheme", "https")
        super().__init__(**kwargs)


class WebhookForm(forms.ModelForm):
    # Declared only to pin the assumed scheme; max_length still tracks the
    # model field so the two cannot drift.
    url = WebhookURLField(max_length=Webhook._meta.get_field("url").max_length)

    class Meta:
        model = Webhook
        fields = [
            "url",
            "active",
            "topics",
        ]
