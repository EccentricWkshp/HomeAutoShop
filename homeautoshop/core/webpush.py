"""
Web push for reminders (SPEC §9.4, FR-MAINT-10).

**The tension, stated rather than buried.** Web Push does not deliver to a
browser directly. It delivers to *the browser vendor's* push service — Google's
FCM for Chrome, Mozilla's autopush for Firefox, Apple's for Safari — which then
wakes the device. So this is the one feature in a local-first application that
cannot work without an outbound call to a large cloud service, and no design
choice here changes that.

What follows from it, and is enforced below:

* **Off unless the operator opts in**, per device, through a browser permission
  prompt they see. There is no way to enable it on someone's behalf.
* **Offline Mode disables it**, like every other outbound path.
* **The endpoint host must be allowlisted** (§12.3), so the operator can see
  exactly which service their phone talks to, and refuse.
* **The payload carries no vehicle identity.** A notification renders on a lock
  screen in front of whoever is standing there, and it passes through a third
  party on the way. "Two things are due" is the whole message; the detail is
  behind the tap.

VAPID keys are generated once and stored as a `Setting`. They are this
instance's identity to the push services and nothing else — losing them means
re-subscribing, not a breach.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

from django.conf import settings
from django.utils.translation import gettext_lazy as _

from .models import Setting
from .runtime import conf

log = logging.getLogger(__name__)

VAPID_KEY = "webpush.vapid"

#: The push services the shipped browsers use. An endpoint on anything else is
#: refused rather than dialed — a subscription is written by script, and the
#: allowlist is what keeps a compromised page from turning this into a
#: general-purpose outbound POST.
PUSH_HOSTS = (
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "web.push.apple.com",
    "wns2-*.notify.windows.com",
)


class PushUnavailable(RuntimeError):
    """Push cannot be used, with a reason worth showing the operator."""


def available() -> bool:
    try:
        import pywebpush  # noqa: F401
    except ImportError:
        return False
    return not conf.OFFLINE_MODE


def keys() -> dict:
    """This instance's VAPID key pair, generated on first use."""
    stored = Setting.get(VAPID_KEY)
    if isinstance(stored, dict) and stored.get("private"):
        return stored

    from py_vapid import Vapid01

    vapid = Vapid01()
    vapid.generate_keys()
    pair = {
        "private": vapid.private_pem().decode("utf-8"),
        "public": _public_key(vapid),
    }
    Setting.put(VAPID_KEY, pair)
    return pair


def _public_key(vapid) -> str:
    """The base64url public key the browser needs to subscribe."""
    import base64

    from cryptography.hazmat.primitives import serialization

    raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def public_key() -> str:
    return keys()["public"]


def endpoint_allowed(endpoint: str) -> bool:
    host = (urlparse(endpoint).hostname or "").lower()
    if not host:
        return False
    for allowed in PUSH_HOSTS:
        if allowed.startswith("wns2-") and host.endswith(".notify.windows.com"):
            return True
        if host == allowed:
            return True
    return False


def send(channel, *, title: str, body: str, url: str = "/") -> None:
    """Deliver one notification to one subscribed browser.

    Deliberately terse. This passes through a third party and lands on a lock
    screen: what it says is that something needs attention, not what or whose.
    """
    if conf.OFFLINE_MODE:
        raise PushUnavailable(_("Offline Mode is on, so nothing is pushed."))

    subscription = channel.subscription or {}
    endpoint = subscription.get("endpoint", "")
    if not endpoint:
        raise PushUnavailable(_("That device has not subscribed."))
    if not endpoint_allowed(endpoint):
        raise PushUnavailable(
            _("%(host)s is not a push service this build will talk to.")
            % {"host": urlparse(endpoint).hostname}
        )

    try:
        from pywebpush import WebPushException, webpush
    except ImportError as exc:  # pragma: no cover - the dependency is declared
        raise PushUnavailable(_("Web push support is not installed.")) from exc

    pair = keys()
    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps({"title": title, "body": body, "url": url, "tag": "homeautoshop"}),
            vapid_private_key=pair["private"],
            # A contact address is required by RFC 8292 so a push service can
            # reach the sender about abuse. The instance's own base URL is used
            # rather than an operator's email — this is a home instance, and
            # putting a personal address on every request to Google is not a
            # trade this application makes for them.
            vapid_claims={"sub": f"mailto:noreply@{urlparse(settings.BASE_URL).hostname or 'localhost'}"},
            timeout=10,
        )
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", 0)
        if status in (404, 410):
            # The subscription is gone: the browser was reinstalled, or the
            # permission revoked. Disabling it is correct — retrying forever
            # would fill the log with a failure nobody can fix.
            channel.is_enabled = False
            channel.last_error = str(_("That device unsubscribed."))[:300]
            channel.save(update_fields=["is_enabled", "last_error", "updated_at"])
            raise PushUnavailable(_("That device unsubscribed.")) from exc
        raise
