"""
Built-in schedule templates (SPEC REFERENCE.md §2, FR-MAINT-1).

Generic intervals, not manufacturer data — no free OEM schedule source exists
(§8.4), so these are a sane starting point the operator edits from. Per-asset
intervals are always editable: a shipped default that cannot be changed is one
people work around rather than with.
"""

from __future__ import annotations

# (name, translation_key, severity, distance, unit, months, hours)
DEFINITIONS = [
    ("Engine oil and filter", "svc.oil", "routine", 5000, "mi", 6, 50),
    ("Tire rotation", "svc.tire_rotation", "safety", 6000, "mi", 6, None),
    ("Engine air filter", "svc.air_filter", "routine", 15000, "mi", 12, 100),
    ("Cabin air filter", "svc.cabin_filter", "routine", 15000, "mi", 12, None),
    ("Brake fluid", "svc.brake_fluid", "safety", None, "mi", 24, None),
    ("Brake inspection", "svc.brake_inspection", "safety", 10000, "mi", 12, None),
    ("Coolant", "svc.coolant", "routine", 50000, "mi", 60, 500),
    ("Transmission fluid", "svc.trans_fluid", "routine", 60000, "mi", 60, None),
    ("Differential fluid", "svc.diff_fluid", "routine", 50000, "mi", 60, None),
    ("Spark plugs", "svc.spark_plugs", "routine", 60000, "mi", 72, None),
    ("Serpentine belt", "svc.belt", "routine", 60000, "mi", 60, 500),
    ("Battery check", "svc.battery", "routine", None, "mi", 12, None),
    ("Wiper blades", "svc.wipers", "safety", None, "mi", 12, None),
    ("State inspection", "svc.state_inspection", "emissions", None, "mi", 12, None),
    ("Registration renewal", "svc.registration", "routine", None, "mi", 12, None),
    # Equipment
    ("Small engine oil", "svc.small_engine_oil", "routine", None, "mi", 12, 50),
    ("Spark plug (small engine)", "svc.small_engine_plug", "routine", None, "mi", 24, 100),
    ("Air filter (small engine)", "svc.small_engine_air", "routine", None, "mi", 12, 25),
    ("Blade sharpening", "svc.blade", "safety", None, "mi", 12, 25),
    ("Fuel stabilizer / winterize", "svc.winterise", "routine", None, "mi", 12, None),
]

