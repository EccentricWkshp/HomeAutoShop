"""
Reminders (SPEC FR-MAINT-10, §5.2 `reminders.evaluate`).

Three rules decide whether this feature gets used or muted:

* **A digest, not a stream.** One message listing everything, never one message
  per item. This is the whole difference between reminders people keep on and
  reminders people filter to trash.
* **Silence when there is nothing to say.** No "all clear" mail. An empty inbox
  is the good outcome, and a weekly nothing-to-report message trains people to
  ignore the next one.
* **A cooldown per item.** An overdue item you have decided to live with must
  not be raised again tomorrow. It waits, then mentions itself once more.

Every channel is opt-in, off by default, and disabled entirely by Offline Mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from .models import NotificationChannel, NotificationSent

log = logging.getLogger(__name__)

SEVERITY_RANK = {"overdue": 0, "safety": 1, "warning": 2, "info": 3}


@dataclass(slots=True)
class Alert:
    dedupe_key: str
    severity: str
    title: str
    detail: str = ""
    url: str = ""
    is_routine: bool = True

    @property
    def rank(self) -> int:
        return SEVERITY_RANK.get(self.severity, 9)


@dataclass(slots=True)
class Digest:
    alerts: list[Alert] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.alerts

    @property
    def urgent(self) -> list[Alert]:
        return [a for a in self.alerts if a.severity in ("overdue", "safety")]

    def subject(self) -> str:
        urgent = len(self.urgent)
        if urgent:
            return _("%(shop)s: %(n)d item(s) need attention") % {
                "shop": settings.SHOP_NAME, "n": urgent
            }
        return _("%(shop)s: %(n)d reminder(s)") % {
            "shop": settings.SHOP_NAME, "n": len(self.alerts)
        }

    def as_text(self) -> str:
        lines = []
        for alert in sorted(self.alerts, key=lambda a: (a.rank, a.title)):
            marker = "!" if alert.severity in ("overdue", "safety") else "-"
            lines.append(f" {marker} {alert.title}")
            if alert.detail:
                lines.append(f"     {alert.detail}")
        base = settings.BASE_URL.rstrip("/")
        lines.append("")
        lines.append(_("Open the shop: %(url)s") % {"url": base})
        return "\n".join(lines)

    def as_payload(self) -> dict:
        return {
            "shop": settings.SHOP_NAME,
            "generated_at": timezone.now().isoformat(),
            "count": len(self.alerts),
            "alerts": [
                {
                    "key": a.dedupe_key,
                    "severity": a.severity,
                    "title": a.title,
                    "detail": a.detail,
                    "url": f"{settings.BASE_URL.rstrip('/')}{a.url}" if a.url else "",
                }
                for a in sorted(self.alerts, key=lambda a: (a.rank, a.title))
            ],
        }


def collect() -> Digest:
    """Everything worth mentioning right now, from data already computed."""
    from datetime import date

    from homeautoshop.assets.models import Asset
    from homeautoshop.maintenance.models import ServiceStatus
    from homeautoshop.maintenance.services import due_dashboard, project
    from homeautoshop.purchasing.models import Purchase

    from .backup import last_backup_age_days

    digest = Digest()
    today = timezone.localdate()

    for item in due_dashboard():
        overdue = item.status == ServiceStatus.OVERDUE
        digest.alerts.append(
            Alert(
                dedupe_key=f"service:{item.pk}:{item.status}",
                severity="overdue" if overdue else ("safety" if item.is_safety else "warning"),
                title=f"{item.asset.nickname}: {item.definition.name}",
                detail=project(item).summary,
                url=f"/vehicles/{item.asset_id}/schedule/",
                is_routine=not (overdue or item.is_safety),
            )
        )

    horizon = today + timedelta(days=45)
    for asset in Asset.objects.fleet().filter(
        plate_expires_on__isnull=False, plate_expires_on__lte=horizon
    ):
        expired = asset.plate_expires_on < today
        digest.alerts.append(
            Alert(
                dedupe_key=f"registration:{asset.pk}:{asset.plate_expires_on}",
                severity="overdue" if expired else "warning",
                title=_("%(name)s registration") % {"name": asset.nickname},
                detail=_("Expired %(d)s") % {"d": asset.plate_expires_on}
                if expired
                else _("Expires %(d)s") % {"d": asset.plate_expires_on},
                url=f"/vehicles/{asset.pk}/",
                is_routine=False,
            )
        )

    for purchase in Purchase.objects.select_related("vendor"):
        if purchase.return_window_closing:
            digest.alerts.append(
                Alert(
                    dedupe_key=f"return:{purchase.pk}",
                    severity="warning",
                    title=_("Return window closing: %(v)s") % {"v": purchase.vendor.name},
                    detail=_("Returnable until %(d)s") % {"d": purchase.return_by},
                    url=f"/purchases/{purchase.pk}/",
                )
            )

    age = last_backup_age_days()
    if age is None or age > settings.BACKUP_WARN_AFTER_DAYS:
        digest.alerts.append(
            Alert(
                dedupe_key=f"backup:{'never' if age is None else int(age // 7)}",
                severity="warning",
                title=_("Backups"),
                detail=_("No backup has ever run.")
                if age is None
                else _("Last backup was %(n)d days ago.") % {"n": int(age)},
                url="/health/",
                is_routine=False,
            )
        )
    return digest


def _already_said(channel: NotificationChannel, alert: Alert) -> bool:
    cutoff = timezone.now() - timedelta(days=settings.REMINDER_COOLDOWN_DAYS)
    return NotificationSent.objects.filter(
        channel=channel, dedupe_key=alert.dedupe_key, sent_at__gte=cutoff
    ).exists()


def digest_for(channel: NotificationChannel, digest: Digest) -> Digest:
    """Narrow a digest to what this channel has not recently been told."""
    alerts = [a for a in digest.alerts if channel.include_routine or not a.is_routine]
    return Digest(alerts=[a for a in alerts if not _already_said(channel, a)])


def deliver(channel: NotificationChannel, digest: Digest) -> bool:
    """Send one digest. Returns whether anything was actually sent."""
    if digest.is_empty:
        return False

    subject = digest.subject()
    try:
        if channel.kind == NotificationChannel.Kind.EMAIL:
            _send_email(channel, subject, digest)
        elif channel.kind == NotificationChannel.Kind.WEBHOOK:
            _send_webhook(channel, digest)
        elif channel.kind == NotificationChannel.Kind.WEBPUSH:
            _send_push(channel, subject, digest)
        else:
            return False
    except Exception as exc:
        log.warning("notification to %s failed: %s", channel, exc)
        channel.last_error = f"{type(exc).__name__}: {exc}"[:300]
        channel.save(update_fields=["last_error", "updated_at"])
        return False

    NotificationSent.objects.bulk_create(
        [
            NotificationSent(channel=channel, dedupe_key=a.dedupe_key, subject=subject[:200])
            for a in digest.alerts
        ]
    )
    channel.last_sent_at = timezone.now()
    channel.last_error = ""
    channel.save(update_fields=["last_sent_at", "last_error", "updated_at"])
    return True


def _send_email(channel: NotificationChannel, subject: str, digest: Digest) -> None:
    from django.core.mail import EmailMessage

    if not settings.EMAIL_HOST:
        raise RuntimeError("SMTP_HOST is not configured")
    body = f"{digest.as_text()}\n"
    EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[channel.target],
    ).send(fail_silently=False)


def _send_webhook(channel: NotificationChannel, digest: Digest) -> None:
    from .outbound import post_json

    post_json(channel.target, digest.as_payload(), purpose="reminders.webhook")


def _send_push(channel: NotificationChannel, subject: str, digest: Digest) -> None:
    """A count and a tap target, and nothing else.

    This renders on a lock screen in front of whoever is standing there, and it
    passes through Google or Mozilla or Apple on the way. Naming the vehicle
    would put "the Silverado needs brakes" on both.
    """
    from . import webpush

    webpush.send(
        channel,
        title=str(_("Something is due")),
        body=str(
            ngettext(
                "%(n)d item needs attention.", "%(d)d items need attention.", len(digest.alerts)
            )
            % {"n": len(digest.alerts), "d": len(digest.alerts)}
        ),
        url="/due/",
    )


def run(*, force: bool = False) -> dict:
    """Evaluate reminders and deliver them (job type `reminders.evaluate`)."""
    result = {"channels": 0, "sent": 0, "alerts": 0, "skipped": ""}

    if settings.OFFLINE_MODE:
        result["skipped"] = "offline mode"
        return result
    if not settings.REMINDERS_ENABLED and not force:
        result["skipped"] = "reminders disabled"
        return result

    channels = list(NotificationChannel.objects.filter(is_enabled=True))
    result["channels"] = len(channels)
    if not channels:
        result["skipped"] = "no enabled channels"
        return result

    digest = collect()
    result["alerts"] = len(digest.alerts)
    if digest.is_empty:
        # Silence is the good outcome. An "all clear" message every week trains
        # people to ignore the one that matters.
        result["skipped"] = "nothing to report"
        return result

    for channel in channels:
        if deliver(channel, digest_for(channel, digest)):
            result["sent"] += 1
    return result
