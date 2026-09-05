"""The shop's admin site.

**This module must not register anything.** `admin.site` is a lazy proxy that
resolves by importing the class named in `default_site`; if that import also
runs `@admin.register` decorators, each one asks for `admin.site` again while
the proxy is still mid-setup, and the re-entrant call builds a *second* site.
The registrations land on the throwaway and vanish. Keeping the class in a
module of its own removes the cycle rather than sequencing around it.
"""

from __future__ import annotations

from datetime import timedelta

from django.apps import apps
from django.contrib.admin import AdminSite
from django.shortcuts import render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class ShopAdminSite(AdminSite):
    """The stock admin, plus one page that reads across every table.

    A per-model "record state" filter answers "is *this* row deleted?", which is
    the wrong question when clearing up: nobody knows which of fifty changelists
    to open. What was missing was the other direction — *what is in the trash?* —
    and answering it needs a page that spans models, which a `ModelAdmin` by
    construction cannot be.

    It is deliberately wider than the application's own `/trash/` screen. That
    one lists eleven curated kinds, because it is a safety net for things people
    delete by accident. This lists everything with a `deleted_at`, because the
    reason to come here is a leftover in a table nobody thinks about.
    """

    #: Named apart from `admin/index.html`: overriding that name and extending
    #: it in the same file gives a template that extends itself.
    index_template = "admin/shop_index.html"

    def get_urls(self):
        return [
            path("trash/", self.admin_view(self.trash_view), name="trash_overview"),
            *super().get_urls(),
        ]

    def each_context(self, request):
        context = super().each_context(request)
        context["trash_url"] = reverse("admin:trash_overview", current_app=self.name)
        return context

    def trash_view(self, request):
        from .models import TRASH_RETENTION_DAYS

        cutoff = timezone.now() - timedelta(days=TRASH_RETENTION_DAYS)
        rows = []
        for model in apps.get_models():
            if not hasattr(model, "all_objects") or model not in self._registry:
                continue
            trashed = model.all_objects.filter(deleted_at__isnull=False)
            count = trashed.count()
            if not count:
                continue
            meta = model._meta
            rows.append(
                {
                    "label": str(meta.verbose_name_plural).title(),
                    "app": meta.app_config.verbose_name,
                    "count": count,
                    "expired": trashed.filter(deleted_at__lt=cutoff).count(),
                    "url": reverse(
                        f"admin:{meta.app_label}_{meta.model_name}_changelist",
                        current_app=self.name,
                    )
                    + "?trashed=1",
                }
            )
        rows.sort(key=lambda row: (-row["count"], row["label"]))
        return render(
            request,
            "admin/trash_overview.html",
            {
                **self.each_context(request),
                "title": _("Trash"),
                "rows": rows,
                "total": sum(row["count"] for row in rows),
                "retention_days": TRASH_RETENTION_DAYS,
            },
        )
