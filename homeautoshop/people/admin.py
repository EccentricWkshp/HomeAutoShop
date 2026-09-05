from django.contrib import admin

from homeautoshop.core.admin import SoftDeleteAdmin

from .models import Person


@admin.register(Person)
class PersonAdmin(SoftDeleteAdmin):
    list_display = ("display_name", "email", "phone", "is_household")
    list_filter = ("is_household",)
    search_fields = ("display_name", "given_name", "family_name", "email", "phone")
