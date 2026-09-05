from django.contrib import admin

from homeautoshop.core.admin import SoftDeleteAdmin

from .models import FluidResult, FluidSample


@admin.register(FluidSample)
class FluidSampleAdmin(SoftDeleteAdmin):
    list_display = ("asset", "compartment", "sampled_on", "lab", "report_number", "fluid_changed")
    list_filter = ("compartment", "lab", "fluid_changed")
    search_fields = ("report_number", "asset__nickname", "fluid_brand")


@admin.register(FluidResult)
class FluidResultAdmin(SoftDeleteAdmin):
    list_display = ("sample", "analyte", "value", "unit", "flagged")
    list_filter = ("flagged", "analyte")
    raw_id_fields = ("sample",)
