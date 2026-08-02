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


def env_ingest_folders(name, default=""):
    """Parse `path[:department[:visibility]]` entries, ";"-separated, into
    the list-of-dicts shape `apps.ingest.connectors.folder` expects. Flat
    env-var syntax (not a nested config file) matches how every other
    Findus setting in this file is sourced.
    """
    raw = env(name, default) or ""
    folders = []
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        path, _, rest = entry.partition(":")
        department, _, visibility = rest.partition(":")
        folders.append(
            {
                "path": path.strip(),
                "department": department.strip() or None,
                "visibility": visibility.strip() or "department",
            }
        )
    return folders


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
    "apps.mail",
    "apps.mcp",
    "apps.ingest",
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

# --------------------------------------------------------------------------
# AI provider layer (apps.ai.providers) -- embeddings + generation are
# configured independently, so e.g. embeddings can run locally via Ollama
# while generation uses a cloud provider. See Architektur.md,
# "Provider-Neutralität & Lock-in-Hedges".
# --------------------------------------------------------------------------
FINDUS_AI_EMBEDDING_PROVIDER = env("FINDUS_AI_EMBEDDING_PROVIDER", "ollama")
FINDUS_AI_GENERATION_PROVIDER = env("FINDUS_AI_GENERATION_PROVIDER", "ollama")

FINDUS_AI_TIMEOUT_SECONDS = float(env("FINDUS_AI_TIMEOUT_SECONDS", "30"))
FINDUS_AI_MAX_RETRIES = int(env("FINDUS_AI_MAX_RETRIES", "3"))
FINDUS_AI_RETRY_BACKOFF_SECONDS = float(env("FINDUS_AI_RETRY_BACKOFF_SECONDS", "0.5"))

