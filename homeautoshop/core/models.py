"""
Core model primitives.

Every entity in HomeAutoShop is built on these, so the rules in SPEC §5.4 and
§5.5 are enforced in one place rather than remembered at each call site:

* UUIDv7 primary keys, mintable by an offline client (§5.5).
* Soft delete with a 30-day trash (P-5).
* Optimistic concurrency via `revision` for mutable entities (§5.4).
* Append-only entities, which by construction cannot conflict (§5.4).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

TRASH_RETENTION_DAYS = 30


def uuid7() -> uuid.UUID:
    """Time-ordered UUID (RFC 9562), per SPEC §5.5.

    Time ordering gives index locality, and the client can mint one offline so
    that replaying a queued create is idempotent.
    """
    return uuid.uuid7()


class StaleRevisionError(Exception):
    """Raised when a write carries a revision older than the stored row.

    Surfaces as HTTP 409 with the current representation attached (SPEC §5.4).
    Conflicts are never auto-resolved and never silently dropped.
    """

    def __init__(self, instance: models.Model, expected: int, actual: int) -> None:
        self.instance = instance
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"{instance.__class__.__name__} {instance.pk} is at revision {actual}, "
            f"write carried {expected}"
        )


class SoftDeleteQuerySet(models.QuerySet):
    def alive(self) -> "SoftDeleteQuerySet":
        return self.filter(deleted_at__isnull=True)

    def dead(self) -> "SoftDeleteQuerySet":
        return self.filter(deleted_at__isnull=False)

    def in_trash(self) -> "SoftDeleteQuerySet":
        """Soft-deleted and still restorable (FR-ADM-7)."""
        cutoff = timezone.now() - timedelta(days=TRASH_RETENTION_DAYS)
        return self.filter(deleted_at__isnull=False, deleted_at__gte=cutoff)

    def delete(self):  # type: ignore[override]
        """Soft-delete the whole queryset.

        Returns Django's `(count, {label: count})` shape rather than the bare
        integer `update()` gives back, so callers written against the ORM
        contract keep working.
        """
        label = self.model._meta.label
        count = self.update(deleted_at=timezone.now())
        return count, {label: count}

    def hard_delete(self):
        return super().delete()


class AliveManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """Default manager: excludes soft-deleted rows."""

    def get_queryset(self) -> SoftDeleteQuerySet:
        return super().get_queryset().filter(deleted_at__isnull=True)


class AllObjectsManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """Every row including trash. Needed by restore, export, and audit."""


def alive_manager(queryset_class: type[models.QuerySet]) -> models.Manager:
    """Build an alive-only manager from a model-specific queryset.

    A model that adds its own queryset methods must still hide soft-deleted
    rows from `objects` — defining a plain `Manager.from_queryset(...)` would
    silently shadow AliveManager and resurrect the trash.
    """

    class _Manager(models.Manager.from_queryset(queryset_class)):
        def get_queryset(self):
            return super().get_queryset().filter(deleted_at__isnull=True)

    return _Manager()


class BaseModel(models.Model):
    """Identity, provenance, and soft delete. The root of every entity."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        editable=False,
    )
    deleted_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)

    objects = AliveManager()
    all_objects = AllObjectsManager()

    class Meta:
        abstract = True
        get_latest_by = "created_at"

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def delete(self, using=None, keep_parents=False, hard: bool = False):
        """Soft delete by default. Deactivating never destroys authored work."""
        if hard:
            return super().delete(using=using, keep_parents=keep_parents)
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at", "updated_at"])
        return (0, {})

    def restore(self) -> None:
        self.deleted_at = None
        self.save(update_fields=["deleted_at", "updated_at"])


class RevisionedModel(BaseModel):
    """A mutable entity, guarded by optimistic concurrency (SPEC §5.4).

    Callers that hold a revision pass `expected_revision` to `save()`. A stale
    value raises `StaleRevisionError` rather than overwriting; the caller is
    expected to present a merge, never to retry blindly.
    """

    revision = models.PositiveIntegerField(default=1, editable=False)

    class Meta(BaseModel.Meta):
        abstract = True

    def save(self, *args, expected_revision: int | None = None, **kwargs):
        if self.pk and expected_revision is not None:
            current = (
                type(self)
                .all_objects.filter(pk=self.pk)
                .values_list("revision", flat=True)
                .first()
            )
            if current is not None and current != expected_revision:
                raise StaleRevisionError(self, expected_revision, current)

        if self.pk and not self._state.adding:
            self.revision = models.F("revision") + 1
            fields = kwargs.get("update_fields")
            if fields is not None:
                kwargs["update_fields"] = list(dict.fromkeys([*fields, "revision", "updated_at"]))

        super().save(*args, **kwargs)

        if isinstance(self.revision, models.expressions.CombinedExpression):
            self.refresh_from_db(fields=["revision"])


