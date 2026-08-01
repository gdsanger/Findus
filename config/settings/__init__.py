import os

_env = os.environ.get("DJANGO_ENV", "dev")

if _env == "prod":
    from .prod import *  # noqa: F401,F403
else:
    from .dev import *  # noqa: F401,F403
