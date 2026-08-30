"""
Users and roles (SPEC §12.2).

Two roles ship: `admin` and `member`. There is deliberately no per-vehicle
access control — but the *seams* for a narrower `helper` role exist from commit
one, so adding it later is policy rules rather than an audit of every view.
"""

from __future__ import annotations

import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.models import uuid7


class Role(models.TextChoices):
    ADMIN = "admin", _("Administrator")
    MEMBER = "member", _("Member")
    # HELPER is intentionally absent in v1. See can() below and SPEC §17 R-2.


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    person = models.OneToOneField(
        "people.Person",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="user_account",
        help_text=_("Links this login to a human, so authored records attribute to a person."),
    )
    locale = models.CharField(max_length=16, blank=True, help_text=_("Overrides the instance default."))
    timezone = models.CharField(max_length=64, blank=True)
    units = models.CharField(
        max_length=16,
        blank=True,
        choices=[("imperial", _("Imperial")), ("metric", _("Metric"))],
        help_text=_("Display only; storage is unaffected."),
    )

    class Meta:
        ordering = ["username"]

    def __str__(self) -> str:
        return self.get_full_name() or self.username

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN or self.is_superuser

    @property
    def display_name(self) -> str:
        if self.person_id:
            return self.person.display_name
        return self.get_full_name() or self.username


# --------------------------------------------------------------------------
# Authorization policy
# --------------------------------------------------------------------------
# Every authorization decision goes through can(). That indirection is the
# whole point (SPEC §12.2): a `helper` role with per-asset access becomes a
# change here plus a table, rather than a hunt through a hundred views.

ADMIN_ONLY = {
    "user.manage",
    "settings.manage",
    "integration.manage",
    "backup.manage",
    "export.run",
    "trash.manage",
    "audit.read",
}


def can(user, action: str, resource=None) -> bool:
    """Return whether `user` may perform `action`, optionally on `resource`."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if not user.is_active:
        return False
    if user.is_admin:
        return True
    if action in ADMIN_ONLY:
        return False
    # v1: members may do everything else. AssetAccess rows are not consulted
    # because none are written — implied-allow, per SPEC §12.2.
    return True


def require(user, action: str, resource=None) -> None:
    from django.core.exceptions import PermissionDenied

    if not can(user, action, resource):
        raise PermissionDenied(action)


class AssetAccess(models.Model):
    """Scaffolding only — unused in v1 (SPEC §12.2, OQ-7).

    No rows are written and no UI writes them; `can()` does not consult this
    table while only `admin` and `member` exist. It is here so that granting a
    helper access to one vehicle later is a data change, not a migration of
    every authorization call site.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="asset_access")
    asset = models.ForeignKey("assets.Asset", on_delete=models.CASCADE, related_name="access_grants")
    level = models.CharField(
        max_length=16,
        default="read",
        choices=[("read", _("Read")), ("write", _("Write"))],
    )
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "asset"], name="unique_asset_access"),
        ]
        verbose_name_plural = "asset access grants"

    def __str__(self) -> str:
        return f"{self.user} -> {self.asset} ({self.level})"


class ApiToken(models.Model):
    """Personal access token for scripts (SPEC §12.2). Stored hashed, shown once."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=12, db_index=True)
    token_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix}…)"

    @staticmethod
    def generate() -> tuple[str, str, str]:
        """Return (full_token, prefix, hash). The full token is never stored."""
        import hashlib
        import secrets

        raw = f"has_{secrets.token_urlsafe(32)}"
        return raw, raw[:12], hashlib.sha256(raw.encode()).hexdigest()

    @property
    def is_active(self) -> bool:
        from django.utils import timezone

        if self.revoked_at:
            return False
        return not (self.expires_at and self.expires_at < timezone.now())
