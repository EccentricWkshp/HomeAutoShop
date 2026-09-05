"""Template context available on every page."""

from __future__ import annotations

from django.conf import settings
from django.utils.translation import gettext_lazy as _
from .runtime import conf


def _pending_restart(request) -> dict:
    """What is waiting for a restart, if anything (§17.2).

    Only computed for somebody who could act on it. A banner telling a person
    with no settings permission that the instance needs restarting is noise
    they cannot clear, on every page, for ever.
    """
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated and getattr(user, "is_admin", False)):
        return {}
    from .runtime import pending_restart_keys
    from .settings_registry import BY_KEY

    keys = pending_restart_keys()
    if not keys:
        return {}
    return {
        "pending_restart": [BY_KEY[key].label for key in keys if key in BY_KEY],
        "can_self_restart": bool(getattr(settings, "GUNICORN_PIDFILE", "")),
    }


def instance(request):
    return {
        **_pending_restart(request),
        # Said on every page, because the whole application is open to anyone
        # who can reach it and that is not a thing to have to remember. Only
        # `noauth` earns a banner: the weaker password floors still keep the
        # sign-in screen, and a permanent warning about a setting somebody
        # deliberately chose is noise they cannot clear.
        "no_authentication": getattr(settings, "NO_AUTHENTICATION", False),
        "shop_name": conf.SHOP_NAME,
        "offline_mode": conf.OFFLINE_MODE,
        "show_product_links": conf.SHOP_NAME and conf.SHOW_PRODUCT_LINKS,
        "units_preference": getattr(getattr(request, "user", None), "units", None) or conf.UNITS,
        # Handed to scanner.js as JSON, same reasoning as below: the script is a
        # cacheable static file and its wording still comes from the catalog.
        "scanner_strings": {
            "viewfinder": _("Camera"),
            "cancel": _("Cancel"),
            "starting": _("Starting the camera…"),
            "looking": _("Hold the code in view."),
            "denied": _("Camera access was refused. Allow it in the browser's site settings."),
            "noCamera": _("No camera was available."),
            "notOurs": _("That code is not one of this shop's labels."),
            "unsupported": _(
                "Scanning needs Chrome or Edge over HTTPS. Safari and Firefox cannot do "
                "it yet — type the value in instead."
            ),
        },
        # Handed to offline.js as JSON so the queue indicator's wording stays in
        # the message catalog (§5.6) while the script itself is a cacheable
        # static file with no template rendering in it.
        "offline_strings": {
            "synced": _("Up to date"),
            "offline": _("Offline"),
            "waiting": _("%(n)s waiting to sync"),
            "conflicts": _("%(n)s need a decision"),
            "stale": _("something has been waiting over two weeks"),
        },
    }
