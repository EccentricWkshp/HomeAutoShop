from django.contrib import admin

from .models import CodeDescription, DiagnosticCode, DiagnosticSession, ParserProfile


class CodeInline(admin.TabularInline):
    model = DiagnosticCode
    extra = 0
    # A reading is what the car said. Only the verdict on it is editable here.
    readonly_fields = ("code", "system", "is_generic", "module", "state", "state_raw")


@admin.register(DiagnosticSession)
class DiagnosticSessionAdmin(admin.ModelAdmin):
    list_display = ("asset", "performed_on", "tool", "source", "review_status", "parse_status")
    list_filter = ("review_status", "parse_status", "source")
    search_fields = ("tool", "tool_model", "notes")
    inlines = [CodeInline]
    raw_id_fields = ("asset", "work_order", "raw_media", "parser_profile")


@admin.register(ParserProfile)
class ParserProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "version", "tool_vendor", "tool_model", "media_type", "is_active")
    list_filter = ("is_active", "media_type", "source")
    search_fields = ("name", "tool_vendor", "tool_model")


@admin.register(CodeDescription)
class CodeDescriptionAdmin(admin.ModelAdmin):
    list_display = ("code", "make", "description")
    search_fields = ("code", "make", "description")
