# Inspection Template Schema — DVI

|  |  |
| --- | --- |
| **Document** | `Artifacts/SCHEMA-INSPECTION-TEMPLATES.md` |
| **Status** | Draft for review |
| **Version** | 0.1.0 |
| **Date** | 2026-08-29 |
| **Parent spec** | [SPEC.md](SPEC.md) |
| **Implements** | SPEC.md §7.8 (`FR-DVI-1`–`FR-DVI-13`), entities `inspection_template` / `inspection_point` |

---

## 1. The contract

Inspection templates are data, like parser profiles. Importable and exportable as YAML (SPEC `FR-DVI-13`), versioned, and **snapshotted onto every inspection** so edits never rewrite history (`FR-DVI-6`).

```yaml
# templates/pre-purchase-inspection.yaml
name: Pre-purchase inspection
translation_key: template.ppi
description: >
  Walk a vehicle you are considering buying. Designed to be completed
  offline, in someone else's driveway, in about 30 minutes.
vehicle_classes: [car, truck, rv]
version: 1

points:
  # --- a positional measurement with thresholds -------------------------
  - area: tires_wheels
    name: Tire tread depth
    translation_key: point.tire_tread
    guidance: >
      Measure at outer, center, and inner across each tire. A large
      spread across one tire means alignment or inflation problems,
      not just wear.
    result_type: measurement
    measurement_unit: /32in          # locale-formatted per §5.6
    positions: [LF, RF, LR, RR]
    sub_positions: [outer, center, inner]
    thresholds:
      fail:      { lte: 2 }          # legal minimum in most US states
      attention: { lte: 4 }          # plan replacement
      pass:      { gt: 4 }
    photo_required: on_attention
    is_safety_critical: true

  # --- a pure status point ---------------------------------------------
  - area: tires_wheels
    name: Tire DOT date code
    translation_key: point.tire_dot
    guidance: >
      Four digits: week and year of manufacture. Over 6 years is
      attention regardless of tread; over 10 is a fail regardless of
      tread. Record the code in the note.
    result_type: status
    positions: [LF, RF, LR, RR]
    photo_required: always           # the sidewall stamp is the evidence
    is_safety_critical: true

  # --- measurement + status together ------------------------------------
  - area: brakes
    name: Brake pad thickness
    translation_key: point.brake_pad
    result_type: both
    measurement_unit: mm
    positions: [LF, RF, LR, RR]
    sub_positions: [inner, outer]
    thresholds:
      fail:      { lte: 3 }
      attention: { lte: 5 }
    photo_required: on_attention
    is_safety_critical: true

  - area: under_vehicle
    name: Frame and rocker corrosion
    translation_key: point.corrosion
    guidance: >
      Structural rust is the single most common reason to walk away.
      Photograph anything scaling or perforated.
    result_type: status
    photo_required: on_attention
    is_safety_critical: true

  - area: road_test
    name: Cold start behavior
    translation_key: point.cold_start
    guidance: >
      Insist on a genuinely cold start. A seller who has warmed the car
      up before you arrive is worth a note of its own.
    result_type: status
    photo_required: never

  - area: fluids
    name: Engine oil condition
    translation_key: point.oil_condition
    result_type: status
    guidance: Check level, color, and any coolant contamination.
    photo_required: on_attention
```

## 2. Threshold evaluation

`thresholds` are evaluated most-severe-first (`fail`, then `attention`, then `pass`); the first match wins and is written to `auto_status`. Operators for numeric comparison are `lt`, `lte`, `gt`, `gte`, and `between`. A point with no thresholds simply has no `auto_status` and is scored by the human.

The computed value always lands in `auto_status`, and the human's answer in `status`. **The two are stored separately and never collapsed** (FR-DVI-4) — that disagreement is the most interesting thing on the record, because it is where judgment overrode a rule, and next year the reason will not be obvious.
