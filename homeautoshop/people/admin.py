from django.contrib import admin

from .models import Person


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ("display_name", "email", "phone", "is_household")
    search_fields = ("display_name", "given_name", "family_name", "email")
