"""
Base settings shared by all environments. Environment-specific values are
read from the process environment (populated from `.env` via python-dotenv
in manage.py / wsgi.py / asgi.py) so nothing environment-specific is
hardcoded here.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    value = env(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    value = env(name, default) or ""
    return [item.strip() for item in value.split(",") if item.strip()]


SECRET_KEY = env("DJANGO_SECRET_KEY", "insecure-dev-key-change-me")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "django_htmx",
    "django_q",
    # Findus apps
    "apps.accounts",
    "apps.documents",
    "apps.ai",
    "apps.mcp",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --------------------------------------------------------------------------
# Custom user model — must be set before the first migration.
# --------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

# --------------------------------------------------------------------------
# Database (PostgreSQL + pgvector)
# --------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "findus"),
        "USER": env("POSTGRES_USER", "findus"),
        "PASSWORD": env("POSTGRES_PASSWORD", "findus"),
        "HOST": env("POSTGRES_HOST", "db"),
        "PORT": env("POSTGRES_PORT", "5432"),
    }
}

# Fixed embedding dimension used across pgvector columns. Changing this
# requires re-embedding the archive (see Architektur.md, "Lock-in-Hedges").
FINDUS_EMBEDDING_DIMENSIONS = int(env("FINDUS_EMBEDDING_DIMENSIONS", "1024"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# Password validation
# --------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --------------------------------------------------------------------------
# Internationalization
# --------------------------------------------------------------------------
LANGUAGE_CODE = "de-de"
TIME_ZONE = env("DJANGO_TIME_ZONE", "Europe/Berlin")
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------------------
# Static & media files
# --------------------------------------------------------------------------
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "mediafiles"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "documents:home"
LOGOUT_REDIRECT_URL = "login"

# --------------------------------------------------------------------------
# Redis: app/semantic cache
# --------------------------------------------------------------------------
REDIS_URL = env("REDIS_URL", "redis://redis:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    }
}

# --------------------------------------------------------------------------
# Background worker: Django-Q2 (not Celery), broker = Redis
# --------------------------------------------------------------------------
Q_CLUSTER = {
    "name": "findus",
    "workers": int(env("FINDUS_WORKER_COUNT", "2")),
    "recycle": 500,
    "timeout": 120,
    "retry": 180,
    "compress": True,
    "label": "Findus Background Tasks",
    "redis": REDIS_URL,
}

# --------------------------------------------------------------------------
# Object storage: original files live outside Postgres (S3 / MinIO).
# --------------------------------------------------------------------------
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

AWS_ACCESS_KEY_ID = env("FINDUS_STORAGE_ACCESS_KEY", "findus")
AWS_SECRET_ACCESS_KEY = env("FINDUS_STORAGE_SECRET_KEY", "findus-secret")
AWS_STORAGE_BUCKET_NAME = env("FINDUS_STORAGE_BUCKET", "findus-documents")
AWS_S3_ENDPOINT_URL = env("FINDUS_STORAGE_ENDPOINT_URL", "http://minio:9000")
AWS_S3_REGION_NAME = env("FINDUS_STORAGE_REGION", "eu-central-1")
AWS_S3_USE_SSL = env_bool("FINDUS_STORAGE_USE_SSL", False)
AWS_S3_ADDRESSING_STYLE = "path"
AWS_S3_FILE_OVERWRITE = False
AWS_DEFAULT_ACL = None

# --------------------------------------------------------------------------
# MCP service
# --------------------------------------------------------------------------
MCP_HOST = env("MCP_HOST", "0.0.0.0")
MCP_PORT = int(env("MCP_PORT", "8001"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "root": {
        "handlers": ["console"],
        "level": env("DJANGO_LOG_LEVEL", "INFO"),
    },
}
