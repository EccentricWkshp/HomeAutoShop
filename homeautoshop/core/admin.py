from django.contrib import admin

from .models import AuditLog, Job, NotificationChannel, NotificationSent, Setting


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


@admin.register(NotificationChannel)
class NotificationChannelAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "masked_target", "is_enabled", "last_sent_at", "last_error")
    list_filter = ("kind", "is_enabled")


@admin.register(NotificationSent)
class NotificationSentAdmin(admin.ModelAdmin):
    list_display = ("sent_at", "channel", "dedupe_key", "subject")
    list_filter = ("channel",)

    def has_add_permission(self, request):
        return False