# template slug -> (name, asset_kinds, vehicle_classes, [(definition, distance, months, hours)])
TEMPLATES = {
    "gas-normal": (
        "Gasoline — normal service",
        ["vehicle"],
        [],
        [
            ("Engine oil and filter", 7500, 12, None),
            ("Tire rotation", 7500, 12, None),
            ("Engine air filter", 30000, 24, None),
            ("Cabin air filter", 15000, 12, None),
            ("Brake fluid", None, 36, None),
            ("Brake inspection", 15000, 12, None),
            ("Coolant", 100000, 60, None),
            ("Transmission fluid", 60000, 72, None),
            ("Spark plugs", 100000, 96, None),
            ("Serpentine belt", 90000, 84, None),
            ("Battery check", None, 12, None),
            ("Wiper blades", None, 12, None),
            ("State inspection", None, 12, None),
            ("Registration renewal", None, 12, None),
        ],
    ),
    "gas-severe": (
        "Gasoline — severe service",
        ["vehicle"],
        [],
        [
            # Short trips, towing, dust and cold are the normal case for a home
            # shop, which is why this is the template most people should pick.
            ("Engine oil and filter", 3000, 6, None),
            ("Tire rotation", 5000, 6, None),
            ("Engine air filter", 15000, 12, None),
            ("Cabin air filter", 12000, 12, None),
            ("Brake fluid", None, 24, None),
            ("Brake inspection", 10000, 12, None),
            ("Coolant", 50000, 48, None),
            ("Transmission fluid", 30000, 36, None),
            ("Differential fluid", 30000, 36, None),
            ("Spark plugs", 60000, 72, None),
            ("Battery check", None, 12, None),
            ("Wiper blades", None, 12, None),
            ("State inspection", None, 12, None),
            ("Registration renewal", None, 12, None),
        ],
    ),
    "diesel": (
        "Diesel",
        ["vehicle"],
        [],
        [
            ("Engine oil and filter", 7500, 12, None),
            ("Tire rotation", 7500, 12, None),
            ("Engine air filter", 15000, 24, None),
            ("Coolant", 50000, 48, None),
            ("Brake fluid", None, 24, None),
            ("Brake inspection", 15000, 12, None),
            ("State inspection", None, 12, None),
            ("Registration renewal", None, 12, None),
        ],
    ),
    "ev": (
        "Electric",
        ["vehicle"],
        [],
        [
            # No oil, no plugs, no belts — the point of shipping this separately
            # is that an EV owner should not have to delete nine irrelevant items.
            ("Tire rotation", 7500, 12, None),
            ("Cabin air filter", 15000, 12, None),
            ("Brake fluid", None, 36, None),
            ("Brake inspection", 15000, 12, None),
            ("Coolant", 100000, 96, None),
            ("Wiper blades", None, 12, None),
            ("State inspection", None, 12, None),
            ("Registration renewal", None, 12, None),
        ],
    ),
    "motorcycle": (
        "Motorcycle",
        ["vehicle"],
        ["motorcycle"],
        [
            ("Engine oil and filter", 3000, 12, None),
            ("Brake fluid", None, 24, None),
            ("Brake inspection", 5000, 12, None),
            ("Wiper blades", None, None, None),
            ("State inspection", None, 12, None),
            ("Registration renewal", None, 12, None),
        ],
    ),
    "small-engine": (
        "Small engine / equipment",
        ["equipment"],
        [],
        [
            ("Small engine oil", None, 12, 50),
            ("Spark plug (small engine)", None, 24, 100),
            ("Air filter (small engine)", None, 12, 25),
            ("Blade sharpening", None, 12, 25),
            ("Fuel stabilizer / winterize", None, 12, None),
        ],
    ),
}


def install(*, refresh: bool = False, revive: bool = False) -> tuple[int, int]:
    """Create or refresh the built-in definitions and templates. Idempotent."""
    from .models import ScheduleTemplate, ServiceDefinition, TemplateItem

    definitions: dict[str, ServiceDefinition] = {}
    for name, key, severity, distance, unit, months, hours in DEFINITIONS:
        definition, _created = ServiceDefinition.objects.update_or_create(
            name=name,
            defaults={
                "translation_key": key,
                "severity": severity,
                "default_interval_distance": distance,
                "default_interval_unit": unit,
                "default_interval_months": months,
                "default_interval_hours": hours,
            },
        )
        definitions[name] = definition

    template_count = 0
    for slug, (name, kinds, classes, entries) in TEMPLATES.items():
        # Looked up through `all_objects`, and a deleted one is left deleted.
        # Two reasons, and the second is the sharp one. An operator who removed
        # a shipped template meant it, and having it reappear on the next boot
        # would be the application arguing with them. And `slug` is uniquely
        # constrained without regard to `deleted_at`, so the alive manager
        # would not find the soft-deleted row and the create would fail on the
        # constraint — seeding would crash, not merely resurrect.
        existing = ScheduleTemplate.all_objects.filter(slug=slug).first()
        if existing is not None and existing.deleted_at is not None:
            # Booting respects the deletion; `revive` is somebody
            # asking for the shipped set back, which is a different
            # intent and the only way home once the trash has aged out.
            if not revive:
                continue
            existing.deleted_at = None
            existing.save(update_fields=["deleted_at"])
        template, _created = ScheduleTemplate.objects.update_or_create(
            slug=slug,
            defaults={
                "name": name,
                "asset_kinds": kinds,
                "vehicle_classes": classes,
                "source": ScheduleTemplate.Source.BUILTIN,
            },
        )
        template_count += 1
        if refresh:
            template.items.all().delete()
        for index, (definition_name, distance, months, hours) in enumerate(entries):
            TemplateItem.objects.update_or_create(
                template=template,
                definition=definitions[definition_name],
                defaults={
                    "interval_distance": distance,
                    "interval_unit": "mi",
                    "interval_months": months,
                    "interval_hours": hours,
                    "sequence": index,
                },
            )
    return len(definitions), template_count