class AppendOnlyModel(BaseModel):
    """An observation, not a fact that can change (SPEC §5.4).

    Odometer readings, notes, media, and time entries are append-only: the
    server always accepts them, so an offline client can never lose a capture
    to a conflict. Corrections are new rows, not edits.
    """

    #: Server-computed columns that may be rewritten after creation.
    #: These are the "derived" class from SPEC §5.4 — recomputed server-side,
    #: never written by a client — so a thumbnail can be regenerated without
    #: making the record itself mutable.
    server_writable_fields: frozenset[str] = frozenset()

    class Meta(BaseModel.Meta):
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            allowed = set(kwargs.get("update_fields") or [])
            permitted = {"deleted_at", "updated_at"} | set(self.server_writable_fields)
            if not allowed or not allowed <= permitted:
                raise ValidationError(
                    _("%(model)s is append-only; record a correction instead of editing.")
                    % {"model": self.__class__.__name__}
                )
        return super().save(*args, **kwargs)


class AuditLog(models.Model):
    """Not a compliance artifact — a 'who changed the odometer, and why' artifact."""

    class Action(models.TextChoices):
        CREATE = "create", _("Created")
        UPDATE = "update", _("Updated")
        DELETE = "delete", _("Deleted")
        RESTORE = "restore", _("Restored")
        LOGIN = "login", _("Signed in")
        LOGIN_FAILED = "login_failed", _("Failed sign-in")
        EXPORT = "export", _("Exported")
        BACKUP = "backup", _("Backed up")
        OUTBOUND = "outbound", _("Outbound request")

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    at = models.DateTimeField(default=timezone.now, db_index=True)
    entity_type = models.CharField(max_length=64, db_index=True)
    entity_id = models.UUIDField(null=True, blank=True, db_index=True)
    action = models.CharField(max_length=32, choices=Action.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    source = models.CharField(max_length=16, default="web")
    summary = models.CharField(max_length=255, blank=True)
    diff = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-at"]
        indexes = [models.Index(fields=["entity_type", "entity_id", "-at"])]

    def __str__(self) -> str:
        return f"{self.action} {self.entity_type} @ {self.at:%Y-%m-%d %H:%M}"


class Job(models.Model):
    """Postgres-backed job queue (SPEC P-3) — no Redis, no broker.

    Jobs are idempotent and retried with backoff; permanent failures land in
    `failed` and are visible in admin health (NFR-R-2).
    """

    class State(models.TextChoices):
        PENDING = "pending", _("Pending")
        RUNNING = "running", _("Running")
        DONE = "done", _("Done")
        FAILED = "failed", _("Failed")

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    type = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING, db_index=True)
    run_after = models.DateTimeField(default=timezone.now, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    last_error = models.TextField(blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["run_after"]
        indexes = [models.Index(fields=["state", "run_after"])]

    def __str__(self) -> str:
        return f"{self.type} [{self.state}]"

    def backoff(self) -> timedelta:
        return timedelta(seconds=min(600, 5 * (2**self.attempts)))


class Setting(models.Model):
    """Instance configuration surfaced in the UI, overriding environment defaults."""

    key = models.CharField(max_length=64, primary_key=True)
    value = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.key

    @classmethod
    def get(cls, key: str, default=None):
        row = cls.objects.filter(key=key).values_list("value", flat=True).first()
        return default if row is None else row.get("v", default)

    @classmethod
    def put(cls, key: str, value) -> None:
        cls.objects.update_or_create(key=key, defaults={"value": {"v": value}})


class ExternalRef(models.Model):
    """Provenance for anything imported from another system (SPEC §6.2).

    One small table is what makes an import idempotent, drift detectable, and
    unlinking possible. Without it, re-running an import duplicates everything
    and there is no way to tell an imported row from a hand-entered one.
    """

    class State(models.TextChoices):
        LINKED = "linked", _("Linked")
        ORPHANED = "orphaned", _("Gone from the source")
        CONFLICTED = "conflicted", _("Changed on both sides")

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    source_system = models.CharField(max_length=32, db_index=True)
    source_instance_url = models.CharField(max_length=255, blank=True)
    external_type = models.CharField(max_length=48)
    external_id = models.CharField(max_length=128)
    entity_type = models.CharField(max_length=48, db_index=True)
    entity_id = models.UUIDField(db_index=True)
    first_imported_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    source_hash = models.CharField(max_length=64, blank=True)
    state = models.CharField(max_length=12, choices=State.choices, default=State.LINKED)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_system", "source_instance_url", "external_type", "external_id"],
                name="unique_external_ref",
            )
        ]
        indexes = [models.Index(fields=["source_system", "external_type"])]

    def __str__(self) -> str:
        return f"{self.source_system}:{self.external_type}:{self.external_id}"

    @classmethod
    def hash_of(cls, payload: dict) -> str:
        """Stable digest of a source record, for drift detection."""
        import hashlib
        import json

        canonical = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    @classmethod
    def lookup(cls, source: str, instance: str, external_type: str, external_id) -> "ExternalRef | None":
        return cls.objects.filter(
            source_system=source,
            source_instance_url=instance,
            external_type=external_type,
            external_id=str(external_id),
        ).first()


