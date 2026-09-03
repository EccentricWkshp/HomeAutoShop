"""
HomeAutoShop settings.

Configuration comes from the environment (SPEC §14). Every setting that the
spec names is read here with the documented default, so `docker compose up`
with no .env produces a working instance.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Safe to import this early: the package `__init__` reads a file and imports no
# Django, so it cannot participate in the settings-import cycle.
from homeautoshop import __version__ as _app_version

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(key: str, default: str = "") -> str:
    """Read a setting, supporting the _FILE indirection for secrets (SPEC §14)."""
    file_key = f"{key}_FILE"
    if path := os.environ.get(file_key):
        return Path(path).read_text(encoding="utf-8").strip()
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    return env(key, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_int(key: str, default: int) -> int:
    try:
        return int(env(key, str(default)))
    except ValueError:
        return default


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

# What this instance is running. The version is the repository's, read from the
# VERSION file by `homeautoshop/__init__.py`; the revision is the commit the
# image was built from and can only come from the build, so it arrives as an
# environment variable the Dockerfile sets from a build argument. Blank outside
# a built image, which is the honest answer for a checkout that may have
# uncommitted changes in it.
#
# Both are for reading, never for asking: nothing here checks whether a newer
# version exists. PRIVACY.md rules out update checks along with the rest of the
# phone-home surface, and a version string is only a liability if something
# sends it somewhere.
APP_VERSION = _app_version
APP_REVISION = env("APP_REVISION", "")

SECRET_KEY = env("SECRET_KEY", "dev-only-insecure-key-change-me")
# Encrypts integration credentials stored in the database (R-9, §17.1). It
# stays in the environment by definition: a key kept in the database it
# protects is not a key. Blank derives it from SECRET_KEY, which keeps an
# existing instance working without a new variable at the cost of one secret
# protecting two things. Rotating it invalidates every stored credential at
# once, which is the intended emergency behavior.
CREDENTIAL_KEY = env("CREDENTIAL_KEY", "")
# Written by gunicorn --pid, and the only thing that makes the "Apply and
# restart" button in the pending-restart banner possible (§17.2). Absent — a
# runserver, a slim profile — the banner names the command instead of offering
# a button that would quietly do nothing.
GUNICORN_PIDFILE = env("GUNICORN_PIDFILE", "/tmp/gunicorn.pid")
DEBUG = env_bool("DEBUG", False)
SHOP_NAME = env("SHOP_NAME", "Home Shop")
# Read so the first-run page can say what has to happen before another
# device will trust this site. Both already reach the app through the
# shared `.env`; only the proxy was being told about them.
TLS_MODE = env("TLS_MODE", "internal")
SITE_ADDRESS = env("SITE_ADDRESS", "")
BASE_URL = env("BASE_URL", "http://localhost:8000")

ALLOWED_HOSTS = [h.strip() for h in env("ALLOWED_HOSTS", "*").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in env("CSRF_TRUSTED_ORIGINS", BASE_URL).split(",") if o.strip().startswith("http")
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "homeautoshop.core",
    "homeautoshop.accounts",
    "homeautoshop.people",
    "homeautoshop.assets",
    "homeautoshop.work",
    "homeautoshop.inspections",
    "homeautoshop.maintenance",
    "homeautoshop.parts",
    "homeautoshop.purchasing",
    "homeautoshop.mediafiles",
    "homeautoshop.diagnostics",
    "homeautoshop.fluids",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Directly after SecurityMiddleware, so a static response still carries the
    # security headers but skips session, locale and auth work it never needs.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Before LocaleMiddleware, which reads LANGUAGE_CODE: a stored language
    # applied after it would take an extra request to appear (§17.2).
    "homeautoshop.core.middleware.ConfigMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "homeautoshop.core.middleware.CurrentUserMiddleware",
    # After authentication, because it reads the user, and before the view, so
    # a helper is stopped at the door of a screen rather than somewhere inside
    # it (§12.2a). Its check runs in `process_view`, where the URL name exists.
    "homeautoshop.accounts.middleware.HelperGateMiddleware",
    # Last, so it sees the final redirect after everything else has had
    # its say about it (§9.2 — the garage-first rule is mostly about not
    # making somebody find their place again on a phone).
    "homeautoshop.core.middleware.KeepYourPlaceMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                # `LANGUAGE_CODE` and `LANGUAGE_BIDI`, which `base.html` puts
                # on <html> as `lang` and `dir`. It was reading `LANGUAGE_CODE`
                # without this, so every page declared `lang="en"` however it
                # had been translated, and nothing could ever be right-to-left.
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "homeautoshop.core.context.instance",
            ],
        },
    },
]

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
# Postgres is the production target (SPEC §5.1). SQLite is supported for local
# development and for the test suite so the app can be run without Docker;
# see docs/DEVELOPMENT.md for what differs.

DATABASE_URL = env("DATABASE_URL", "")
if DATABASE_URL:
    from urllib.parse import unquote, urlparse

    parsed = urlparse(DATABASE_URL)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": parsed.path.lstrip("/"),
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname or "",
            "PORT": str(parsed.port or ""),
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "data" / "homeautoshop.sqlite3",
            "OPTIONS": {"init_command": "PRAGMA journal_mode=WAL;"},
            # A file-backed test database rather than :memory:, so the backup
            # and restore paths are exercisable rather than assumed (§13.2).
            "TEST": {"NAME": BASE_DIR / "data" / "test-homeautoshop.sqlite3"},
        }
    }

# The suite must not depend on somebody having a connection. A mock that
# misses on a failing fetch is a confusing error; one that misses on a
# succeeding fetch is a passing test that proves nothing (§8.1b).
TEST_RUNNER = "homeautoshop.core.testrunner.Runner"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

# --------------------------------------------------------------------------
# Authentication (SPEC §12.2)
# --------------------------------------------------------------------------

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    # A length floor rather than composition rules, per SPEC §12.2.
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 30
SESSION_SAVE_EVERY_REQUEST = True
CSRF_COOKIE_SAMESITE = "Lax"

_secure = BASE_URL.startswith("https://")
SESSION_COOKIE_SECURE = _secure
CSRF_COOKIE_SECURE = _secure
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"
if _secure:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True

# --------------------------------------------------------------------------
# Localization (SPEC §5.6) — built in from commit one, not retrofitted.
# --------------------------------------------------------------------------

LANGUAGE_CODE = env("LOCALE_DEFAULT", "en-us").lower().replace("_", "-")
TIME_ZONE = env("TZ", "UTC")
USE_I18N = True
USE_L10N = True
USE_TZ = True
LOCALE_PATHS = [BASE_DIR / "locale"]

LANGUAGES = [
    ("en-us", "English (United States)"),
    ("en-ca", "English (Canada)"),
    ("fr-ca", "français (Canada)"),
    ("es-mx", "español (México)"),
]

# Reporting currency for rollups. Individual transactions carry their own
# currency and a snapshotted FX rate (SPEC §5.5); nothing here overrides that.
CURRENCY_REPORTING = env("CURRENCY_REPORTING", "USD")
UNITS = env("UNITS", "imperial")

# OQ-4: tooling is tracked and exported, but excluded from per-asset cost
# unless the operator opts in. A torque wrench is not a cost of the Civic.
COST_INCLUDE_TOOLING = env_bool("COST_INCLUDE_TOOLING", False)
# Optional valuation of your own time, for reporting only (FR-TIME-3).
LABOR_RATE_MINOR = env_int("LABOR_RATE_MINOR", 0)

# Maintenance (SPEC §7.7). "Due soon" is a lead window HomeAutoShop owns —
# never inherited from another product's per-user setting (WL-Q10).
DUE_SOON_DAYS = env_int("DUE_SOON_DAYS", 30)
DUE_SOON_DISTANCE = env_int("DUE_SOON_DISTANCE", 500)
# Fallback when an asset has too little history to observe a usage rate.
DEFAULT_DISTANCE_PER_DAY = env_int("DEFAULT_DISTANCE_PER_DAY", 30)

# Inspections (SPEC §7.8).
DVI_ENABLED = env_bool("DVI_ENABLED", True)

# --------------------------------------------------------------------------
# Storage and media (SPEC §5.1, §7.9)
# --------------------------------------------------------------------------

STORAGE_DRIVER = env("STORAGE_DRIVER", "filesystem")
MEDIA_ROOT = Path(env("MEDIA_ROOT", str(BASE_DIR / "data" / "media")))
MEDIA_URL = "/media/"
MAX_UPLOAD_MB = env_int("MAX_UPLOAD_MB", 50)
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_MB * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
STRIP_GPS_EXIF = env_bool("STRIP_GPS_EXIF", True)  # FR-DOC-9

# OCR (FR-DOC-5, §14). Runs in the worker, entirely on this machine — the
# reason it is worth the image weight is that the alternative for a receipt is
# somebody else's server. Turning it off leaves media marked `pending` rather
# than `failed`, so switching it back on has a backlog to work through instead
# of a set of rows that claim to have been tried.
OCR_ENABLED = env_bool("OCR_ENABLED", True)
# Tesseract language codes, in preference order. Set from docker-compose.yml
# from the same variable that decides which packs the image installs; asking
# for a language that was never installed is an error, not a fallback.
OCR_LANGUAGES = "+".join(env("OCR_LANGUAGES", "eng").replace(",", " ").split())
# How far into an image-only PDF to read. A receipt is one page and a scan-tool
# report a handful; a service manual is hundreds, and rasterising all of them at
# 300 DPI is hours of worker time for text nobody searches.
OCR_PDF_MAX_PAGES = env_int("OCR_PDF_MAX_PAGES", 20)

# Read whichever driver is selected, because `migrate_storage` has to build
# both ends at once and the configured default is only ever one of them —
# including halfway through a migration, when the setting has been flipped and
# the files have not yet moved.
S3_OPTIONS = {
    # No default: nothing is bundled to point at, so `s3` means a store the
    # operator runs or rents and the address is theirs to give. Blank is
    # boto3's own default, which is real AWS S3.
    "endpoint_url": env("STORAGE_ENDPOINT", ""),
    "bucket": env("STORAGE_BUCKET", "homeautoshop"),
    "access_key": env("STORAGE_ACCESS_KEY", ""),
    "secret_key": env("STORAGE_SECRET_KEY", ""),
    "region": env("STORAGE_REGION", "us-east-1"),
    # Only for an operator who has actually published their object store on an
    # address a browser can reach. Left blank — the default — files are served
    # by the application instead, which needs no second hostname, no second
    # certificate, and no exposed port. See homeautoshop/mediafiles/views.py.
    "public_endpoint": env("STORAGE_PUBLIC_ENDPOINT", ""),
}

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
if STORAGE_DRIVER == "s3":
    STORAGES["default"] = {
        "BACKEND": "homeautoshop.mediafiles.storage.S3Storage",
        "OPTIONS": S3_OPTIONS,
    }

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# Hashed, immutable filenames require `collectstatic` to have run, which is true
# in the container and false for `runserver` and the test suite. Rather than
# guess, the container sets STATIC_HASHED and everywhere else serves the source
# files through the finders — an unhashed stylesheet beats a template that
# raises because staticfiles.json is not there.
STATIC_HASHED = env_bool("STATIC_HASHED", False)
if STATIC_HASHED:
    STORAGES["staticfiles"] = {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    }
WHITENOISE_USE_FINDERS = not STATIC_HASHED
WHITENOISE_AUTOREFRESH = DEBUG

# WhiteNoise warns about a missing STATIC_ROOT even when the finders are doing
# the serving and it is not needed — a false alarm on every runserver and every
# test run, and a warning nobody can act on is a warning people learn to skip.
# The container already creates this directory; failing to is not worth
# refusing to boot over.
try:
    STATIC_ROOT.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

# Python's mimetypes table has no entry for .webmanifest, and on Windows it
# reads types out of the registry, so the answer varies by host. An unknown
# type is served as application/octet-stream and the browser refuses the
# manifest, so the one type we depend on is stated here rather than looked up.
WHITENOISE_MIMETYPES = {".webmanifest": "application/manifest+json"}

# --------------------------------------------------------------------------
# Integrations (SPEC §8) — every one off unless explicitly enabled, and all of
# them subject to OFFLINE_MODE (NFR-S-2).
# --------------------------------------------------------------------------

OFFLINE_MODE = env_bool("OFFLINE_MODE", False)
VIN_DECODE_ENABLED = env_bool("VIN_DECODE_ENABLED", True)
RECALLS_ENABLED = env_bool("RECALLS_ENABLED", True)
VIN_DECODE_TIMEOUT = env_int("VIN_DECODE_TIMEOUT", 5)
VPIC_BASE_URL = env("VPIC_BASE_URL", "https://vpic.nhtsa.dot.gov/api/vehicles")
SERVICE_INFO_ENABLED = env_bool("SERVICE_INFO_ENABLED", True)
SERVICE_INFO_DEFAULT = env("SERVICE_INFO_DEFAULT", "lemon")
SHOW_PRODUCT_LINKS = env_bool("SHOW_PRODUCT_LINKS", True)

# Hosts an outbound request may reach at all (SPEC §12.3). The allowlist is the
# control, not the address range: a configured integration host is permitted
# even on a private network, while anything else is refused.
OUTBOUND_ALLOWLIST = [
    h.strip()
    for h in env("OUTBOUND_ALLOWLIST", "vpic.nhtsa.dot.gov,api.nhtsa.gov").split(",")
    if h.strip()
]

# The shared template catalog (SPEC §17 R-1). Defaults to this project's own,
# because expecting somebody to stand up a repository before they can install a
# schedule is expecting them not to bother.
#
# A default *address* is not a default *request*. Nothing here is contacted
# until somebody presses Browse — no start-up call, no background poll, no
# update check (§8.1b, and `test_nothing_is_fetched_without_being_asked`). An
# instance that never opens that screen never talks to it, and Offline Mode
# refuses it like everything else.
CATALOG_URL = env("CATALOG_URL", "https://raw.githubusercontent.com/EccentricWkshp/HomeAutoShop/main/catalog/").rstrip("/")

# LubeLogger (SPEC §8.6) — optional and additive, never a dependency. An
# instance with none configured is not a degraded instance.
LUBELOGGER_URL = env("LUBELOGGER_URL", "").rstrip("/")
LUBELOGGER_API_KEY = env("LUBELOGGER_API_KEY", "")
LUBELOGGER_MODE = env("LUBELOGGER_MODE", "import_once")

if LUBELOGGER_URL:
    # The allowlist is the control, not the address range (§12.3): a configured
    # integration host is permitted even when it resolves to a private IP,
    # which is the whole point for a self-hosted LubeLogger on the LAN.
    from urllib.parse import urlparse as _urlparse

    if _host := _urlparse(LUBELOGGER_URL).hostname:
        OUTBOUND_ALLOWLIST.append(_host)

# Plate lookup (SPEC §8.2). Off by default and deliberately so: there is no
# free legal plate-to-VIN service, so every call costs money and sends a plate
# off-box. No provider is bundled or endorsed.
PLATE_LOOKUP_ENABLED = env_bool("PLATE_LOOKUP_ENABLED", False)
PLATE_LOOKUP_PROVIDER = env("PLATE_LOOKUP_PROVIDER", "generic")
PLATE_LOOKUP_URL = env("PLATE_LOOKUP_URL", "")
PLATE_LOOKUP_KEY = env("PLATE_LOOKUP_KEY", "")
# 0 means no cap. A cap is the difference between a mistake costing a dollar
# and a mistake costing a month's budget.
PLATE_LOOKUP_MONTHLY_CAP = env_int("PLATE_LOOKUP_MONTHLY_CAP", 0)
# The operator's own estimate, shown before each call. Nothing reads a price
# list; providers do not publish one.
PLATE_LOOKUP_COST_MINOR = env_int("PLATE_LOOKUP_COST_MINOR", 0)

if PLATE_LOOKUP_ENABLED and PLATE_LOOKUP_URL:
    from urllib.parse import urlparse as _plate_parse

    if _plate_host := _plate_parse(PLATE_LOOKUP_URL).hostname:
        OUTBOUND_ALLOWLIST.append(_plate_host)

# LubeLogger scheduled pull (FR-INT-13). Only consulted when the mode asks for
# a sync; `import_once` and `off` never poll.
LUBELOGGER_SYNC_HOURS = env_int("LUBELOGGER_SYNC_HOURS", 12)

# WrenchLedger (SPEC §8.7) — tool availability on a work order. Off unless a
# key is present, disabled by Offline Mode, and never load-bearing: with it
# absent HomeAutoShop is complete and correct, only less convenient (FR-WL-7).
WRENCHLEDGER_API_KEY = env("WRENCHLEDGER_API_KEY", "")
# Overridable for testing against a staging workspace. The `www` host is not
# incidental: the API drops the Authorization header across a cross-host
# redirect, so the apex domain fails in a way that looks like a bad key.
WRENCHLEDGER_URL = env("WRENCHLEDGER_URL", "https://www.wrench-ledger.app/api/v1")
WRENCHLEDGER_SYNC_HOURS = env_int("WRENCHLEDGER_SYNC_HOURS", 6)
# WL-Q3: which system owns shop consumables, so a quart of oil is not counted
# in both. `homeautoshop` is correct for anyone without a WrenchLedger account,
# which is most people.
CONSUMABLES_OWNER = env("CONSUMABLES_OWNER", "homeautoshop")

if WRENCHLEDGER_API_KEY:
    from urllib.parse import urlparse as _wl_parse

    if _wl_host := _wl_parse(WRENCHLEDGER_URL).hostname:
        OUTBOUND_ALLOWLIST.append(_wl_host)

# --------------------------------------------------------------------------
# Reminders (SPEC FR-MAINT-10) — every channel is opt-in and off by default.
# A notification system people mute is worse than none, so nothing sends until
# a channel is configured, and nothing sends when there is nothing to say.
# --------------------------------------------------------------------------

REMINDERS_ENABLED = env_bool("REMINDERS_ENABLED", False)
# How long before the same unchanged alert may be repeated. Daily nagging about
# a thing you already decided to live with is how people turn reminders off.
REMINDER_COOLDOWN_DAYS = env_int("REMINDER_COOLDOWN_DAYS", 7)

EMAIL_HOST = env("SMTP_HOST", "")
EMAIL_PORT = env_int("SMTP_PORT", 587)
EMAIL_HOST_USER = env("SMTP_USER", "")
EMAIL_HOST_PASSWORD = env("SMTP_PASSWORD", "")
EMAIL_USE_TLS = env_bool("SMTP_TLS", True)
DEFAULT_FROM_EMAIL = env("SMTP_FROM", "homeautoshop@localhost")
EMAIL_TIMEOUT = 15
EMAIL_BACKEND = (
    "django.core.mail.backends.smtp.EmailBackend"
    if EMAIL_HOST
    else "django.core.mail.backends.dummy.EmailBackend"
)

# --------------------------------------------------------------------------
# Backup (SPEC §13)
# --------------------------------------------------------------------------

BACKUP_DIR = Path(env("BACKUP_DIR", str(BASE_DIR / "data" / "backups")))
# How often the worker enqueues `backup.run` (§15.1). An interval rather than
# a cron expression: the worker asks what is due on each pass, so there is no
# crontab to parse and no second scheduler to supervise.
BACKUP_INTERVAL_HOURS = env_int("BACKUP_INTERVAL_HOURS", 24)
BACKUP_RETENTION_DAILY = env_int("BACKUP_RETENTION_DAILY", 7)
BACKUP_RETENTION_WEEKLY = env_int("BACKUP_RETENTION_WEEKLY", 4)
BACKUP_RETENTION_MONTHLY = env_int("BACKUP_RETENTION_MONTHLY", 6)
BACKUP_WARN_AFTER_DAYS = env_int("BACKUP_WARN_AFTER_DAYS", 7)

# --------------------------------------------------------------------------
# Logging — structured, no secrets, no full VINs at info level (NFR-R-3)
# --------------------------------------------------------------------------

LOG_LEVEL = env("LOG_LEVEL", "INFO").upper()
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "handlers": ["console"], "propagate": False},
    },
}

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"
