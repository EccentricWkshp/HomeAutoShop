from django.contrib import admin

from .models import FluidResult, FluidSample


class ResultInline(admin.TabularInline):
    model = FluidResult
    extra = 0


@admin.register(FluidSample)
class FluidSampleAdmin(admin.ModelAdmin):
    list_display = ("asset", "compartment", "position", "sampled_on", "fluid_usage", "lab")
    list_filter = ("compartment", "lab")
    search_fields = ("asset__nickname", "report_number", "lab")
    inlines = [ResultInline]


@admin.register(FluidResult)
class FluidResultAdmin(admin.ModelAdmin):
    list_display = ("sample", "analyte", "value", "unit", "reference", "flagged")
    list_filter = ("analyte", "flagged")
