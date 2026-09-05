"""
Assets — vehicles and serviceable equipment (SPEC §6.2, §7.1, §7.1a).

One table, `asset_kind` distinguishing them. A mower and a truck share ~90% of
their behavior; splitting them would duplicate every relationship and query.
Vehicle-specific fields are nullable and gated by kind.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.measurements import to_canonical
from homeautoshop.core.models import (
    AppendOnlyModel,
    BaseModel,
    RevisionedModel,
    SoftDeleteQuerySet,
    alive_manager,
    uuid7,
)

from . import vin as vinlib


class AssetKind(models.TextChoices):
    VEHICLE = "vehicle", _("Vehicle")
    EQUIPMENT = "equipment", _("Equipment")


class VehicleClass(models.TextChoices):
    CAR = "car", _("Car")
    TRUCK = "truck", _("Truck")
    MOTORCYCLE = "motorcycle", _("Motorcycle")
    TRAILER = "trailer", _("Trailer")
    RV = "rv", _("RV")
    BUS = "bus", _("Bus")
    OTHER = "other_plated", _("Other plated")


class AssetStatus(models.TextChoices):
    PROSPECT = "prospect", _("Prospect")
    ACTIVE = "active", _("Active")
    PROJECT = "project", _("Project")
    STORED = "stored", _("Stored")
    SOLD = "sold", _("Sold")
    PARTED_OUT = "parted_out", _("Parted out")
    TOTALED = "totaled", _("Totaled")


class VinStatus(models.TextChoices):
    VALID = "valid", _("Validated")
    UNVALIDATED = "unvalidated", _("Not validated")
    PRE_1981 = "pre_1981", _("Pre-1981 format")
    NONE = "none", _("No VIN")


# Statuses excluded from fleet counts, dashboards and default lists. A prospect
# is not yours yet; a sold car is no longer yours. Both keep full history.
INACTIVE_STATUSES = (AssetStatus.SOLD, AssetStatus.PARTED_OUT, AssetStatus.TOTALED)


class AssetQuerySet(SoftDeleteQuerySet):
    def fleet(self):
        """Assets that count as yours today (excludes prospects and disposals)."""
        return self.exclude(status__in=[*INACTIVE_STATUSES, AssetStatus.PROSPECT])

    def vehicles(self):
        return self.filter(asset_kind=AssetKind.VEHICLE)

    def equipment(self):
        return self.filter(asset_kind=AssetKind.EQUIPMENT)


class Asset(RevisionedModel):
    """A vehicle or a piece of serviceable equipment."""

    nickname = models.CharField(
        max_length=120,
        help_text=_("What the household actually calls it — “the red truck”."),
    )
    asset_kind = models.CharField(max_length=16, choices=AssetKind.choices, default=AssetKind.VEHICLE)
    vehicle_class = models.CharField(max_length=16, choices=VehicleClass.choices, blank=True)
    status = models.CharField(
        max_length=16, choices=AssetStatus.choices, default=AssetStatus.ACTIVE, db_index=True
    )

    # Identity — vehicles
    vin = models.CharField(max_length=32, blank=True, db_index=True)
    vin_status = models.CharField(max_length=16, choices=VinStatus.choices, default=VinStatus.NONE)
    plate = models.CharField(max_length=16, blank=True)
    plate_region = models.CharField(max_length=8, blank=True, help_text=_("State or province."))
    plate_expires_on = models.DateField(null=True, blank=True)
    title_state = models.CharField(max_length=32, blank=True)

    # Description
    year = models.PositiveIntegerField(null=True, blank=True)
    make = models.CharField(max_length=64, blank=True)
    model = models.CharField(max_length=64, blank=True)
    trim = models.CharField(max_length=64, blank=True)
    body_style = models.CharField(max_length=64, blank=True)
    engine = models.CharField(max_length=120, blank=True)
    fuel_type = models.CharField(max_length=32, blank=True)
    transmission = models.CharField(max_length=64, blank=True)
    drivetrain = models.CharField(max_length=32, blank=True)
    color_exterior = models.CharField(max_length=48, blank=True)

    # Identity — equipment
    manufacturer = models.CharField(max_length=64, blank=True)
    model_number = models.CharField(max_length=64, blank=True)
    serial_number = models.CharField(max_length=64, blank=True)

    # Meter
    meter = models.CharField(
        max_length=16,
        default="odometer",
        choices=[
            ("odometer", _("Odometer")),
            ("engine_hours", _("Engine hours")),
            ("cycles", _("Cycles")),
            ("none", _("No meter")),
        ],
    )
    meter_unit = models.CharField(max_length=8, default="mi")

    # Ownership economics
    acquired_on = models.DateField(null=True, blank=True)
    disposed_on = models.DateField(null=True, blank=True)

    notes = models.TextField(blank=True)
    primary_photo = models.ForeignKey(
        "mediafiles.Media", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    # Decode provenance (SPEC FR-VEH-3/4)
    decode_source = models.CharField(max_length=32, blank=True)
    decoded_at = models.DateTimeField(null=True, blank=True)
    decoded_raw = models.JSONField(default=dict, blank=True)
    field_overrides = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Fields the operator corrected; a re-decode must never clobber these."),
    )

    objects = alive_manager(AssetQuerySet)
    all_objects = models.Manager.from_queryset(AssetQuerySet)()

    class Meta:
        ordering = ["nickname"]
        indexes = [
            models.Index(fields=["asset_kind", "status"]),
            models.Index(fields=["status", "nickname"]),
        ]

    def __str__(self) -> str:
        return self.nickname

    # -- validation ------------------------------------------------------

    def clean(self):
        super().clean()
        if self.asset_kind == AssetKind.EQUIPMENT and (self.vin or self.plate):
            raise ValidationError(
                {"vin": _("Equipment does not carry a VIN or license plate.")}
            )
        if self.vin:
            # The year is what makes a length rule mean anything: a short VIN
            # on a 1978 truck is the format it shipped with, and the same
            # entry on a 2016 one is a typo.
            check = vinlib.validate(self.vin, year=self.year)
            if check.errors:
                raise ValidationError({"vin": check.errors})

    def save(self, *args, **kwargs):
        self.vin = vinlib.normalize(self.vin)
        if self.vin:
            self.vin_status = vinlib.validate(self.vin, year=self.year).status
        else:
            # This used to preserve `pre_1981` across a cleared VIN, which was
            # harmless only because nothing ever set it. Now that a short VIN
            # does, clearing one would leave a record with no VIN still
            # claiming a VIN format.
            self.vin_status = VinStatus.NONE
        if self.asset_kind == AssetKind.EQUIPMENT:
            self.vehicle_class = ""
            if self.meter == "odometer":
                self.meter = "engine_hours"
                self.meter_unit = "hours"
        return super().save(*args, **kwargs)

    # -- presentation ----------------------------------------------------

    @property
    def descriptor(self) -> str:
        """Year make model, or the equipment equivalent."""
        if self.asset_kind == AssetKind.EQUIPMENT:
            parts = [self.manufacturer, self.model_number]
        else:
            parts = [str(self.year) if self.year else "", self.make, self.model, self.trim]
        return " ".join(p for p in parts if p).strip()

    @property
    def masked_vin(self) -> str:
        return vinlib.mask(self.vin)

    def decoded_details(self):
        """The rest of what the VIN decode returned, grouped for display.

        Eight fields are mapped onto columns; a typical decode populates thirty
        or more. The remainder has always been stored in `decoded_raw` and
        never shown, which made the lookup look thinner than it is.
        """
        from . import vpic_fields

        return vpic_fields.details(self.decoded_raw)

    @property
    def is_vehicle(self) -> bool:
        return self.asset_kind == AssetKind.VEHICLE

    @property
    def has_meter(self) -> bool:
        return self.meter != "none"

    @property
    def in_fleet(self) -> bool:
        return self.status not in [*INACTIVE_STATUSES, AssetStatus.PROSPECT]

    def is_overridden(self, field_name: str) -> bool:
        return field_name in (self.field_overrides or {})

    # -- meter -----------------------------------------------------------

    def latest_reading(self):
        return self.usage_readings.order_by("-read_on", "-created_at").first()

    @property
    def current_usage(self):
        reading = self.latest_reading()
        return reading.value if reading else None

    def current_owner(self):
        row = self.ownerships.filter(to_date__isnull=True, role="owner").select_related("person").first()
        return row.person if row else None


class AssetOwnership(BaseModel):
    """Ownership is history, not a foreign key (SPEC §6.2).

    Cars change hands, and the service record must stay correct about who owned
    it when.
    """

    class Role(models.TextChoices):
        OWNER = "owner", _("Owner")
        CO_OWNER = "co_owner", _("Co-owner")
        DRIVER = "primary_driver", _("Primary driver")

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="ownerships")
    person = models.ForeignKey("people.Person", on_delete=models.PROTECT, related_name="ownerships")
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.OWNER)
    from_date = models.DateField(default=timezone.localdate)
    to_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-from_date"]
        verbose_name_plural = "asset ownerships"

    def __str__(self) -> str:
        return f"{self.person} — {self.asset} ({self.get_role_display()})"

    @property
    def is_current(self) -> bool:
        return self.to_date is None


class UsageReading(AppendOnlyModel):
    """A meter observation — odometer, engine hours, or cycles.

    Append-only, so a reading captured in the garage can never be lost to a
    sync conflict (SPEC §5.4, P-5).
    """

    class Source(models.TextChoices):
        MANUAL = "manual", _("Entered by hand")
        WORK_ORDER = "work_order", _("From a work order")
        OBD = "obd", _("Scan tool")
        DOCUMENT = "document", _("From a document")
        IMPORT = "import", _("Imported")

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="usage_readings")
    meter = models.CharField(max_length=16, default="odometer")
    value = models.DecimalField(max_digits=12, decimal_places=2)
    unit = models.CharField(max_length=8, default="mi")
    value_canonical = models.DecimalField(max_digits=14, decimal_places=4, editable=False)
    read_on = models.DateField(default=timezone.localdate, db_index=True)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.MANUAL)
    note = models.TextField(blank=True)
    is_rollback = models.BooleanField(
        default=False,
        help_text=_("Set when the reading decreases — allowed, but it needs an explanation."),
    )

    class Meta:
        ordering = ["-read_on", "-created_at"]
        indexes = [models.Index(fields=["asset", "-read_on"])]

    def __str__(self) -> str:
        return f"{self.value} {self.unit} on {self.read_on}"

    def clean(self):
        super().clean()
        # A decrease is allowed but flagged: cluster swaps, meter replacements
        # and rollbacks are all real (SPEC FR-VEH-9). We require a reason
        # rather than refusing the reading.
        if self.is_rollback and not self.note.strip():
            raise ValidationError({"note": _("A decreasing reading needs a note explaining why.")})

    def save(self, *args, **kwargs):
        if self.unit in ("hours", "cycles"):
            self.value_canonical = Decimal(str(self.value))
        else:
            self.value_canonical = to_canonical(self.value, self.unit)
        if self._state.adding:
            previous = (
                UsageReading.objects.filter(asset_id=self.asset_id, meter=self.meter)
                .exclude(pk=self.pk)
                .order_by("-read_on", "-created_at")
                .first()
            )
            if previous and self.value_canonical < previous.value_canonical:
                self.is_rollback = True
        return super().save(*args, **kwargs)


class ServiceInfoProvider(BaseModel):
    """A service-manual library to link out to (SPEC §8.5).

    HomeAutoShop never hosts, mirrors, scrapes, or crawls these. It renders a
    link for a human to click, and makes it one tap from the work order.
    """

    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(max_length=40, unique=True)
    base_urls = models.JSONField(
        default=list, help_text=_("Ordered mirror list; the first reachable one wins.")
    )
    url_template = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Tokens: {make} {year} {model}. Only as deep as the pattern is reliable."),
    )
    #: Appended to a pinned URL's vehicle root to reach that library's index of
    #: diagnostic trouble codes. See `service_info.dtc_url` for why this can be
    #: derived at all, and for what it does not promise.
    dtc_path = models.CharField(
        max_length=300,
        blank=True,
        help_text=_("Path from the vehicle root to this library's DTC index, if it has one."),
    )
    deep_link_depth = models.CharField(
        max_length=16,
        default="make_year",
        choices=[
            ("root", _("Site root only")),
            ("make", _("Make")),
            ("make_year", _("Make and year")),
        ],
    )
    access = models.CharField(
        max_length=16,
        default="free",
        choices=[
            ("free", _("Free, no sign-up")),
            ("paid", _("Paid subscription")),
            ("account", _("Account required")),
        ],
    )
    notes = models.TextField(blank=True)
    is_enabled = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=100)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name

    def browse_url(self, asset: Asset) -> str:
        """Deep-link only as far as the pattern is deterministic.

        These libraries index by a catalog string that fuses model, trim,
        body and engine — it cannot be derived from a VIN decode, so generating
        it would produce dead links. We land the operator on the shortlist and
        let them pin the real URL (SPEC §8.5).
        """
        base = (self.base_urls or [""])[0].rstrip("/")
        if not base:
            return ""
        if self.deep_link_depth == "root" or not self.url_template:
            return base
        make = (asset.make or "").strip()
        if not make:
            return base
        if self.deep_link_depth == "make":
            return f"{base}/{make}/"
        if asset.year:
            return f"{base}/{make}/{asset.year}/"
        return f"{base}/{make}/"


class AssetServiceInfoLink(BaseModel):
    """A resolved, pinned service-manual URL for one asset.

    Resolve once, pin forever — see ServiceInfoProvider.browse_url for why
    generating these is not possible.
    """

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="service_info_links")
    provider = models.ForeignKey(ServiceInfoProvider, on_delete=models.CASCADE, related_name="links")
    #: Blank on a row that exists only to hide the provider for this vehicle.
    url = models.URLField(max_length=500, blank=True)
    label = models.CharField(max_length=120, blank=True)
    is_hidden = models.BooleanField(
        default=False,
        help_text=_(
            "Hidden for this vehicle. CHARM has no entry for a 2025 Crosstrek and never "
            "will, so a permanent empty box is worse than no box (OQ-11)."
        ),
    )
    subscription_status = models.CharField(
        max_length=20,
        default="unknown",
        choices=[
            ("subscribed", _("Subscribed")),
            ("not_subscribed", _("Not subscribed")),
            ("unknown", _("Unknown")),
        ],
        help_text=_("ALLDATA DIY is sold per vehicle, so this is per asset."),
    )
    subscription_expires_on = models.DateField(null=True, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["provider__sort_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["asset", "provider"],
                name="unique_asset_provider_link",
                condition=models.Q(deleted_at__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider} — {self.asset}"

    @property
    def is_pinned(self) -> bool:
        return bool(self.url)


class SpecGroup(models.TextChoices):
    FLUIDS = "fluids", _("Fluids and capacities")
    TORQUE = "torque", _("Torque specs")
    TIRES = "tires", _("Tires and wheels")
    ELECTRICAL = "electrical", _("Electrical")
    ALIGNMENT = "alignment", _("Alignment")
    FILTERS = "filters", _("Filters and part numbers")
    ACCESS = "access", _("Keys and access codes")
    OTHER = "other", _("Other")


# Key codes, radio codes, alarm PINs and wheel-lock key locations are exactly
# what this is for — and exactly what must never land in a service history PDF
# handed to a buyer, or in a shared export (SPEC §18 C-5).
SENSITIVE_BY_DEFAULT = (SpecGroup.ACCESS,)


class AssetSpec(RevisionedModel):
    """Reference values you look up mid-job (SPEC §7.9, FR-SPEC-1..4).

    Free-form key/value on purpose: no fixed schema covers a 1968 Mustang and a
    2024 EV. These are looked up far more often than any report is run, and the
    answer is usually buried in a manual PDF that takes three minutes to find.
    """

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="specs")
    group = models.CharField(max_length=16, choices=SpecGroup.choices, default=SpecGroup.OTHER)
    name = models.CharField(max_length=120)
    value = models.CharField(max_length=200)
    #: The other end, when the spec is a range rather than a figure. Plenty are:
    #: a refrigerant charge is 0.50–0.55 kg, a cold tire pressure is 32–35 psi,
    #: a valve lash is a window. Typed into `value` as "0.50-0.55" it reads
    #: correctly and compares to nothing, so a range stored as text is a range
    #: nothing can ever check a measurement against.
    value_max = models.CharField(
        max_length=200,
        blank=True,
        verbose_name=_("to"),
        help_text=_("Only for a spec that is a range. Leave blank for a single figure."),
    )
    unit = models.CharField(max_length=24, blank=True)
    condition = models.CharField(
        max_length=120, blank=True, help_text=_("e.g. “cold, curb weight” — a spec without its condition is a guess.")
    )
    source = models.CharField(
        max_length=16,
        default="manual",
        choices=[
            ("manual", _("Owner's manual")),
            ("oem_doc", _("OEM document")),
            ("measured", _("Measured")),
            ("decoded", _("From a VIN decode")),
            ("scan_tool", _("Read from a scan tool")),
            ("other", _("Other")),
        ],
    )
    source_media = models.ForeignKey(
        "mediafiles.Media", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text=_("The page it came from (FR-SPEC-2)."),
    )
    is_sensitive = models.BooleanField(
        default=False,
        help_text=_("Hidden from reports and shared exports. Key and radio codes belong here."),
    )
    is_pinned = models.BooleanField(
        default=False, help_text=_("Show in the quick-reference panel on work orders.")
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["group", "name"]
        indexes = [models.Index(fields=["asset", "group"])]
        constraints = [
            models.UniqueConstraint(
                fields=["asset", "group", "name", "condition"],
                name="unique_asset_spec",
                condition=models.Q(deleted_at__isnull=True),
            )
        ]

    @property
    def is_range(self) -> bool:
        return bool(self.value_max)

    def __str__(self) -> str:
        return f"{self.name}: {self.display_value}"

    @property
    def display_value(self) -> str:
        """The figure or the range, with its unit and condition.

        One place, because a range formatted by hand at each call site is one
        call site away from printing `0.50 kg` with the top of it missing.
        """
        span = f"{self.value}–{self.value_max}" if self.value_max else self.value
        bits = [span, self.unit]
        text = " ".join(b for b in bits if b)
        return f"{text} ({self.condition})" if self.condition else text

    def save(self, *args, **kwargs):
        if self._state.adding and self.group in SENSITIVE_BY_DEFAULT and not self.is_sensitive:
            # Default the security-adjacent group to sensitive rather than
            # relying on the operator to remember.
            self.is_sensitive = True
        return super().save(*args, **kwargs)


class Recall(BaseModel):
    """A safety campaign that may apply to this vehicle (SPEC §8.4).

    The free NHTSA API is queried by year/make/model, **not** by VIN, and
    VIN-level completion status is not available from it. So these are
    campaigns that *may* apply, and `owner_status` is always operator-
    maintained — presenting a scraped guess as "your recall status" would be
    worse than useless on a safety matter.
    """

    class OwnerStatus(models.TextChoices):
        OPEN = "open", _("Not checked")
        SCHEDULED = "scheduled", _("Scheduled")
        COMPLETED = "completed", _("Done")
        NOT_APPLICABLE = "not_applicable", _("Does not apply to this VIN")

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="recalls")
    campaign_number = models.CharField(max_length=48, blank=True)
    nhtsa_id = models.CharField(max_length=48, blank=True)
    reported_on = models.DateField(null=True, blank=True)
    component = models.CharField(max_length=200, blank=True)
    summary = models.TextField(blank=True)
    consequence = models.TextField(blank=True)
    remedy = models.TextField(blank=True)
    source = models.CharField(
        max_length=12,
        default="nhtsa",
        choices=[("nhtsa", _("NHTSA")), ("manual", _("Entered by hand"))],
    )
    owner_status = models.CharField(
        max_length=16, choices=OwnerStatus.choices, default=OwnerStatus.OPEN
    )
    completed_on = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-reported_on", "campaign_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["asset", "campaign_number"],
                name="unique_asset_recall",
                condition=models.Q(deleted_at__isnull=True),
            )
        ]

    def __str__(self) -> str:
        return f"{self.campaign_number} — {self.component}"[:120]

    @property
    def needs_attention(self) -> bool:
        return self.owner_status in (self.OwnerStatus.OPEN, self.OwnerStatus.SCHEDULED)


class AssetLink(BaseModel):
    """A page on the internet that is about this particular vehicle.

    The forum thread where somebody solved the exact fault, the wiring diagram
    that took an hour to find, the video of the timing-belt job on this engine,
    the listing it was bought from. All of it is knowledge that took work to
    locate and that is otherwise kept in a browser bookmark folder nobody else
    in the household can reach.

    **Distinct from `AssetServiceInfoLink` on purpose.** That one pins a
    *provider's* page — one row per configured provider, resolved once because
    those libraries index by a catalog string no VIN yields (§8.5). This is the
    open-ended other half: any address, no provider, as many as you like. They
    look similar on screen and they answer different questions — "where is the
    manual for this" against "what else did I find out about this".

    Nothing is ever fetched. A link is stored and displayed; the shop does not
    reach out to it, so no allowlist and no offline-mode question arises.
    """

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="links")
    url = models.URLField(max_length=500)
    label = models.CharField(max_length=120, blank=True)
    notes = models.CharField(
        max_length=300,
        blank=True,
        help_text=_("Why this is worth keeping — the thing you would forget."),
    )

    class Meta:
        ordering = ["label", "-created_at"]

    def __str__(self) -> str:
        return self.label or self.url

    @property
    def display_label(self) -> str:
        """Something to click on, even when nobody typed a label.

        The bare URL is a poor label and a truncated one is worse, so the host
        stands in: `www.ford-trucks.com` says more about where a link goes than
        the first forty characters of a query string ever do.
        """
        if self.label:
            return self.label
        from urllib.parse import urlparse

        return urlparse(self.url).netloc or self.url


class AssetCardPreference(models.Model):
    """How one person wants one vehicle to look on the board (SPEC §9.3).

    Per user, not per shop. Order, color and pins are answers to "which of
    these is the one I mean", and that is a question each person answers
    differently: the person who drives the truck every day wants it first, and
    the person who only ever services the mower does not.

    Not a `BaseModel`. Provenance and a thirty-day trash are for records of
    work; this is a display preference, and a soft-deleted one — a row that
    exists, is invisible, and blocks the unique constraint on the row that
    would replace it — would be a bug with no upside. Deleting one means the
    card goes back to its default, which is exactly what the absent row means.

    Every field is optional in effect: no row at all is a valid, complete
    answer, and it is the state every card starts in.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="asset_cards"
    )
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="card_preferences")

    #: Position on this person's board. `NULL` means unplaced — a vehicle added
    #: since they last arranged anything — and unplaced cards sort after placed
    #: ones, alphabetically, so a new vehicle appears in a predictable spot
    #: rather than being silently inserted into the middle of somebody's board.
    board_order = models.PositiveIntegerField(null=True, blank=True)
    color = models.CharField(max_length=16, blank=True)
    #: Pin keys from `cards.PINS`, in that module's order.
    #:
    #: `NULL`, not `[]`, when nobody has chosen — and the difference is the
    #: whole field. An empty list is a real choice ("show me nothing but the
    #: nickname") and is honored; `NULL` takes `cards.DEFAULT_PINS`. With
    #: `default=list` the two were the same value, so the first time anybody
    #: dragged a card, `ensure_placed` wrote a row to hold its position and
    #: every card on the board went blank.
    pins = models.JSONField(null=True, blank=True, default=None)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["board_order"]
        constraints = [
            models.UniqueConstraint(fields=["user", "asset"], name="unique_asset_card_preference"),
        ]
        indexes = [models.Index(fields=["user", "board_order"])]

    def __str__(self) -> str:
        return f"{self.user} · {self.asset}"
