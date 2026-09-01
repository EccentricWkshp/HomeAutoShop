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

from django.core.exceptions import PermissionDenied

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


__all__ = ["HelperGateMiddleware", "HELPER_URLS", "READ_VERBS"]
