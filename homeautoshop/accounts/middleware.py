"""The request gate for the `helper` role (SPEC §12.2a, R-2).

One check, on every request, against an allow-list of URL names. A helper
reaching anything not on that list is refused before the view function runs.

This exists because the alternative was tried and failed. §12.2 promised that
routing every decision through `can()` would make a per-vehicle role "policy
rules, not an audit of every view" — but of 225 view functions only 19 ever
named a resource, all of them in one app, so the promise held for the screens
somebody remembered and nowhere else. A boundary that depends on 225 people
remembering is not a boundary; it is a hope with good documentation.

Enforced in `process_view` rather than in `__call__` because the URL name only
exists after resolution, and enforced on the *name* rather than the path so
that moving a URL cannot silently open it.
"""

from __future__ import annotations

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.utils.functional import SimpleLazyObject

from .policy import HELPER_READ_ONLY_URLS, HELPER_URLS, READ_VERBS, is_helper


class HelperGateMiddleware:
    """Refuse a helper any screen not explicitly opened to them."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, "user", None)
        if user is None or not is_helper(user):
            return None

        match = request.resolver_match
        name = getattr(match, "url_name", None) if match else None

        # An unnamed route cannot be checked against a list of names, so it is
        # refused. There are none in this application, and if one is added the
        # failure is a helper seeing a 403 rather than a helper seeing
        # somebody else's vehicle.
        if not name or name not in HELPER_URLS:
            raise PermissionDenied(name or request.path)

        # The catalog is readable and not writable, and that is a property
        # of the screen rather than of the object on it — so it is checked
        # here, where the screen is known, instead of being left to each view.
        if request.method not in ("GET", "HEAD", "OPTIONS") and name in HELPER_READ_ONLY_URLS:
            raise PermissionDenied(name)
        return None


def _standing_in_for_everyone():
    """The account an unauthenticated request becomes under `noauth`.

    The oldest active administrator, which on a single-person instance is the
    only account there is. Deliberately a real row rather than a synthetic
    superuser: everything this application records — `created_by`, the audit
    log, who closed a work order — names a user, and a phantom that exists only
    in memory would write foreign keys pointing at nothing.

    `None` when the instance has no administrator yet, which leaves the request
    anonymous and lets the first-run setup page do its job. Turning sign-in off
    cannot conjure the account it would sign you in as.
    """
    from .models import Role, User

    return (
        User.objects.filter(is_active=True, role=Role.ADMIN)
        .order_by("date_joined", "id")
        .first()
    )


class NoAuthMiddleware:
    """`PASSWORD_POLICY=noauth`: every request is already signed in.

    **Anyone who can reach the site is the shop.** There is no check of any
    kind here — that is the whole feature, and it is why it is opt-in, named
    plainly, announced in the log at startup, shown on the health screen and
    banner-ed on every page. It is for an instance on a private network where
    the sign-in screen was a toll gate on the way to the only person who was
    ever going to use it.

    A middleware rather than an authentication backend because a backend still
    needs somebody to submit a form: `authenticate()` is only consulted when
    something calls it, and the point here is that nothing does. This runs
    directly after `AuthenticationMiddleware` and only supplies a user where
    that left an anonymous one, so a real session — somebody who signed in
    before the policy changed — is still their own account and not this one.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        signed_in = getattr(request, "user", None)
        # Lazy, like the one it replaces: a request for a static file or the
        # health probe should not cost a user lookup, and this runs on all of
        # them.
        request.user = SimpleLazyObject(lambda: self._resolve(signed_in))
        return self.get_response(request)

    @staticmethod
    def _resolve(signed_in):
        if signed_in is not None and signed_in.is_authenticated:
            return signed_in
        return _standing_in_for_everyone() or signed_in or AnonymousUser()


__all__ = ["HelperGateMiddleware", "NoAuthMiddleware", "HELPER_URLS", "READ_VERBS"]
