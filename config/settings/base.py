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

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,findus.angerlabs.de")
CSRF_TRUSTED_ORIGINS = [
    "https://findus.angerlabs.de",
    "http://localhost:8011",
    "http://127.0.0.1"
]

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
    'whitenoise.middleware.WhiteNoiseMiddleware',
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # After AuthenticationMiddleware so `request.user` is resolved when the
    # request id is attached to the Sentry user context.
    "config.middleware.RequestIDMiddleware",
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
                "apps.documents.context_processors.open_action_status_count",
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
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

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
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
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
# MCP service (#1052: auth baseline). `MCP_HOST` stays "0.0.0.0" -- that's
# the bind address *inside* the container, and docker's port publishing
# can only reach a container process listening on its non-loopback
# interface. Restricting exposure to "local only" therefore happens one
# level up, in docker-compose.yml's port mapping, not here (see that
# file's `mcp` service).
# --------------------------------------------------------------------------
MCP_HOST = env("MCP_HOST", "0.0.0.0")
MCP_PORT = int(env("MCP_PORT", "8001"))
# No insecure default: empty/missing means `apps.mcp.auth` rejects every
# request (a candidate token can never equal an empty expected token).
MCP_TOKEN = env("MCP_TOKEN", "")
# The single Django identity MCP tools run as -- see `apps.mcp.auth.get_mcp_user`.
MCP_USER_USERNAME = env("MCP_USER_USERNAME", "")

# --------------------------------------------------------------------------
# AI provider layer (apps.ai.providers) -- embeddings, generation and vision
# (describe_image) are configured independently, so e.g. embeddings can run
# locally via Ollama while generation uses a cloud provider. Vision defaults
# to local llava/Ollama, since document images carry the same DSGVO concern
# as generation prompts (see Architektur.md, "Provider-Neutralität &
# Lock-in-Hedges" and "LLM-Anbindung").
# --------------------------------------------------------------------------
FINDUS_AI_EMBEDDING_PROVIDER = env("FINDUS_AI_EMBEDDING_PROVIDER", "ollama")
FINDUS_AI_GENERATION_PROVIDER = env("FINDUS_AI_GENERATION_PROVIDER", "ollama")
FINDUS_AI_VISION_PROVIDER = env("FINDUS_AI_VISION_PROVIDER", "ollama")

FINDUS_AI_TIMEOUT_SECONDS = float(env("FINDUS_AI_TIMEOUT_SECONDS", "30"))
FINDUS_AI_MAX_RETRIES = int(env("FINDUS_AI_MAX_RETRIES", "3"))
FINDUS_AI_RETRY_BACKOFF_SECONDS = float(env("FINDUS_AI_RETRY_BACKOFF_SECONDS", "0.5"))

# --------------------------------------------------------------------------
# Chunking (apps.documents.chunking / apps.documents.processing): how
# `Document.text_content` is split before embedding. "Tokens" here are
# whitespace-split words, not a vendor tokenizer -- the embedding provider
# is swappable, so the chunk boundary must not depend on any one vendor's
# tokenizer (see apps.ai.providers).
# --------------------------------------------------------------------------
FINDUS_CHUNK_SIZE_TOKENS = int(env("FINDUS_CHUNK_SIZE_TOKENS", "500"))
FINDUS_CHUNK_OVERLAP_TOKENS = int(env("FINDUS_CHUNK_OVERLAP_TOKENS", "50"))
FINDUS_CHUNK_EMBEDDING_BATCH_SIZE = int(env("FINDUS_CHUNK_EMBEDDING_BATCH_SIZE", "64"))

# --------------------------------------------------------------------------
# KI-Analyse (apps.documents.analysis, #1020): ein `generate()`-Call pro
# Dokument auf dem bereits extrahierten Text (kostenbewusst -- kein
# zusätzlicher Vision-Call). MAX_CHARS begrenzt den Prompt bei sehr langen
# Dokumenten, statt das komplette `text_content` unbeschränkt mitzusenden.
# --------------------------------------------------------------------------
FINDUS_ANALYSIS_MAX_CHARS = int(env("FINDUS_ANALYSIS_MAX_CHARS", "12000"))

# Kontakte im Analyse-Prompt (#1048): bestehende `Correspondent`s als
# Kontext, damit die KI Aussteller/Empfaenger gegen bekannte Identitaeten
# (inkl. is_self) abgleicht statt Duplikate zu erfinden. Begrenzt, damit ein
# gewachsener Kontaktbestand den Prompt/die Kosten nicht unbeschraenkt
# aufblaeht -- is_self-Kontakte gehen dabei immer zuerst rein (siehe
# apps.documents.analysis._correspondent_context_lines).
FINDUS_ANALYSIS_MAX_CONTACTS = int(env("FINDUS_ANALYSIS_MAX_CONTACTS", "200"))

