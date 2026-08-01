from .base import *  # noqa: F401,F403
from .base import env_bool

DEBUG = env_bool("DJANGO_DEBUG", True)

INTERNAL_IPS = ["127.0.0.1"]