class NotificationChannel(BaseModel):
    """Somewhere reminders can be delivered (SPEC FR-MAINT-10).

    Every channel is opt-in and starts disabled. Nothing is delivered anywhere
    until an operator deliberately configures a destination — and nothing is
    sent at all when there is nothing to say.
    """

    class Kind(models.TextChoices):
        EMAIL = "email", _("Email")
        WEBHOOK = "webhook", _("Webhook")
        WEBPUSH = "webpush", _("Phone notification")

    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.EMAIL)
    target = models.CharField(
        max_length=500, help_text=_("Email address, or the URL to POST to.")
    )
    #: A Web Push subscription: endpoint plus the browser's own keys. Written by
    #: the browser, never typed. Kept apart from `target` because it is a
    #: credential the operator cannot read or check, and mixing the two would
    #: put it on a screen that shows targets.
    subscription = models.JSONField(default=dict, blank=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="notification_channels",
        help_text=_("Leave blank for a shop-wide channel."),
    )
    is_enabled = models.BooleanField(default=False)
    include_routine = models.BooleanField(
        default=True, help_text=_("Include routine items, not just safety and overdue.")
    )
    last_sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["kind", "name"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.name}"

    @property
    def masked_target(self) -> str:
        """Targets can carry secrets in a query string; do not echo them."""
        if self.kind == self.Kind.WEBPUSH:
            # The endpoint identifies a browser installation to its push
            # service and is a bearer capability — showing it buys nothing and
            # gives it away. What the operator needs is which device this is,
            # and that is what `name` holds.
            from urllib.parse import urlparse

            host = urlparse((self.subscription or {}).get("endpoint", "")).hostname or ""
            return host or str(_("this browser"))
        if self.kind == self.Kind.EMAIL:
            name, _sep, domain = self.target.partition("@")
            return f"{name[:2]}…@{domain}" if domain else self.target
        from urllib.parse import urlparse

        # The path is never shown. Home Assistant, Slack and ntfy webhooks all
        # carry their secret in the path, so truncating it is not enough —
        # a short path leaks the whole token.
        parsed = urlparse(self.target)
        return f"{parsed.scheme}://{parsed.hostname}/…"


class NotificationSent(models.Model):
    """What has already been said, so it is not said again tomorrow."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    channel = models.ForeignKey(
        NotificationChannel, on_delete=models.CASCADE, related_name="sent"
    )
    dedupe_key = models.CharField(max_length=200, db_index=True)
    sent_at = models.DateTimeField(default=timezone.now)
    subject = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-sent_at"]
        indexes = [models.Index(fields=["channel", "dedupe_key", "-sent_at"])]

    def __str__(self) -> str:
        return f"{self.dedupe_key} -> {self.channel}"
