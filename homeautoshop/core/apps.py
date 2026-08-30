from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "homeautoshop.core"
    label = "core"
    verbose_name = "Core"

    def ready(self) -> None:
        from . import signals  # noqa: F401
