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

from apps.mcp.tools import mcp_app  # noqa: E402


def main():
    mcp_app.run(transport="sse")


if __name__ == "__main__":
    main()
