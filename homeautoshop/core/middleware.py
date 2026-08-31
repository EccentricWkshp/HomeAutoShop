"""Request-scoped current user, and applying stored configuration (R-9, §17.2)."""

from __future__ import annotations

import contextvars

_current_user: contextvars.ContextVar = contextvars.ContextVar("current_user", default=None)


def get_current_user():
    return _current_user.get()


class CurrentUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        token = _current_user.set(user if user and user.is_authenticated else None)
        try:
            return self.get_response(request)
        finally:
            _current_user.reset(token)


class ConfigMiddleware:
    """Apply the stored restart-class settings, once, before serving anything.

    §17.2 divides settings into those that take effect immediately — read
    through `conf` at the moment they are needed — and those Django resolves
    for itself at startup: the locale, the upload ceiling, the timezone. The
    second group has to be written into `django.conf.settings` before Django
    reads them, and this is the first point in a request where the database is
    guaranteed to exist.

    Placed early in `MIDDLEWARE` for one specific reason: `LocaleMiddleware`
    reads `settings.LANGUAGE_CODE`, so an overlay applied after it would take
    an extra request to show up and look like a bug in the language setting.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .runtime import ensure_overlay, invalidate

        # Start each request from the database rather than from whatever the
        # last one left cached. The cache exists to stop a page render asking
        # the same question a dozen times, and one read per request does that
        # just as well — while removing a second-long window in which a
        # setting that was just saved is still reported as its old value.
        #
        # It also makes the cache follow a transaction rollback, which under
        # test it otherwise cannot: a case that saves a setting used to leave
        # the value visible to the next case, whose database no longer had it.
        invalidate()
        ensure_overlay()
        return self.get_response(request)


class KeepYourPlaceMiddleware:
    """Land a redirect where the form was, not at the top of the page.

    Post-redirect-get is right and stays, but it costs the reader their place:
    ticking the fourth job item on a long work order reloads the page and puts
    them back at the top, hunting for the row they were on. A script fixes that
    properly by never navigating at all (`static/liveform.js`) — this is the
    half that works with no script.

    A form inside a live region carries `_anchor`, and any redirect that answers
    it gains that fragment. Done here rather than in each view because it is
    true of every one of them, and forty views each remembering to append a
    fragment is thirty-nine chances to forget.

    Only same-page redirects are touched. A delete that returns to a list is
    going somewhere the anchor does not exist, and scrolling to a missing target
    is at best nothing and at worst a jump to somewhere arbitrary.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.method != "POST":
            return response
        anchor = (request.POST.get("_anchor") or "").strip()
        location = response.headers.get("Location", "") if response else ""
        if not anchor or not location or "#" in location:
            return response
        if not _is_same_page(request, location):
            return response
        # Fragment-safe characters only: this ends up in a header, and an id is
        # a short slug in every template that sets one.
        if not anchor.replace("-", "").replace("_", "").isalnum():
            return response
        response.headers["Location"] = f"{location}#{anchor}"
        return response


def _is_same_page(request, location: str) -> bool:
    """Whether the redirect goes back to the page the form was posted from."""
    referer = request.META.get("HTTP_REFERER") or ""
    if not referer:
        return False
    from urllib.parse import urlparse

    here = urlparse(referer)
    there = urlparse(location)
    if there.netloc and there.netloc != here.netloc:
        return False
    return (there.path or here.path) == here.path