# --------------------------------------------------------------------------
# Extraction cascade (apps.documents.extraction, #1009): text-layer -> OCR
# -> vision, cheapest usable stage wins. A page only escalates to the next
# (more expensive) stage when the current one produced fewer than
# MIN_CHARS_PER_PAGE characters, or -- for OCR -- when its mean per-word
# confidence is also below MIN_OCR_CONFIDENCE; vision is the last resort,
# never the first call. OCR itself needs the tesseract-ocr + poppler-utils
# system packages (see Dockerfile) -- pytesseract/pdf2image are just their
# Python bindings.
# --------------------------------------------------------------------------
FINDUS_EXTRACTION_MIN_CHARS_PER_PAGE = int(env("FINDUS_EXTRACTION_MIN_CHARS_PER_PAGE", "20"))
FINDUS_EXTRACTION_MIN_OCR_CONFIDENCE = float(env("FINDUS_EXTRACTION_MIN_OCR_CONFIDENCE", "60"))
FINDUS_EXTRACTION_OCR_LANGUAGES = env("FINDUS_EXTRACTION_OCR_LANGUAGES", "deu+eng")
FINDUS_EXTRACTION_PDF_RENDER_DPI = int(env("FINDUS_EXTRACTION_PDF_RENDER_DPI", "200"))

