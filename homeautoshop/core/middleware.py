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
