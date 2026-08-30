"""Seed data shipped with the application (SPEC REFERENCE.md §2)."""

from django.core.management.base import BaseCommand
from django.db import transaction

from homeautoshop.assets.models import ServiceInfoProvider
from homeautoshop.diagnostics.profiles import seed as install_profiles
from homeautoshop.inspections.seed import install as install_inspections
from homeautoshop.maintenance.seed import install as install_schedules

PROVIDERS = [
    {
        "slug": "lemon",
        "name": "LEMON Manuals",
        "base_urls": ["https://lemon-manuals.la", "https://lemon-manuals.org.ua"],
        "url_template": "{make}/{year}/",
        "deep_link_depth": "make_year",
        # Double-encoded on purpose: the library's own paths carry a literal
        # `%20` that has itself been percent-encoded, and normalizing it 404s.
        "dtc_path": "Repair%2520and%2520Diagnosis/A%2520L%2520L%2520%2520Diagnostic%2520Trouble%2520Codes%2520%2528%2520DTC%2520%2529/index.html",
        "access": "free",
        "sort_order": 10,
        "notes": (
            "Free, no sign-up. Successor to Operation CHARM and a superset of it: "
            "roughly 10,000 US/Canada-market vehicles, 1960-2025. Mirrors are listed "
            "in order; the project runs across several jurisdictions deliberately."
        ),
    },
    {
        "slug": "charm",
        "name": "Operation CHARM",
        "base_urls": ["https://charm.li"],
        "url_template": "{make}/{year}/",
        "deep_link_depth": "make_year",
        "dtc_path": "Repair%2520and%2520Diagnosis/A%2520L%2520L%2520%2520Diagnostic%2520Trouble%2520Codes%2520%2528%2520DTC%2520%2529/index.html",
        "access": "free",
        "sort_order": 20,
        "notes": "Free, no sign-up. Coverage through roughly 2014; retained as a stable fallback.",
    },
    {
        "slug": "alldata",
        "name": "ALLDATA DIY",
        "base_urls": ["https://www.alldatadiy.com"],
        "url_template": "",
        "deep_link_depth": "root",
        "access": "paid",
        "sort_order": 30,
        "notes": (
            "Paid, per-vehicle annual subscription. Procedure URLs need an authenticated "
            "session, so HomeAutoShop links to the entry point only and you pin whatever "
            "URL you land on. Credentials are never stored here."
        ),
    },
]


class Command(BaseCommand):
    help = "Install or refresh seed data. Idempotent."

    def add_arguments(self, parser):
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="Rebuild template contents, discarding edits to built-in templates.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        created = updated = 0
        for row in PROVIDERS:
            _obj, was_created = ServiceInfoProvider.objects.update_or_create(
                slug=row["slug"], defaults=row
            )
            created += was_created
            updated += not was_created
        self.stdout.write(
            self.style.SUCCESS(f"service info providers: {created} created, {updated} updated")
        )
        definitions, templates = install_schedules(refresh=options["refresh"])
        self.stdout.write(
            self.style.SUCCESS(
                f"maintenance: {definitions} service definitions, {templates} schedule templates"
            )
        )
        inspections = install_inspections(refresh=options["refresh"])
        self.stdout.write(self.style.SUCCESS(f"inspections: {inspections} templates"))

        # Never refreshed, even with --refresh. A profile the operator has
        # edited is theirs; a revised bundled profile arrives as a new version
        # row instead, which is what versioning them is for.
        profiles = install_profiles()
        self.stdout.write(self.style.SUCCESS(f"parser profiles: {profiles} installed"))