# Per-provider settings, keyed by the same name used in
# FINDUS_AI_{EMBEDDING,GENERATION,VISION}_PROVIDER. `*_model_version` is not
# read back from any provider API (none of them report it consistently) --
# it's an operator-set tag that travels onto `Chunk.embedding_model_version`
# so a later model swap re-indexes only what actually changed. `vision_model`
# is only read by providers registered for vision (openai, ollama) -- see
# apps.ai.providers.registry._VISION_BUILDERS.
FINDUS_AI_PROVIDERS = {
    "openai": {
        "api_key": env("FINDUS_OPENAI_API_KEY", ""),
        "base_url": env("FINDUS_OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "embedding_model": env("FINDUS_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
        "embedding_model_version": env("FINDUS_OPENAI_EMBEDDING_MODEL_VERSION", "1"),
        "generation_model": env("FINDUS_OPENAI_GENERATION_MODEL", "gpt-4o-mini"),
        "generation_model_version": env("FINDUS_OPENAI_GENERATION_MODEL_VERSION", "1"),
        "vision_model": env("FINDUS_OPENAI_VISION_MODEL", "gpt-4o-mini"),
        "vision_model_version": env("FINDUS_OPENAI_VISION_MODEL_VERSION", "1"),
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
        "vision_model": env("FINDUS_OLLAMA_VISION_MODEL", "llava"),
        "vision_model_version": env("FINDUS_OLLAMA_VISION_MODEL_VERSION", "1"),
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
# Ingest: UI-Upload (#1019) -- reuses `FINDUS_INGEST_ALLOWED_EXTENSIONS`
# above for the type check, adds an upload-specific size cap (the folder/
# mail connectors don't need one: those files already sit on trusted infra).
# --------------------------------------------------------------------------
FINDUS_UPLOAD_MAX_SIZE_MB = float(env("FINDUS_UPLOAD_MAX_SIZE_MB", "25"))

# --------------------------------------------------------------------------
# Ingest: Mail-Connectoren (apps.ingest.connectors.mail_imap /
# apps.ingest.connectors.mail_graph) -- der eigentliche Wert-Keil: pollt ein
# IMAP- und/oder ein Graph-Postfach und legt je Anhang ein Document an; nur
# wenn eine Mail *keine* Anhänge hat, wird stattdessen der Mailbody selbst
# zum Document (steuerbar über "ingest_body", sonst ginge die Info
# verloren). Verarbeitete Mails werden als gelesen markiert (Idempotenz,
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

# Mail-Body als Leitdokument (#1070): Mindest-Wortzahl im bereinigten
# Body (nach Entfernen von Zitat-Verlauf/Signatur/Tracking), ab der er
# als eigenständiger Inhalt gilt. Darunter bleibt das Leitdokument eine
# dünne Hülle (nur Metadaten, kein Body-PDF/Embedding) -- die Schwelle
# entscheidet also "Body-Inhalt füllen ja/nein", nicht "Dokument ja/nein".
FINDUS_MAIL_BODY_MIN_WORDS = int(env("FINDUS_MAIL_BODY_MIN_WORDS", "8"))

# --------------------------------------------------------------------------
# Mail-Ingest: Grampf-Filter fuer Anhaenge (#1081). Typische Signatur-/Deko-
# Bilder (LinkedIn/Instagram/Facebook-Logos, winzige Inline-Grafiken,
# Tracking-Pixel) sollen gar nicht erst als Unterdokumente entstehen.
# Sicherheitsnetz: es werden ausschliesslich *Bilder* (content_type image/*)
# verworfen -- PDFs/Office/echte Belege nie. Greift in IMAP- und Graph-Pfad
# (siehe apps.ingest.attachment_filter), Schwellen hier justierbar, `ENABLED`
# schaltet den Filter komplett ab (dann bleibt alles wie vor #1081).
#   - inline/`cid:`-referenzierte Bilder werden immer verworfen,
#   - Bilder unter MIN_IMAGE_BYTES ebenso (0 = Groessen-Check aus),
#   - Bilder kleiner als MIN_IMAGE_DIMENSION px (Breite oder Hoehe) ebenso;
#     das faengt auch 1px-Tracking-Pixel (0 = Masse-Check aus).
# --------------------------------------------------------------------------
FINDUS_INGEST_ATTACHMENT_FILTER_ENABLED = env_bool(
    "FINDUS_INGEST_ATTACHMENT_FILTER_ENABLED", True
)
FINDUS_INGEST_ATTACHMENT_MIN_IMAGE_BYTES = int(
    env("FINDUS_INGEST_ATTACHMENT_MIN_IMAGE_BYTES", str(20 * 1024))
)
FINDUS_INGEST_ATTACHMENT_MIN_IMAGE_DIMENSION = int(
    env("FINDUS_INGEST_ATTACHMENT_MIN_IMAGE_DIMENSION", "200")
)

# --------------------------------------------------------------------------
# Ingest: Mail-Trigger-Intervall der django-q2-Schedule "mail-ingest" (#1060,
# siehe apps.ingest.schedules) -- unabhängig von
# FINDUS_INGEST_MAIL_POLL_INTERVAL_SECONDS oben, das den Poll-Abstand des
# dauerhaft laufenden `watch_mail_ingest`-Prozesses steuert; hier geht es um
# den Minuten-Takt, in dem der qcluster-Scheduler `watch_mail_ingest --once`
# auslöst.
# --------------------------------------------------------------------------
FINDUS_MAIL_POLL_MINUTES = int(env("FINDUS_MAIL_POLL_MINUTES", "5"))

# --------------------------------------------------------------------------
# Logging: console (for container stdout) + one dated file per day under
# ./logs, retained 7 days. See config.logging_utils for the handler and the
# request-id filter. The level is env-driven (DEBUG/INFO/WARNING/ERROR ...);
# handlers are pinned to DEBUG so raising DJANGO_LOG_LEVEL is the only knob.
# --------------------------------------------------------------------------
LOG_DIR = BASE_DIR / "logs"
# Ensure ./logs exists before dictConfig opens the file handler; no crash if
# it is already there.
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOG_LEVEL = env("DJANGO_LOG_LEVEL", "INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {
            "()": "config.logging_utils.RequestIDFilter",
        },
    },
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "filters": ["request_id"],
        },
        "file": {
            "class": "config.logging_utils.DailyRotatingFileHandler",
            "log_dir": str(LOG_DIR),
            "prefix": "app",
            "backup_count": 7,
            "level": "DEBUG",
            "formatter": "standard",
            "filters": ["request_id"],
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        # Django's own logs and every app logger (getLogger("apps....")) share
        # both handlers; propagate=False keeps them off the root a second time.
        "django": {
            "handlers": ["console", "file"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "apps": {
            "handlers": ["console", "file"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
    },
}

# --------------------------------------------------------------------------
# Sentry: error monitoring for unhandled exceptions and Django errors. Active
# only when SENTRY_DSN is set -- with no DSN, sentry_sdk.init is never called
# and the app runs untouched. The DjangoIntegration reports exceptions in
# addition to Django's normal handling, so errors still propagate as usual
# (no try/except swallows them).
# --------------------------------------------------------------------------
SENTRY_DSN = env("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        environment=env("SENTRY_ENVIRONMENT", env("DJANGO_ENV", "dev")),
        release=env("SENTRY_RELEASE") or None,
        # Performance/PII are opt-in and off by default: error monitoring is
        # the requirement, and PII stays off unless an operator enables it.
        traces_sample_rate=float(env("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        send_default_pii=env_bool("SENTRY_SEND_DEFAULT_PII", False),
    )
