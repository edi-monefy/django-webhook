# pylint: disable=redefined-builtin
from django.apps import apps
from django.db import models
from django.db.models.signals import ModelSignal, post_delete, post_save

from .dispatch import CREATE, DELETE, UPDATE, emit_event, find_webhooks
from .serializers import default_serialize
from .settings import get_settings

# Re-exported for backwards compatibility. The canonical implementations now
# live in ``dispatch`` (subscription lookup) and ``serializers`` (payload).
__all__ = [
    "CREATE",
    "UPDATE",
    "DELETE",
    "SignalListener",
    "connect_signals",
    "model_dict",
    "find_webhooks",
]


class SignalListener:
    def __init__(
        self, signal: ModelSignal, signal_name: str, model_cls: type[models.Model]
    ):
        valid_signals = ["post_save", "post_delete"]
        if signal_name not in valid_signals:
            raise ValueError(f"{signal} must be one of {valid_signals}")

        self.signal = signal
        self.signal_name = signal_name
        self.model_cls = model_cls

    # pylint: disable=unused-argument
    def run(self, sender, created: bool = False, instance=None, **kwargs):
        action_type = None
        match self.signal_name:
            case "post_save" if created:
                action_type = CREATE
            case "post_save":
                action_type = UPDATE
            case "post_delete":
                action_type = DELETE

        # Delegate to the shared dispatch seam. It defers to transaction commit,
        # isolates payload production from the writer, and never re-broadcasts a
        # model signal to trigger itself.
        emit_event(instance, action_type)

    def connect(self):
        self.signal.connect(
            self.run, sender=self.model_cls, weak=False, dispatch_uid=self.uid  # type: ignore
        )

    @property
    def uid(self):
        return f"django_webhook_{self.model_label}_{self.signal_name}"

    @property
    def model_label(self):
        return self.model_cls._meta.label


def connect_signals():
    for cls in _active_models():
        post_save_listener = SignalListener(
            signal=post_save, signal_name="post_save", model_cls=cls
        )
        post_delete_listener = SignalListener(
            signal=post_delete, signal_name="post_delete", model_cls=cls
        )
        post_save_listener.connect()
        post_delete_listener.connect()


def model_dict(model):
    """
    Deprecated alias for the default serializer. Retained so existing imports
    keep working; new code should use ``django_webhook.serializers``.
    """
    return default_serialize(model)


def _active_models():
    model_names = get_settings().get("MODELS", [])
    model_classes = []
    for name in model_names:
        parts = name.split(".")
        if len(parts) != 2:
            continue
        app_label, model_label = parts
        try:
            model_class = apps.get_model(app_label, model_label)
        except LookupError:
            continue
        model_classes.append(model_class)
    return model_classes
