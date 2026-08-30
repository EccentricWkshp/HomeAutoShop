"""
Built-in inspection templates (SPEC REFERENCE.md §2, FR-DVI-13).

The pre-purchase inspection leads because it is the highest-leverage thirty
minutes in the hobby, and it is the one that happens somewhere with no signal
and no second chance.

**Areas describe where you are standing, not what kind of thing you are
looking at.** There is deliberately no "Fluids" area: you check engine oil,
coolant, brake fluid and washer fluid with the hood up, and differential and
transfer-case fluid on your back under the car. Grouping them by substance
sent you round the vehicle twice.

Point names are load-bearing. `services.compare()` matches a result to its
counterpart in the previous inspection by `(name, position)`, so renaming a
built-in point silently severs year-over-year history. Add and re-file freely;
rename only on purpose.
"""

from __future__ import annotations

# name, area, type, unit, positions, sub_positions, thresholds, photo, safety, optional, guidance
PPI = [
    ("Tire tread depth", "tires_wheels", "measurement", "/32in",
     ["LF", "RF", "LR", "RR"], ["outer", "center", "inner"],
     {"fail": {"lte": 2}, "attention": {"lte": 4}, "pass": {"gt": 4}},
     "on_attention", True, False,
     "Measure outer, center and inner on each tire. A large spread across one "
     "tire means alignment or inflation, not just wear."),
    ("Tire DOT date code", "tires_wheels", "status", "",
     ["LF", "RF", "LR", "RR"], [], {}, "always", True, False,
     "Four digits: week and year of manufacture. Over 6 years is attention "
     "regardless of tread; over 10 is a fail regardless of tread."),
    ("Brake pad thickness", "brakes", "both", "mm",
     ["LF", "RF", "LR", "RR"], ["inner", "outer"],
     {"fail": {"lte": 3}, "attention": {"lte": 5}, "pass": {"gt": 5}},
     "on_attention", True, False, "Measure the friction material, not the backing plate."),
    ("Brake rotor condition", "brakes", "status", "", ["LF", "RF", "LR", "RR"], [],
     {}, "on_attention", True, False,
     "Scoring, lipping, heat cracking, rust on the swept area."),
    ("Frame and rocker corrosion", "under_vehicle", "status", "", [], [], {},
     "on_attention", True, False,
     "Structural rust is the single most common reason to walk away. "
     "Photograph anything scaling or perforated."),
    ("Fluid leaks", "under_vehicle", "status", "", [], [], {}, "on_attention", False, False,
     "Engine, transmission, differential, brake lines, power steering."),
    ("Exhaust condition", "exhaust_emissions", "status", "", [], [], {},
     "on_attention", True, False,
     "Perforation, hangers, and anything sounding louder than it should."),
    ("Suspension play", "suspension_steering", "status", "", ["LF", "RF", "LR", "RR"], [],
     {}, "on_attention", True, False, "Bushings, ball joints, tie rods, shock leakage."),
    ("Cold start behavior", "road_test", "status", "", [], [], {}, "never", False, False,
     "Insist on a genuinely cold start. A seller who warmed the car up before "
     "you arrived is worth a note of its own."),

    # Fluids, where you actually check them.
    ("Engine oil condition", "under_hood", "status", "", [], [], {},
     "on_attention", False, False,
     "Level, color, and any sign of coolant contamination. Milky residue on "
     "the cap is a reason to stop and think, not a reason to negotiate."),
    ("Coolant condition", "under_hood", "status", "", [], [], {},
     "on_attention", False, False,
     "Level, color, and oil contamination in the reservoir."),
    ("Brake fluid condition", "under_hood", "status", "", [], [], {},
     "on_attention", True, False,
     "At the master cylinder. Dark fluid is old fluid; a low level with good "
     "pads means it went somewhere."),
    ("Automatic transmission fluid", "under_hood", "status", "", [], [], {},
     "on_attention", False, True,
     "If it has a dipstick. Burnt smell or dark brown is expensive news on a "
     "car you do not own yet."),
    ("Power steering fluid", "under_hood", "status", "", [], [], {},
     "never", False, True, "Skip on an electrically assisted rack."),

    ("Battery voltage", "under_hood", "measurement", "V", [], [],
     {"fail": {"lt": 12.0}, "attention": {"lt": 12.4}, "pass": {"gte": 12.4}},
     "never", False, False, "Engine off, after sitting. Under 12.4 V is a tired battery."),
    ("Warning lights", "interior", "status", "", [], [], {}, "on_attention", True, False,
     "Check they illuminate at key-on and then extinguish. A bulb removed to "
     "hide a fault is a fail, not a pass."),
    ("Road test — braking", "road_test", "status", "", [], [], {}, "never", True, False,
     "Pull, pulsation, noise, pedal travel."),
    ("Road test — steering and tracking", "road_test", "status", "", [], [], {},
     "never", True, False, "Wander, vibration, off-center wheel."),
    ("Road test — transmission", "road_test", "status", "", [], [], {}, "never", False, False,
     "Shift quality, slipping, flare between gears."),
]

