from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import (
    AuditLog, Credential, ExternalRef, Job, NotificationChannel, NotificationSent, Setting,
)


class TrashedFilter(admin.SimpleListFilter):
    """Alive / trashed / everything, for a model that soft-deletes.

    The admin's own changelist reads `_default_manager`, which is `AliveManager`
    — so before this existed a soft-deleted row was not merely hard to find, it
    was unreachable. Defaulting to "alive" keeps the familiar view; the point is
    that the other two options now exist at all.
    """

    title = _("record state")
    parameter_name = "trashed"

    def lookups(self, request, model_admin):
        return (("0", _("Alive")), ("1", _("In the trash")), ("all", _("Both")))

    def queryset(self, request, queryset):
        value = self.value()
        if value == "1":
            return queryset.filter(deleted_at__isnull=False)
        if value == "all":
            return queryset
        return queryset.filter(deleted_at__isnull=True)

    def choices(self, changelist):
        # Suppress the built-in "All" choice: it would read as a fourth option
        # meaning the same thing as "Both" while actually meaning "the default".
        for choice in super().choices(changelist):
            if choice["query_string"] == "?":
                continue
            yield choice


class SoftDeleteAdmin(admin.ModelAdmin):
    """Base admin for a `BaseModel`: can see the trash, and can empty it.

    Two things every soft-deleting model needs and none of them had:

    * **Visibility.** `get_queryset` reads `all_objects`, so a trashed row can
      be found, inspected and dealt with instead of merely vanishing.
    * **A way out.** `delete_selected` on a soft-delete model just re-stamps
      `deleted_at`, which is not what somebody clearing up expects. The two
      actions here say plainly which one they are.

    Note that a soft delete never cascades — nothing in the ORM fires, because
    no SQL DELETE runs — so a hard delete here is also the first moment the
    database's own `on_delete` rules apply to these rows.
    """

    actions = ["restore_selected", "hard_delete_selected"]

    def get_queryset(self, request):
        queryset = self.model.all_objects.get_queryset()
        ordering = self.get_ordering(request)
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset

    def get_list_filter(self, request):
        return (TrashedFilter, *super().get_list_filter(request))

    @admin.display(boolean=True, description=_("alive"), ordering="deleted_at")
    def is_alive(self, obj) -> bool:
        return obj.deleted_at is None

    def get_list_display(self, request):
        return (*super().get_list_display(request), "is_alive")

    @admin.action(description=_("Restore selected (take out of the trash)"))
    def restore_selected(self, request, queryset):
        count = queryset.filter(deleted_at__isnull=False).update(deleted_at=None)
        self._audit(request, queryset, AuditLog.Action.RESTORE)
        self.message_user(request, _("Restored %(n)s record(s).") % {"n": count})

    @admin.action(description=_("Delete permanently (cannot be undone)"))
    def hard_delete_selected(self, request, queryset):
        """The real DELETE. Logged before the rows stop existing.

        Auditing first is not fussiness: after `hard_delete()` there is no row
        left to describe, and an unexplained gap in a table is exactly the thing
        this application's audit log exists to prevent.
        """
        self._audit(request, queryset, AuditLog.Action.DELETE)
        count, _detail = queryset.hard_delete()
        self.message_user(request, _("Permanently deleted %(n)s record(s).") % {"n": count})

    def _audit(self, request, queryset, action) -> None:
        AuditLog.objects.bulk_create(
            [
                AuditLog(
                    entity_type=type(obj).__name__,
                    entity_id=obj.pk,
                    action=action,
                    user=request.user,
                    summary=str(obj)[:255],
                )
                for obj in queryset
            ]
        )


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("at", "action", "entity_type", "user", "summary")
    list_filter = ("action", "entity_type")
    search_fields = ("summary",)
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("type", "state", "attempts", "run_after", "finished_at")
    list_filter = ("state", "type")
    actions = ["retry"]

    @admin.action(description="Retry selected jobs")
    def retry(self, request, queryset):
        queryset.update(state=Job.State.PENDING, attempts=0, last_error="")


admin.site.register(Setting)


@admin.register(ExternalRef)
class ExternalRefAdmin(admin.ModelAdmin):
    """Provenance rows, which outlive the things they point at.

    `ExternalRef` is a plain model with no soft delete and, until now, no admin
    page — so when the entity it names went into the trash the ref stayed
    behind, still resolving, still making an importer say "already imported"
    about an order nobody could see. Being able to find and drop a stale ref is
    what makes an order re-readable.
    """

    list_display = (
        "source_system", "external_type", "external_id",
        "entity_type", "entity_id", "state", "last_seen_at",
    )
    list_filter = ("source_system", "external_type", "entity_type", "state")
    search_fields = ("external_id", "entity_id", "source_instance_url")
    readonly_fields = ("first_imported_at",)


@admin.register(Credential)
class CredentialAdmin(admin.ModelAdmin):
    """Keys and dates only — never the secret.

    §17.1 is explicit that a credential "can be replaced or cleared, never
    displayed back", so the ciphertext is not a field here and there is no
    change form. What the admin is for is seeing which integrations hold a
    secret, and removing one.
    """

    list_display = ("key", "updated_at")
    readonly_fields = ("key", "updated_at")
    fields = ("key", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(NotificationChannel)
class NotificationChannelAdmin(SoftDeleteAdmin):
    list_display = ("name", "kind", "masked_target", "is_enabled", "last_sent_at", "last_error")
    list_filter = ("kind", "is_enabled")


@admin.register(NotificationSent)
class NotificationSentAdmin(admin.ModelAdmin):
    list_display = ("sent_at", "channel", "dedupe_key", "subject")
    list_filter = ("channel",)

    def has_add_permission(self, request):
        return False
