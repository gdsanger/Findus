"""
Cross-cutting request middleware.

`RequestIDMiddleware` gives every request a correlation id (taken from an
inbound `X-Request-ID`/`X-Correlation-ID` header when a proxy already set one,
otherwise generated), publishes it to logging via `request_id_var`, echoes it
back on the response, and -- when Sentry is active -- tags the event and sets
the user context so an exception can be traced to a request and a person.
"""

import uuid

import sentry_sdk
from django.conf import settings

from config.logging_utils import request_id_var


class RequestIDMiddleware:
    """Bind a request id for the lifetime of the request (logging + Sentry)."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.sentry_enabled = bool(getattr(settings, "SENTRY_DSN", ""))

    def __call__(self, request):
        request_id = (
            request.headers.get("X-Request-ID")
            or request.headers.get("X-Correlation-ID")
            or uuid.uuid4().hex
        )
        request.request_id = request_id
        token = request_id_var.set(request_id)

        if self.sentry_enabled:
            sentry_sdk.set_tag("request_id", request_id)
            user = getattr(request, "user", None)
            if user is not None and user.is_authenticated:
                sentry_sdk.set_user(
                    {"id": user.id, "email": getattr(user, "email", "") or ""}
                )

        try:
            response = self.get_response(request)
        finally:
            # Reset regardless of outcome so the id never leaks onto the next
            # request handled by the same worker/thread. This restores the
            # previous value rather than swallowing anything -- exceptions from
            # the view still propagate to Django (and thus to Sentry).
            request_id_var.reset(token)

        response["X-Request-ID"] = request_id
        return response
