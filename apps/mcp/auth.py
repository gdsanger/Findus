"""Query-string token auth for the MCP SSE endpoint (#1052).

FastMCP's built-in `auth`/`token_verifier` is header- and OAuth-oriented;
it has no concept of a query-string credential. Claude Desktop (the MCP
client) can't attach a custom `Authorization` header to an SSE connection,
so the query string is the only place it can put a token -- hence this
standalone ASGI middleware in front of `mcp_app.sse_app()` instead of
FastMCP's own auth hook.
"""

import hmac
from urllib.parse import parse_qsl

from django.conf import settings
from starlette.responses import JSONResponse


def _extract_token(scope):
    query_params = dict(parse_qsl((scope.get("query_string") or b"").decode("latin-1")))
    token = query_params.get("token")
    if token:
        return token

    headers = dict(scope.get("headers") or ())
    auth_header = headers.get(b"authorization", b"").decode("latin-1")
    if auth_header.lower().startswith("bearer "):
        return auth_header[len("bearer ") :].strip()

    return None


def _token_is_valid(candidate):
    expected = settings.MCP_TOKEN
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate, expected)


class TokenAuthMiddleware:
    """Rejects any HTTP request that doesn't carry a valid `MCP_TOKEN`,

    before it ever reaches the SSE transport -- the query string is
    stripped out of the JSON error body and is never logged (the MCP
    process also runs uvicorn with `access_log=False`, see server.py, so
    the token never round-trips into a log line either way).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not _token_is_valid(_extract_token(scope)):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def get_mcp_user():
    """The single, fixed Django identity every MCP tool runs as (#1052).

    MCP has no per-request login -- one token authenticates the whole
    process to one Django user, set via `MCP_USER_USERNAME`. Every future
    tool that reads Findus data must scope its query through
    `.visible_to(get_mcp_user())`, the same call web views make, so a
    department/private boundary can't be bypassed just because the caller
    is MCP instead of the browser.
    """
    from django.contrib.auth import get_user_model

    username = settings.MCP_USER_USERNAME
    if not username:
        raise RuntimeError("MCP_USER_USERNAME is not configured -- refusing to run MCP tools without an identity.")
    return get_user_model().objects.get(username=username)
