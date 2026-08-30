"""Stamp `created_by` from the request-scoped user, and audit writes."""

from __future__ import annotations

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .middleware import get_current_user
from .models import AuditLog, BaseModel

# Models whose every write is worth a log line. Kept narrow on purpose: an
# audit trail nobody can read is not an audit trail.
AUDITED = {"Asset", "WorkOrder", "Person", "User", "UsageReading"}


@receiver(pre_save)
def stamp_created_by(sender, instance, **kwargs):
    if not isinstance(instance, BaseModel) or instance.created_by_id:
        return
    user = get_current_user()
    if user is not None and getattr(user, "pk", None):
        instance.created_by = user


@receiver(post_save)
def audit_write(sender, instance, created, **kwargs):
    name = sender.__name__
    if name not in AUDITED or not isinstance(instance, BaseModel):
        return
    user = get_current_user()
    AuditLog.objects.create(
        entity_type=name,
        entity_id=instance.pk,
        action=AuditLog.Action.CREATE if created else AuditLog.Action.UPDATE,
        user=user if getattr(user, "pk", None) else None,
        summary=str(instance)[:255],
    )