ANNUAL = [
    ("Tire tread depth", "tires_wheels", "measurement", "/32in",
     ["LF", "RF", "LR", "RR"], ["outer", "center", "inner"],
     {"fail": {"lte": 2}, "attention": {"lte": 4}, "pass": {"gt": 4}},
     "on_attention", True, False, "Measure outer, center and inner on each tire."),
    ("Tire pressure", "tires_wheels", "measurement", "psi", ["LF", "RF", "LR", "RR"], [],
     {}, "never", False, False, "Cold, against the door-jamb placard."),
    ("Brake pad thickness", "brakes", "both", "mm", ["LF", "RF", "LR", "RR"], ["inner", "outer"],
     {"fail": {"lte": 3}, "attention": {"lte": 5}, "pass": {"gt": 5}},
     "on_attention", True, False, ""),
    ("Lights and signals", "lighting_electrical", "status", "", [], [], {},
     "on_attention", True, False,
     "Headlights, brake lights, indicators, reverse, plate light."),
    ("Wiper blades", "body_glass", "status", "", [], [], {}, "never", False, False, ""),
    ("Battery voltage", "under_hood", "measurement", "V", [], [],
     {"fail": {"lt": 12.0}, "attention": {"lt": 12.4}, "pass": {"gte": 12.4}},
     "never", False, False, ""),
    ("Belts and hoses", "under_hood", "status", "", [], [], {}, "on_attention", False, False, ""),

    # Every fluid, each where you check it. Optional points cover drivetrains
    # that do not have them — mark those "not applicable" once and move on.
    ("Engine oil condition", "under_hood", "status", "", [], [], {},
     "on_attention", False, False, "Level, color, contamination."),
    ("Coolant condition", "under_hood", "status", "", [], [], {},
     "on_attention", False, False,
     "Level in the reservoir when cold, color, and freeze protection if you "
     "have a refractometer to hand."),
    ("Brake fluid condition", "under_hood", "status", "", [], [], {},
     "never", True, False,
     "Color and moisture content. Brake fluid is hygroscopic — it degrades on "
     "a calendar, not on mileage."),
    ("Clutch fluid", "under_hood", "status", "", [], [], {}, "never", True, True,
     "Hydraulic clutches only, and often the same reservoir as the brakes."),
    ("Power steering fluid", "under_hood", "status", "", [], [], {},
     "never", False, True, "Not fitted to an electrically assisted rack."),
    ("Automatic transmission fluid", "under_hood", "status", "", [], [], {},
     "on_attention", False, True,
     "Level at temperature per the manual, plus color and smell. Many modern "
     "units have no dipstick — mark not applicable."),
    ("Washer fluid", "under_hood", "status", "", [], [], {}, "never", False, False,
     "Topped up, and rated for the coldest weather you expect."),
    ("Diesel exhaust fluid (DEF)", "under_hood", "status", "", [], [], {},
     "never", False, True, "Diesels with SCR only."),

    ("Manual transmission fluid", "under_vehicle", "status", "", [], [], {},
     "never", False, True, "Level at the fill plug. Check the fill plug opens "
     "before you drain anything."),
    ("Transfer case fluid", "under_vehicle", "status", "", [], [], {},
     "never", False, True, "Four-wheel and all-wheel drive only."),
    ("Front differential fluid", "under_vehicle", "status", "", [], [], {},
     "never", False, True, ""),
    ("Rear differential fluid", "under_vehicle", "status", "", [], [], {},
     "never", False, True, "Rear-drive and four-wheel drive. Check the breather "
     "too — a blocked one pushes fluid past the seals."),

    ("Suspension and steering", "suspension_steering", "status", "", [], [], {},
     "on_attention", True, False, ""),
    ("Exhaust condition", "exhaust_emissions", "status", "", [], [], {},
     "on_attention", True, False, ""),
]

