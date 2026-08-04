"""Entrypoint for the Findus MCP service: `python -m apps.mcp.server`.

Started as its own process (see docker-compose.yml, service `mcp`), but
shares Django's settings/models/services rather than running a parallel
data-access layer -- only the transport (SSE) is separate from the web
process.
"""

import os

from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from apps.mcp.auth import TokenAuthMiddleware  # noqa: E402
from apps.mcp.tools import mcp_app  # noqa: E402


def main():
    """Runs the Starlette app ourselves (rather than `mcp_app.run(transport="sse")`)

    so `TokenAuthMiddleware` sits in front of every request. `access_log=False`
    is deliberate: uvicorn's default access log would otherwise print the
    `?token=...` query string of every request in plaintext -- the middleware
    only keeps the token out of its own 401 response, it can't stop uvicorn
    from logging the request line before the app even runs.
    """
    import uvicorn

    app = TokenAuthMiddleware(mcp_app.sse_app())
    uvicorn.run(app, host=settings.MCP_HOST, port=settings.MCP_PORT, access_log=False)


if __name__ == "__main__":
    main()