# Per-provider settings, keyed by the same name used in
# FINDUS_AI_{EMBEDDING,GENERATION}_PROVIDER. `*_model_version` is not read
# back from any provider API (none of the four report it consistently) --
# it's an operator-set tag that travels onto `Chunk.embedding_model_version`
# so a later model swap re-indexes only what actually changed.
FINDUS_AI_PROVIDERS = {
    "openai": {
        "api_key": env("FINDUS_OPENAI_API_KEY", ""),
        "base_url": env("FINDUS_OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "embedding_model": env("FINDUS_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
        "embedding_model_version": env("FINDUS_OPENAI_EMBEDDING_MODEL_VERSION", "1"),
        "generation_model": env("FINDUS_OPENAI_GENERATION_MODEL", "gpt-4o-mini"),
        "generation_model_version": env("FINDUS_OPENAI_GENERATION_MODEL_VERSION", "1"),
    },
    "anthropic": {
        "api_key": env("FINDUS_ANTHROPIC_API_KEY", ""),
        "base_url": env("FINDUS_ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        "generation_model": env("FINDUS_ANTHROPIC_GENERATION_MODEL", "claude-sonnet-5"),
        "generation_model_version": env("FINDUS_ANTHROPIC_GENERATION_MODEL_VERSION", "1"),
        "max_tokens": int(env("FINDUS_ANTHROPIC_MAX_TOKENS", "1024")),
    },
    "gemini": {
        "api_key": env("FINDUS_GEMINI_API_KEY", ""),
        "base_url": env(
            "FINDUS_GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
        ),
        "embedding_model": env("FINDUS_GEMINI_EMBEDDING_MODEL", "text-embedding-004"),
        "embedding_model_version": env("FINDUS_GEMINI_EMBEDDING_MODEL_VERSION", "1"),
        "generation_model": env("FINDUS_GEMINI_GENERATION_MODEL", "gemini-2.5-flash"),
        "generation_model_version": env("FINDUS_GEMINI_GENERATION_MODEL_VERSION", "1"),
    },
    "ollama": {
        "api_key": env("FINDUS_OLLAMA_API_KEY", ""),
        "base_url": env("FINDUS_OLLAMA_BASE_URL", "http://localhost:11434"),
        "embedding_model": env("FINDUS_OLLAMA_EMBEDDING_MODEL", "nomic-embed-text"),
        "embedding_model_version": env("FINDUS_OLLAMA_EMBEDDING_MODEL_VERSION", "1"),
        "generation_model": env("FINDUS_OLLAMA_GENERATION_MODEL", "llama3.1"),
        "generation_model_version": env("FINDUS_OLLAMA_GENERATION_MODEL_VERSION", "1"),
    },
}

# --------------------------------------------------------------------------
# Mail-Versand (apps.mail.backends) -- backend is a config choice, not a
# code change. Backend name must be one of: smtp, graph, fake.
# --------------------------------------------------------------------------
FINDUS_MAIL_BACKEND = env("FINDUS_MAIL_BACKEND", "smtp")
FINDUS_MAIL_FROM_ADDRESS = env("FINDUS_MAIL_FROM_ADDRESS", "findus@localhost")

FINDUS_MAIL_TIMEOUT_SECONDS = float(env("FINDUS_MAIL_TIMEOUT_SECONDS", "30"))
FINDUS_MAIL_MAX_RETRIES = int(env("FINDUS_MAIL_MAX_RETRIES", "3"))
FINDUS_MAIL_RETRY_BACKOFF_SECONDS = float(env("FINDUS_MAIL_RETRY_BACKOFF_SECONDS", "0.5"))

FINDUS_MAIL_BACKENDS = {
    "smtp": {
        "host": env("FINDUS_SMTP_HOST", "localhost"),
        "port": int(env("FINDUS_SMTP_PORT", "587")),
        "username": env("FINDUS_SMTP_USERNAME", ""),
        "password": env("FINDUS_SMTP_PASSWORD", ""),
        "use_tls": env_bool("FINDUS_SMTP_USE_TLS", True),
        "use_ssl": env_bool("FINDUS_SMTP_USE_SSL", False),
    },
    "graph": {
        "tenant_id": env("FINDUS_GRAPH_TENANT_ID", ""),
        "client_id": env("FINDUS_GRAPH_CLIENT_ID", ""),
        "client_secret": env("FINDUS_GRAPH_CLIENT_SECRET", ""),
        # Mailbox to send as: the app registration needs Mail.Send
        # application permission, granted for this user/UPN.
        "sender": env("FINDUS_GRAPH_SENDER", ""),
    },
}

# --------------------------------------------------------------------------
# Ingest: Ordner-Connector (apps.ingest.connectors.folder) -- pollt die
# konfigurierten Eingangsordner, legt neue Dateien als Document an (siehe
# apps.ingest.service) und verschiebt sie nach processed/failed.
# --------------------------------------------------------------------------
FINDUS_INGEST_WATCH_FOLDERS = env_ingest_folders("FINDUS_INGEST_WATCH_FOLDERS", "")
FINDUS_INGEST_ALLOWED_EXTENSIONS = env_list(
    "FINDUS_INGEST_ALLOWED_EXTENSIONS",
    "pdf,png,jpg,jpeg,tif,tiff,docx,txt,eml",
)
FINDUS_INGEST_POLL_INTERVAL_SECONDS = float(env("FINDUS_INGEST_POLL_INTERVAL_SECONDS", "10"))

# --------------------------------------------------------------------------
# Ingest: Mail-Connectoren (apps.ingest.connectors.mail_imap /
# apps.ingest.connectors.mail_graph) -- der eigentliche Wert-Keil: pollt ein
# IMAP- und/oder ein Graph-Postfach, legt Anhänge (+ optional den Mailbody)
# als Document an und markiert verarbeitete Mails als gelesen (Idempotenz,
# kein Extra-Zielordner).
#
# Genau ein Postfach pro Backend, kein Array: Customer-Zero-Prototyp ist
# ein Container pro Kunde (siehe Architektur.md, "Deployment"), also
# dieselbe Dict-Form wie FINDUS_MAIL_BACKENDS oben statt einer Liste.
# --------------------------------------------------------------------------
FINDUS_INGEST_MAIL_SOURCES = {
    "imap": {
        "enabled": env_bool("FINDUS_INGEST_IMAP_ENABLED", False),
        "host": env("FINDUS_INGEST_IMAP_HOST", ""),
        "port": int(env("FINDUS_INGEST_IMAP_PORT", "993")),
        "username": env("FINDUS_INGEST_IMAP_USERNAME", ""),
        "password": env("FINDUS_INGEST_IMAP_PASSWORD", ""),
        "use_ssl": env_bool("FINDUS_INGEST_IMAP_USE_SSL", True),
        "folder": env("FINDUS_INGEST_IMAP_FOLDER", "INBOX"),
        "department": env("FINDUS_INGEST_IMAP_DEPARTMENT", "") or None,
        "visibility": env("FINDUS_INGEST_IMAP_VISIBILITY", "department"),
        "on_duplicate": env("FINDUS_INGEST_IMAP_ON_DUPLICATE", "skip"),
        "ingest_body": env_bool("FINDUS_INGEST_IMAP_INGEST_BODY", True),
    },
    "graph": {
        # Gleiche App-Registrierung wie FINDUS_MAIL_BACKENDS["graph"]
        # (Mail.Send) -- braucht zusätzlich Mail.Read (application) für
        # dieses Postfach, siehe apps.mail.backends.graph.GraphTokenClient.
        "enabled": env_bool("FINDUS_INGEST_GRAPH_ENABLED", False),
        "tenant_id": env("FINDUS_GRAPH_TENANT_ID", ""),
        "client_id": env("FINDUS_GRAPH_CLIENT_ID", ""),
        "client_secret": env("FINDUS_GRAPH_CLIENT_SECRET", ""),
        "mailbox": env("FINDUS_INGEST_GRAPH_MAILBOX", ""),
        "folder": env("FINDUS_INGEST_GRAPH_FOLDER", "inbox"),
        "department": env("FINDUS_INGEST_GRAPH_DEPARTMENT", "") or None,
        "visibility": env("FINDUS_INGEST_GRAPH_VISIBILITY", "department"),
        "on_duplicate": env("FINDUS_INGEST_GRAPH_ON_DUPLICATE", "skip"),
        "ingest_body": env_bool("FINDUS_INGEST_GRAPH_INGEST_BODY", True),
    },
}
FINDUS_INGEST_MAIL_POLL_INTERVAL_SECONDS = float(
    env("FINDUS_INGEST_MAIL_POLL_INTERVAL_SECONDS", "60")
)

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
