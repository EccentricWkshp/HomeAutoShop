import logging

from django.apps import AppConfig
from django.conf import settings

log = logging.getLogger(__name__)


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

        # Said once, at startup, in the log an operator reads when something is
        # wrong. The banner and the health screen cover somebody looking at the
        # site; this covers the case where nobody is, and it needs no database.
        if getattr(settings, "NO_AUTHENTICATION", False):
            log.warning(
                "PASSWORD_POLICY=noauth: sign-in is disabled and every request "
                "is the oldest administrator account. Anyone who can reach this "
                "instance has full access to it."
            )
