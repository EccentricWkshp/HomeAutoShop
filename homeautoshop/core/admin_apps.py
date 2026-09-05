"""The admin app config, kept out of `core/apps.py` on purpose.

`AdminConfig` carries `default = True`, and so does `CoreConfig` — two of those
in one module and Django refuses to pick a default app config for the package.
It also belongs apart on its own merits: this configures `django.contrib.admin`,
not this app.
"""

from django.contrib.admin.apps import AdminConfig


class ShopAdminConfig(AdminConfig):
    """Swaps in the admin site that knows about the trash.

    `default_site` is the supported seam for this, and it is resolved lazily —
    `admin.site` is a proxy object — so every `@admin.register` in the codebase
    keeps working untouched.
    """

    default_site = "homeautoshop.core.adminsite.ShopAdminSite"
