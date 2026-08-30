"""Template context available on every page."""

from __future__ import annotations

from django.conf import settings
from django.utils.translation import gettext_lazy as _


def instance(request):
    return {
        "shop_name": settings.SHOP_NAME,
        "offline_mode": settings.OFFLINE_MODE,
        "show_product_links": settings.SHOP_NAME and settings.SHOW_PRODUCT_LINKS,
        "units_preference": getattr(getattr(request, "user", None), "units", None) or settings.UNITS,
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
