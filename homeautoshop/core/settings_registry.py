"""
The instance settings registry (SPEC §17, R-9, §17.1, §17.2).

Until now every choice in §14 lived in a `.env` file, which put renaming the
shop behind a text editor and a container restart, and put the Offline Mode
kill switch — an *emergency* control (NFR-S-2) — out of reach of the person
holding the emergency. §17.1 divides the configuration three ways, and this
module is the middle third: the settings that move into the application.

**Why a registry rather than a form.** §17.1 is explicit that a free-text
`setting` row with no schema is a way to brick an instance by typo. So every
entry declares its type, its bounds, its choices and its wording here, in one
place, and the form, the validation, the audit summary and the API all read
from it. Adding a setting is adding a row to this table and nothing else.

**Why `applies` is a field and not a comment.** Settings are read at import
time by Django itself for a handful of entries — the locale set, upload
ceilings, the mail backend — and no amount of storing them in a database
changes that. Rather than let someone discover this by changing the timezone
and watching nothing happen, each entry says which it is, and §17.2's banner
holds the instance to it.

**What is deliberately absent.** §14 also lists `SCAN_IMPORT_ENABLED` and
`EQUIPMENT_ENABLED`. Nothing in the codebase reads either, and a settings
screen showing a switch that does nothing is worse than one that does not show
it — it is a promise the instance cannot keep. They arrive here when they gate
something. (`RECALLS_ENABLED` and `SERVICE_INFO_ENABLED` were in the same state
and are now wired, because each gates one existing feature and the gate is
three lines.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

#: Takes effect on save.
IMMEDIATE = "immediate"
#: Django resolved this once at startup; only a restart moves it (§17.2).
RESTART = "restart"


@dataclass(frozen=True)
class Group:
    key: str
    label: Any
    blurb: Any = ""


GROUPS: tuple[Group, ...] = (
    Group("shop", _("Shop"), _("Naming, locale and the units everything is shown in.")),
    Group("costs", _("Costs"), _("What counts toward what a vehicle has cost you.")),
    Group(
        "maintenance",
        _("Maintenance"),
        _("How far ahead something counts as due, and whether you are told about it."),
    ),
    Group("media", _("Photos and documents"), _("Uploads, privacy, and text recognition.")),
    Group(
        "outbound",
        _("Outbound requests"),
        _("Everything this instance may ask the internet, and the switch that stops all of it."),
    ),
    Group("integrations", _("Integrations"), _("Other systems this shop talks to.")),
    Group(
        "email",
        _("Outgoing email"),
        _("Only used to deliver reminders. Leave the server blank and no email is sent."),
    ),
    Group("backup", _("Backup"), _("How often, how many are kept, and when to warn you.")),
)


@dataclass(frozen=True)
class Entry:
    """One configurable value.

    `key` is the Django settings attribute, so the environment default already
    computed in `config/settings.py` stays the fallback and precedence comes
    out as **database → environment → default** with no second copy of the
    defaults to drift.
    """

    key: str
    group: str
    kind: str  # bool | int | money | str | choice | secret
    label: Any
    help: Any = ""
    applies: str = IMMEDIATE
    choices: tuple[tuple[str, Any], ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    unit: Any = ""
    placeholder: str = ""
    #: The key this one is meaningless without — a plate-lookup URL under the
    #: plate-lookup switch, a reminder cooldown under the reminders switch. The
    #: screen hides a child while its parent is off, because a form full of
    #: fields that currently do nothing is a form nobody can read.
    depends_on: str = ""
    #: Extra checking beyond type and range. Raises ValidationError.
    check: Callable[[Any], None] | None = field(default=None, compare=False)

    @property
    def is_secret(self) -> bool:
        return self.kind == "secret"


def _a_url(value: str) -> None:
    from urllib.parse import urlparse

    if not value:
        return
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValidationError(_("Enter a full address, starting with http:// or https://."))


def _a_currency(value: str) -> None:
    if value and (len(value) != 3 or not value.isalpha()):
        raise ValidationError(_("Use a three-letter ISO 4217 code, such as USD."))


def _a_timezone(value: str) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError(
            _("%(value)s is not a known timezone. Use a name like America/Chicago.")
            % {"value": value}
        ) from None


def timezone_choices() -> tuple[tuple[str, str], ...]:
    """Every zone this machine's tzdata knows, for a real picker.

    Typed into a text box, this is the setting a typo corrupts most quietly:
    `America/Chicargo` is refused now, but `America/Indiana/Knox` versus
    `America/Indianapolis` is the kind of thing nobody should have to spell from
    memory. Read from `zoneinfo` rather than hard-coded so it follows the
    tzdata in the image instead of drifting from it.
    """
    from zoneinfo import available_timezones

    names = sorted(available_timezones())
    # Legacy single-word aliases (`EST5EDT`, `Zulu`, `Cuba`) are still resolvable
    # and are not what anybody is looking for in a list of six hundred. `UTC` is
    # put back because it is the default and has to be selectable.
    regional = [name for name in names if "/" in name]
    return tuple((name, name.replace("_", " ")) for name in ["UTC", *regional])


def _ocr_languages(value: str) -> None:
    for lang in value.replace(",", " ").split():
        if not (lang.isalpha() and len(lang) == 3):
            raise ValidationError(
                _("Tesseract language codes are three letters, such as eng or fra.")
            )


UNITS_CHOICES = (("imperial", _("Imperial — miles, gallons, °F")), ("metric", _("Metric — kilometres, litres, °C")))

LUBELOGGER_MODES = (
    ("off", _("Off")),
    ("import_once", _("One-time import only")),
    ("pull", _("Pull changes on a schedule")),
    ("pull_push_odometer", _("Pull changes, and push odometer readings back")),
)

CONSUMABLES_OWNERS = (
    ("homeautoshop", _("Here — HomeAutoShop tracks the oil and the shop rags")),
    ("split", _("Split — each system tracks what it stocks")),
    ("wrenchledger", _("WrenchLedger — it owns consumables, this shop only uses them")),
)

SERVICE_INFO_PROVIDERS = (
    ("lemon", "LEMON"),
    ("charm", "CHARM"),
    ("alldata", "ALLDATA DIY"),
)


REGISTRY: tuple[Entry, ...] = (
    # ---------------------------------------------------------------- shop
    Entry(
        "SHOP_NAME", "shop", "str",
        _("Shop name"),
        _("Shown in the header, on reports, and in reminder emails."),
    ),
    Entry(
        "LANGUAGE_CODE", "shop", "choice",
        _("Default language"),
        _("Each person can still choose their own; this is what a new account starts with."),
        applies=RESTART,
        choices=(
            ("en-us", "English (United States)"),
            ("en-ca", "English (Canada)"),
            ("fr-ca", "français (Canada)"),
            ("es-mx", "español (México)"),
        ),
    ),
    Entry(
        "TIME_ZONE", "shop", "choice",
        _("Timezone"),
        _("What times are displayed in. They are stored in UTC either way."),
        applies=RESTART,
        choices=timezone_choices(),
        check=_a_timezone,
    ),
    Entry(
        "UNITS", "shop", "choice",
        _("Units"),
        _("What distances and volumes are shown in. Each person can override it."),
        choices=UNITS_CHOICES,
    ),
    Entry(
        "CURRENCY_REPORTING", "shop", "str",
        _("Reporting currency"),
        _(
            "Totals across vehicles are shown in this. Individual purchases keep the "
            "currency they were made in and the rate at the time."
        ),
        placeholder="USD",
        check=_a_currency,
    ),
    # --------------------------------------------------------------- costs
    Entry(
        "COST_INCLUDE_TOOLING", "costs", "bool",
        _("Count tools toward vehicle cost"),
        _("Off by default: a torque wrench you keep is not a cost of the car you bought it for."),
    ),
    Entry(
        "LABOR_RATE_MINOR", "costs", "money",
        _("Value your own time at"),
        _("Per hour. Zero hides labor value entirely."),
        minimum=0,
        maximum=100_000_000,
    ),
    # --------------------------------------------------------- maintenance
    Entry(
        "DUE_SOON_DAYS", "maintenance", "int",
        _("Call something due this many days ahead"),
        minimum=1, maximum=3650, unit=_("days"),
    ),
    Entry(
        "DUE_SOON_DISTANCE", "maintenance", "int",
        _("…or this far ahead on the odometer"),
        minimum=1, maximum=100_000, unit=_("miles or kilometres, matching your units"),
    ),
    Entry(
        "DEFAULT_DISTANCE_PER_DAY", "maintenance", "int",
        _("Assume this much driving per day"),
        _("Only used for a vehicle with too little history to work out its own rate."),
        minimum=1, maximum=2000, unit=_("miles or kilometres per day"),
    ),
    Entry(
        "REMINDERS_ENABLED", "maintenance", "bool",
        _("Send reminders"),
        _(
            "A channel must also be created and enabled. Nothing is ever sent when there "
            "is nothing to say — there is no all-clear message."
        ),
    ),
    Entry(
        "REMINDER_COOLDOWN_DAYS", "maintenance", "int",
        _("Do not repeat the same reminder for"),
        _("Daily nagging about something you have decided to live with is how reminders get muted."),
        minimum=1, maximum=365, unit=_("days"),
        depends_on="REMINDERS_ENABLED",
    ),
    Entry(
        "DVI_ENABLED", "maintenance", "bool",
        _("Inspections"),
        _("The walk-around checklist and its photo evidence."),
    ),
    # --------------------------------------------------------------- media
    Entry(
        "MAX_UPLOAD_MB", "media", "int",
        _("Largest file that may be uploaded"),
        applies=RESTART,
        minimum=1, maximum=2048, unit=_("MB"),
    ),
    Entry(
        "STRIP_GPS_EXIF", "media", "bool",
        _("Remove location data from photos"),
        _("A photo of a car on the driveway carries the home address in it."),
    ),
    Entry(
        "OCR_ENABLED", "media", "bool",
        _("Read text out of documents and receipts"),
        _(
            "Runs on this machine — nothing is sent anywhere. Turning it off leaves files "
            "waiting rather than failed, so switching it back on catches them up."
        ),
    ),
    Entry(
        "OCR_LANGUAGES", "media", "str",
        _("Languages to read"),
        _(
            "Tesseract codes such as eng fra spa. A language must also be installed in the "
            "image — set TESSERACT_LANGS and rebuild. The health page shows which are present."
        ),
        placeholder="eng",
        check=_ocr_languages,
        depends_on="OCR_ENABLED",
    ),
    Entry(
        "OCR_PDF_MAX_PAGES", "media", "int",
        _("Read at most this many pages of a scanned PDF"),
        _("A receipt is one page; a service manual is hundreds, and reading all of them helps nobody."),
        minimum=1, maximum=500, unit=_("pages"),
        depends_on="OCR_ENABLED",
    ),
    # ------------------------------------------------------------ outbound
    Entry(
        "OFFLINE_MODE", "outbound", "bool",
        _("Offline Mode"),
        _(
            "Stops every outbound request at once, whatever else is switched on. "
            "Everything in this application keeps working; the parts that ask the internet "
            "say so and let you type the answer in."
        ),
    ),
    Entry(
        "VIN_DECODE_ENABLED", "outbound", "bool",
        _("Decode VINs"),
        _("Asks the NHTSA vPIC database what a VIN means. Free, public, and no key needed."),
    ),
    Entry(
        "RECALLS_ENABLED", "outbound", "bool",
        _("Check for recalls"),
        _("Asks NHTSA whether a vehicle has open safety campaigns."),
    ),
    Entry(
        "SERVICE_INFO_ENABLED", "outbound", "bool",
        _("Service manual links"),
        _("Links out to a manual library. Nothing is fetched or crawled — these are links."),
    ),
    Entry(
        "SERVICE_INFO_DEFAULT", "outbound", "choice",
        _("Preferred manual library"),
        choices=SERVICE_INFO_PROVIDERS,
        depends_on="SERVICE_INFO_ENABLED",
    ),
    Entry(
        "SHOW_PRODUCT_LINKS", "outbound", "bool",
        _("Show suggested-product links"),
        _("Turning this off removes them entirely rather than hiding them."),
    ),
    Entry(
        "PLATE_LOOKUP_ENABLED", "outbound", "bool",
        _("Look up a VIN from a license plate"),
        _(
            "Off deliberately. There is no free, legal plate-to-VIN service, so every "
            "lookup costs money and sends a plate off this machine."
        ),
    ),
    Entry(
        "PLATE_LOOKUP_URL", "outbound", "str",
        _("Plate lookup address"),
        _("Your provider's endpoint. {plate} and {region} are filled in where they appear."),
        check=_a_url,
        depends_on="PLATE_LOOKUP_ENABLED",
    ),
    Entry(
        "PLATE_LOOKUP_KEY", "outbound", "secret",
        _("Plate lookup key"),
        _("Sent as a bearer token."),
        depends_on="PLATE_LOOKUP_ENABLED",
    ),
    Entry(
        "PLATE_LOOKUP_MONTHLY_CAP", "outbound", "int",
        _("Stop after this many lookups a month"),
        _("Zero means no cap — the difference between a mistake costing a dollar and a month's budget."),
        minimum=0, maximum=100_000, unit=_("lookups"),
        depends_on="PLATE_LOOKUP_ENABLED",
    ),
    Entry(
        "PLATE_LOOKUP_COST_MINOR", "outbound", "money",
        _("What one lookup costs you"),
        _("Your own estimate, shown before each lookup. No provider publishes a price list."),
        minimum=0, maximum=1_000_000,
        depends_on="PLATE_LOOKUP_ENABLED",
    ),
    # -------------------------------------------------------- integrations
    Entry(
        "CATALOG_URL", "integrations", "str",
        _("Shared template catalog"),
        _(
            "Where to look for schedule templates and parser profiles other people "
            "have published. Checked only when you press Browse — never in the "
            "background, and never on start-up."
        ),
        placeholder="https://raw.githubusercontent.com/EccentricWkshp/HomeAutoShop/main/catalog/",
        check=_a_url,
    ),
    Entry(
        "LUBELOGGER_URL", "integrations", "str",
        _("LubeLogger address"),
        _("Leave blank to disable. A LAN address is fine; it is allowed through automatically."),
        placeholder="https://lubelogger.home.arpa",
        check=_a_url,
    ),
    Entry(
        "LUBELOGGER_API_KEY", "integrations", "secret",
        _("LubeLogger API key"),
        _("Only if your instance requires one. Viewer scope is enough to pull."),
        depends_on="LUBELOGGER_URL",
    ),
    Entry(
        "LUBELOGGER_MODE", "integrations", "choice",
        _("What to do with LubeLogger"),
        choices=LUBELOGGER_MODES,
        depends_on="LUBELOGGER_URL",
    ),
    Entry(
        "LUBELOGGER_SYNC_HOURS", "integrations", "int",
        _("Pull from LubeLogger every"),
        minimum=1, maximum=720, unit=_("hours"),
        depends_on="LUBELOGGER_URL",
    ),
    Entry(
        "WRENCHLEDGER_API_KEY", "integrations", "secret",
        _("WrenchLedger API key"),
        _("Blank disables the integration. Read scopes are enough."),
    ),
    Entry(
        "WRENCHLEDGER_SYNC_HOURS", "integrations", "int",
        _("Pull tool availability every"),
        minimum=1, maximum=720, unit=_("hours"),
    ),
    Entry(
        "CONSUMABLES_OWNER", "integrations", "choice",
        _("Who owns shop consumables"),
        _("So a quart of oil is not counted in both systems."),
        choices=CONSUMABLES_OWNERS,
    ),
    # --------------------------------------------------------------- email
    # Reminders build their own SMTP connection from these rather than relying
    # on Django's module-level `EMAIL_BACKEND`, which is chosen once at import
    # and would make every one of them a restart.
    Entry(
        "EMAIL_HOST", "email", "str",
        _("SMTP server"),
        _("Blank turns email delivery off. Other reminder channels are unaffected."),
        placeholder="smtp.example.com",
    ),
    Entry(
        "EMAIL_PORT", "email", "int",
        _("Port"),
        minimum=1, maximum=65535,
        depends_on="EMAIL_HOST",
    ),
    Entry(
        "EMAIL_HOST_USER", "email", "str",
        _("Username"),
        depends_on="EMAIL_HOST",
    ),
    Entry(
        "EMAIL_HOST_PASSWORD", "email", "secret",
        _("Password"),
        _("Stored encrypted, never shown back, and excluded from backups and exports."),
        depends_on="EMAIL_HOST",
    ),
    Entry(
        "EMAIL_USE_TLS", "email", "bool",
        _("Use STARTTLS"),
        depends_on="EMAIL_HOST",
    ),
    Entry(
        "DEFAULT_FROM_EMAIL", "email", "str",
        _("Send from"),
        placeholder="homeautoshop@example.com",
        depends_on="EMAIL_HOST",
    ),
    # -------------------------------------------------------------- backup
    Entry(
        "BACKUP_INTERVAL_HOURS", "backup", "int",
        _("Back up every"),
        minimum=1, maximum=720, unit=_("hours"),
    ),
    Entry(
        "BACKUP_RETENTION_DAILY", "backup", "int",
        _("Keep this many daily backups"),
        minimum=0, maximum=365,
    ),
    Entry(
        "BACKUP_RETENTION_WEEKLY", "backup", "int",
        _("…and this many weekly"),
        minimum=0, maximum=520,
    ),
    Entry(
        "BACKUP_RETENTION_MONTHLY", "backup", "int",
        _("…and this many monthly"),
        minimum=0, maximum=120,
    ),
    Entry(
        "BACKUP_WARN_AFTER_DAYS", "backup", "int",
        _("Warn when the last backup is older than"),
        minimum=1, maximum=365, unit=_("days"),
    ),
)

BY_KEY: dict[str, Entry] = {entry.key: entry for entry in REGISTRY}

#: Keys held in `core_credential`, encrypted, and stripped from every artifact
#: this instance produces (§17.1).
SECRET_KEYS: frozenset[str] = frozenset(e.key for e in REGISTRY if e.is_secret) | {
    "EMAIL_HOST_PASSWORD"
}

RESTART_KEYS: frozenset[str] = frozenset(e.key for e in REGISTRY if e.applies == RESTART)


def children_of(key: str) -> list[Entry]:
    return [entry for entry in REGISTRY if entry.depends_on == key]


def entries_for(group: str) -> list[Entry]:
    return [entry for entry in REGISTRY if entry.group == group]


def settings_currency() -> str:
    """The currency amounts on the settings screen are written in."""
    from homeautoshop.core.runtime import conf

    return conf.CURRENCY_REPORTING or "USD"


def _within_range(entry: "Entry", value: int, render=str) -> int:
    """Bounds, reported in whatever units the reader typed."""
    if entry.minimum is not None and value < entry.minimum:
        raise ValidationError(
            _("The smallest value allowed is %(n)s.") % {"n": render(entry.minimum)}
        )
    if entry.maximum is not None and value > entry.maximum:
        raise ValidationError(
            _("The largest value allowed is %(n)s.") % {"n": render(entry.maximum)}
        )
    return value


def coerce(entry: Entry, raw) -> Any:
    """Turn a form value into the stored type, or explain why it cannot be.

    Every message here is one somebody reads while trying to fix something, so
    none of them is a type name.
    """
    if entry.kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    if entry.kind == "int":
        try:
            value = int(str(raw).strip() or 0)
        except (TypeError, ValueError):
            raise ValidationError(_("Enter a whole number.")) from None
        return _within_range(entry, value)

    if entry.kind == "money":
        # Stored as minor units like every other amount; typed the way it is
        # written. "2500 is $25.00" was a help-text apology for a form that
        # should have taken $25.00 in the first place.
        from homeautoshop.core.measurements import Money, format_money
        from homeautoshop.core.moneyform import parse_amount

        currency = settings_currency()
        value = parse_amount(raw if str(raw).strip() else 0, currency)
        return _within_range(entry, value, render=lambda n: format_money(Money(n, currency)))

    value = "" if raw is None else str(raw).strip()

    if entry.kind == "choice":
        allowed = [key for key, _label in entry.choices]
        if value not in allowed:
            # Listing them only helps while the list is short. The timezone
            # picker has five hundred and fifty options, and an error message
            # that prints all of them is not an error message.
            if len(allowed) <= 12:
                raise ValidationError(
                    _("Choose one of: %(options)s.") % {"options": ", ".join(allowed)}
                )
            raise ValidationError(
                _("%(value)s is not one of the choices offered.") % {"value": value}
            )
        return value

    if entry.check is not None:
        entry.check(value)
    return value