SEASONAL = [
    ("Coolant freeze protection", "under_hood", "measurement", "°F", [], [],
     {"fail": {"gt": 0}, "attention": {"gt": -20}, "pass": {"lte": -20}},
     "never", False, False, "Test with a refractometer, not a floating-ball tester."),
    ("Washer fluid", "under_hood", "status", "", [], [], {}, "never", False, False,
     "Winter mix. Summer fluid freezes in the lines and splits the reservoir."),
    ("Battery voltage", "under_hood", "measurement", "V", [], [],
     {"fail": {"lt": 12.0}, "attention": {"lt": 12.4}, "pass": {"gte": 12.4}},
     "never", False, False, "Cold weather finds a weak battery before you do."),
    ("Tire tread depth", "tires_wheels", "measurement", "/32in", ["LF", "RF", "LR", "RR"], [],
     {"fail": {"lte": 2}, "attention": {"lte": 5}, "pass": {"gt": 5}},
     "never", True, False, "Winter traction wants more tread than the legal minimum."),
    ("Wiper blades and washer fluid", "body_glass", "status", "", [], [], {},
     "never", False, False, ""),
    ("Heater and defroster", "interior", "status", "", [], [], {}, "never", True, False, ""),
    ("Emergency kit", "interior", "status", "", [], [], {}, "never", False, False,
     "Blanket, light, jumper leads, scraper."),
]

POST_REPAIR = [
    ("Fasteners torqued", "under_vehicle", "status", "", [], [], {}, "never", True, False,
     "Everything disturbed, checked and marked."),
    ("Fluid levels after work", "under_hood", "status", "", [], [], {}, "never", True, False,
     "Everything you opened, and everything you did not — a fluid that is low "
     "now and was not before is a leak you just made."),
    ("No leaks after run-up", "under_vehicle", "status", "", [], [], {},
     "on_attention", True, False, ""),
    ("Road test — the original complaint", "road_test", "status", "", [], [], {},
     "never", True, False, "Confirm the thing you were asked to fix is actually fixed."),
    ("Warning lights clear", "interior", "status", "", [], [], {}, "never", True, False, ""),
    ("Tools and parts accounted for", "under_hood", "status", "", [], [], {},
     "never", True, False, "Nothing left on the engine, nothing left under the car."),
]

TEMPLATES = {
    "ppi": ("Pre-purchase inspection", ["vehicle"], [],
            "Walk a vehicle you are considering buying. Designed to be completed "
            "offline, in someone else's driveway, in about 30 minutes.", PPI),
    "annual-safety": ("Annual safety check", ["vehicle"], [],
                      "The yearly once-over on a vehicle you already own.", ANNUAL),
    "winter-prep": ("Seasonal / winter prep", ["vehicle"], [],
                    "Before the weather turns.", SEASONAL),
    "post-repair": ("Post-repair quality check", ["vehicle"], [],
                    "Before you hand the keys back or call the job done.", POST_REPAIR),
}


def install(*, refresh: bool = False) -> int:
    """Create or refresh the built-in inspection templates. Idempotent."""
    from .models import InspectionPoint, InspectionTemplate

    count = 0
    for slug, (name, kinds, classes, description, points) in TEMPLATES.items():
        template, _created = InspectionTemplate.objects.update_or_create(
            slug=slug,
            defaults={
                "name": name,
                "description": description,
                "asset_kinds": kinds,
                "vehicle_classes": classes,
                "source": InspectionTemplate.Source.BUILTIN,
            },
        )
        count += 1
        if refresh:
            template.points.all().delete()
        for index, row in enumerate(points):
            (
                point_name, area, result_type, unit, positions,
                sub_positions, thresholds, photo, safety, optional, guidance,
            ) = row
            InspectionPoint.objects.update_or_create(
                template=template,
                name=point_name,
                defaults={
                    "area": area,
                    "sequence": index,
                    "result_type": result_type,
                    "measurement_unit": unit,
                    "positions": positions,
                    "sub_positions": sub_positions,
                    "thresholds": thresholds,
                    "photo_required": photo,
                    "is_safety_critical": safety,
                    "is_optional": optional,
                    "guidance": guidance,
                },
            )

        # Drop points this template no longer defines. Without this, a built-in
        # that gets re-filed or split leaves its old row behind forever and the
        # checklist grows a duplicate on every release. Only built-ins are
        # pruned, so a template someone has adapted is never touched.
        if template.source == InspectionTemplate.Source.BUILTIN:
            template.points.exclude(name__in=[row[0] for row in points]).delete()

    return count
