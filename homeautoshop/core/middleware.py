"""Request-scoped current user, so BaseModel can stamp `created_by`."""

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
