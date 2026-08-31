from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "homeautoshop.core"
    label = "core"
    verbose_name = "Core"

    def ready(self) -> None:
        # Note what is deliberately *not* here: reading the stored settings.
        # `ready()` runs for every management command, including the `migrate`
        # that creates the table it would be reading, and Django warns about
        # database access at this point for exactly that reason. The overlay is
        # applied on the first request instead (`ConfigMiddleware`) and at
        # worker startup — both of which are after the database exists.
        from . import signals  # noqa: F401
