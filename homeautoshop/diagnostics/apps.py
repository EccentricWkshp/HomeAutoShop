from django.apps import AppConfig
from django.db.models.signals import post_delete, post_save


def _forget_code_lists(**_kwargs):
    """Drop the cached view of which code lists are installed.

    Module level, not a closure inside `ready`. `Signal.connect` keeps a *weak*
    reference to its receiver, so a function defined inside `ready` is
    collected the moment `ready` returns and the signal quietly stops firing —
    which looks exactly like the cache working, until somebody removes a list
    and the code page carries on quoting it.
    """
    from . import dtc

    dtc.forget()


class DiagnosticsConfig(AppConfig):
    name = "homeautoshop.diagnostics"
    label = "diagnostics"

    def ready(self):
        """Invalidate the code-list cache whenever what is installed changes.

        `dtc._lists` is consulted on every code lookup, so it is cached for the
        life of the process. That was safe while the lists were files in the
        image. Now that a make's list is a row somebody can install or
        remove, a cache with no invalidation would leave a code page quoting a
        list that has just been deleted — and the removal would look broken.

        On the signals rather than in the install path, because the rows are
        also written directly: by the management command, by the admin, by
        tests, and by `loaddata` on a restore. Anything that writes the table
        should invalidate the read of it.
        """
        from .models import InstalledCodeList

        post_save.connect(
            _forget_code_lists, sender=InstalledCodeList, dispatch_uid="dtc-forget-save"
        )
        post_delete.connect(
            _forget_code_lists, sender=InstalledCodeList, dispatch_uid="dtc-forget-delete"
        )
