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
                "apps.documents.context_processors.due_follow_up_count",
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
#
# `timeout` is the *default* wall-clock budget a task gets before Django-Q
# kills the worker process running it -- kept short so a genuinely hung
# ingest/thumbnail job fails fast instead of tying up a worker slot. LLM
# jobs whose provider call can legitimately run long (retries included)
# override it per-task via `async_task(..., timeout=...)`, e.g.
# `FINDUS_VORGANG_RECOMMENDATION_TASK_TIMEOUT_SECONDS` below (#1134).
#
# `retry` MUST stay greater than the largest `timeout` in use across the
# cluster -- this one's cluster-wide, there is no per-task override.
# Get it backwards and Django-Q requeues a task while the *first* attempt
# is still running (its broker-visibility window expired before the task
# itself did), so a slow-but-alive job runs twice: doubled provider calls,
# doubled token cost, worst case an unbounded loop. Checked in
# `apps.documents.test_contracts.QClusterRetryOutlivesTimeoutTests`.
# --------------------------------------------------------------------------
Q_CLUSTER = {
    "name": "findus",
    "workers": int(env("FINDUS_WORKER_COUNT", "2")),
    "recycle": 500,
    "timeout": 120,
    "retry": 900,
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
    # CompressedStaticFilesStorage bewusst ohne Manifest (#1104): kein
    # collectstatic-Schritt im Deploy-Ablauf (Dockerfile/docker-compose),
    # Manifest-Storage würde {% static %} beim Fehlen der manifest.json
    # hart brechen lassen.
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
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
# Ähnliche Dokumente (#1088, apps.documents.retrieval.similar_documents):
# reine Vektor-Ähnlichkeit über die vorhandenen Chunk-Embeddings, kein
# zusätzlicher LLM-Call. LIMIT ist das Cap N pro Dokument-Detail,
# MIN_SCORE der Relevanz-Schwellwert (Kosinus-Ähnlichkeit, 1.0 =
# identische Richtung) -- lieber ein leerer Block als eine Liste
# beliebiger Dokumente, deshalb bewusst eher streng. CANDIDATE_CHUNKS
# begrenzt, wie viele Chunk-Zeilen die kNN-Query überhaupt liefert, bevor
# auf Dokument-Ebene aggregiert wird: ein Dokument kann mehrere Chunks
# unter den besten Treffern haben, deshalb deutlich mehr als LIMIT.
# --------------------------------------------------------------------------
FINDUS_SIMILAR_DOCUMENTS_LIMIT = int(env("FINDUS_SIMILAR_DOCUMENTS_LIMIT", "5"))
FINDUS_SIMILAR_DOCUMENTS_MIN_SCORE = float(env("FINDUS_SIMILAR_DOCUMENTS_MIN_SCORE", "0.7"))
FINDUS_SIMILAR_DOCUMENTS_CANDIDATE_CHUNKS = int(
    env("FINDUS_SIMILAR_DOCUMENTS_CANDIDATE_CHUNKS", "200")
)

# Auswahlliste für den manuellen Querverweis (#1088) im Dokument-Detail:
# die zuletzt erfassten N sichtbaren Dokumente. Begrenzt, weil ein
# gewachsenes Archiv sonst tausende <option>s in jede Detailseite
# rendern würde -- ältere Dokumente werden stattdessen über den
# "Verknüpfen"-Button an einem Ähnlichkeits-Treffer verlinkt.
FINDUS_DOCUMENT_LINK_PICKER_LIMIT = int(env("FINDUS_DOCUMENT_LINK_PICKER_LIMIT", "200"))

# Verwandte Dokumente über gemeinsame Kennungen (#1099,
# apps.documents.references): Cap je Kennung im Dokument-Detail. Anders
# als bei der Ähnlichkeit ist das *kein* Relevanz-Schwellwert -- ein
# exakter Treffer ist immer relevant -- sondern nur eine Anzeigegrenze für
# den Ausreißer (eine IBAN kann an hunderten Rechnungen hängen). Was
# abgeschnitten wurde, sagt der Block, statt es zu verschweigen.
FINDUS_REFERENCE_MATCH_LIMIT = int(env("FINDUS_REFERENCE_MATCH_LIMIT", "20"))

# Auto-Zuordnung über Kennungen (#1100, apps.documents.reference_matching).
# Default aus: der Regelfall bleibt der Vorschlag mit einem Klick. Eine
# still danebengegangene Auto-Zuordnung fällt niemandem auf -- das Dokument
# liegt ja irgendwo --, während ein unzugeordnetes Dokument sichtbar
# offen ist. Wer den Schalter umlegt, kauft Bequemlichkeit gegen genau
# dieses Risiko.
FINDUS_REFERENCE_AUTO_ASSIGN = env_bool("FINDUS_REFERENCE_AUTO_ASSIGN", False)

# Welche Kennungsarten für die Auto-Zuordnung als "stark" gelten: der Wert
# identifiziert die Gegenseite für sich allein, ohne dass man den
# Nummernkreis dahinter kennen muss. Rechnungs-/Belegnummern stehen
# bewusst nicht drin -- "2024/17" vergibt jeder Rechnungssteller einmal im
# Jahr, und ein Zahlendreher träfe damit die falsche Akte.
FINDUS_REFERENCE_AUTO_ASSIGN_TYPES = env_list(
    "FINDUS_REFERENCE_AUTO_ASSIGN_TYPES", "aktenzeichen,iban"
)

# Fokus-Graph (#1091, apps.documents.graph): Cap für die Nachbarn *einer*
# Expansion und je Kantengruppe -- ein Vorgang mit 400 Dokumenten soll
# beim Aufklappen keine unlesbare Wolke erzeugen. Was abgeschnitten wurde,
# meldet der Endpunkt als `truncated`, damit die UI es sagen kann, statt
# es zu verschweigen. Die Ähnlichkeitskanten benutzen bewusst *kein*
# eigenes Limit, sondern FINDUS_SIMILAR_DOCUMENTS_LIMIT/-MIN_SCORE von
# oben: Graph und Dokument-Detail sollen nicht zwei verschiedene
# Vorstellungen davon haben, was "ähnlich" heißt.
FINDUS_GRAPH_NEIGHBOR_LIMIT = int(env("FINDUS_GRAPH_NEIGHBOR_LIMIT", "25"))

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

# Kennungen je Dokument aus der Analyse (#1099): Obergrenze an
# `DocumentReference`-Zeilen, die *ein* Analyse-Lauf anlegen darf. Ein
# Dokument trägt realistisch eine Handvoll Kennungen -- das Cap fängt die
# ausufernde Modellantwort ab, die jede Ziffernfolge im Text für eine
# Referenznummer hält, statt sie ins Archiv zu schreiben.
FINDUS_ANALYSIS_MAX_REFERENCES = int(env("FINDUS_ANALYSIS_MAX_REFERENCES", "12"))

# Plausibilitätsprüfung des Dokumentdatums (#1141,
# apps.documents.document_dates): Fällt das abgeleitete Dokumentdatum in
# dieses Fenster um den Upload-Tag und nennt das Dokument zugleich eine
# andere Datumsquelle, gewinnt die andere Quelle -- der beobachtete Fall
# waren Kontoauszüge, deren "Erstellt am" der Tag des Hochladens war. 1 Tag
# statt 0, damit ein am Vortag gezogener Auszug genauso erkannt wird; 0
# schaltet die Prüfung auf exakt den Upload-Tag ein.
FINDUS_DOCUMENT_DATE_UPLOAD_TOLERANCE_DAYS = int(
    env("FINDUS_DOCUMENT_DATE_UPLOAD_TOLERANCE_DAYS", "1")
)

# --------------------------------------------------------------------------
# Handlungsempfehlungen je Vorgang (#1093,
# apps.documents.recommendations): ein `generate()`-Call pro Generierung,
# nur auf Knopfdruck. Datenbasis sind die Zusammenfassungen/Key-Facts der
# Dokumente (#1020), NICHT deren Volltexte -- MAX_DOCUMENTS begrenzt, wie
# viele Dokumente ein Vorgang in den Prompt schickt (die jüngsten, danach
# wieder chronologisch), MAX_SUMMARY_CHARS die Länge je Zusammenfassung.
# Wurde gekürzt, steht das in `based_on["truncated"]` und damit im Panel --
# eine stille Kürzung wäre eine Lüge über die Datenbasis. MAX_ITEMS ist
# die Obergrenze an Empfehlungen pro Lauf (im Prompt genannt und beim
# Parsen hart durchgesetzt, damit eine ausufernde Modellantwort keine
# 200-Zeilen-Liste anlegt).
# --------------------------------------------------------------------------
FINDUS_VORGANG_RECOMMENDATION_MAX_DOCUMENTS = int(
    env("FINDUS_VORGANG_RECOMMENDATION_MAX_DOCUMENTS", "40")
)
FINDUS_VORGANG_RECOMMENDATION_MAX_SUMMARY_CHARS = int(
    env("FINDUS_VORGANG_RECOMMENDATION_MAX_SUMMARY_CHARS", "800")
)
FINDUS_VORGANG_RECOMMENDATION_MAX_ITEMS = int(
    env("FINDUS_VORGANG_RECOMMENDATION_MAX_ITEMS", "8")
)

# Wie viele der juengsten Kommentare (ueber alle Dokumente des Vorgangs
# hinweg) zusaetzlich in den Prompt wandern (#1132) -- offene Wiedervorlagen
# zaehlen dabei nicht mit und sind unabhaengig von ihrem Alter immer dabei
# (apps.documents.recommendations._limited_comments), ein Vorgang mit
# hundert Notizen soll den Prompt trotzdem nicht sprengen.
FINDUS_VORGANG_RECOMMENDATION_MAX_COMMENTS = int(
    env("FINDUS_VORGANG_RECOMMENDATION_MAX_COMMENTS", "30")
)

# Output-Budget für den Empfehlungs-Call (#1096). Die Antwort ist ein
# JSON-Objekt aus Lage-Einschätzung *und* bis zu MAX_ITEMS Empfehlungen mit
# Begründung, Frist, Priorität und Quellen -- reichlich 2.000 Tokens, bevor
# das Objekt überhaupt geschlossen ist. Mit dem alten Provider-Default von
# 1.024 Tokens brach die Antwort mitten in der Lage-Einschätzung ab und die
# Empfehlungsliste kam nie an; das Panel zeigte daraufhin "keine
# Empfehlungen". Deshalb hier ein eigener, großzügiger Wert statt des
# Provider-Defaults: er wird nur ausgeschöpft, wenn wirklich so viel
# geschrieben wird (Output-Tokens werden nach Verbrauch abgerechnet, nicht
# nach Reservierung). Reicht er doch nicht, wiederholt `generate_json` den
# Call mit dem doppelten Budget, statt das abgeschnittene JSON zu flicken.
FINDUS_VORGANG_RECOMMENDATION_MAX_OUTPUT_TOKENS = int(
    env("FINDUS_VORGANG_RECOMMENDATION_MAX_OUTPUT_TOKENS", "4000")
)

# Task-Timeout fuer den Empfehlungs-Job (#1134): ueberschreibt den
# kurzen `Q_CLUSTER["timeout"]`-Default fuer genau diesen `async_task()`-
# Aufruf (`vorgang_views.vorgang_recommendations_generate`). Muss die
# Provider-Schicht bequem ueberstehen: `generate_json()` kann `generate()`
# bis zu zweimal aufrufen (Retry bei abgeschnittener/unparsbarer Antwort,
# #1096), jeder Call wiederum bis zu `1 + FINDUS_AI_MAX_RETRIES`-mal je
# `FINDUS_AI_TIMEOUT_SECONDS` (apps.ai.providers.base.with_retry) -- macht
# mit den Standardwerten (30s, 3 Retries) rechnerisch ~250s im
# schlechtesten Fall, plus Backoff. 600s lassen davon reichlich Luft, ohne
# den Worker unbegrenzt zu blockieren. Muss kleiner sein als
# `Q_CLUSTER["retry"]` (siehe dortiger Kommentar) -- sonst laeuft der
# Job doppelt.
FINDUS_VORGANG_RECOMMENDATION_TASK_TIMEOUT_SECONDS = int(
    env("FINDUS_VORGANG_RECOMMENDATION_TASK_TIMEOUT_SECONDS", "600")
)

# Obergrenze fuers Panel-Polling (#1134,
# apps.documents.recommendations.expire_if_stalled): verschwindet der
# Worker-Prozess spurlos (Neustart, OOM-Kill), bevor er selbst oder der
# Django-Q-`hook` den Lauf auf `failed` setzen kann, wuerde der Spinner
# sonst unbegrenzt weiterdrehen. Groesser als
# FINDUS_VORGANG_RECOMMENDATION_TASK_TIMEOUT_SECONDS, damit ein Lauf, der
# noch regulaer laeuft, nicht faelschlich als haengengeblieben gilt.
FINDUS_VORGANG_RECOMMENDATION_POLL_TIMEOUT_SECONDS = int(
    env("FINDUS_VORGANG_RECOMMENDATION_POLL_TIMEOUT_SECONDS", "1200")
)

# --------------------------------------------------------------------------
# Ausfuehrliche Zusammenfassung (#1135, apps.documents.long_summary): ein
# `generate()`-Call pro Erzeugung, nur auf Knopfdruck -- wie die
# Handlungsempfehlungen, aber je Dokument bzw. je Vorgang statt nur je
# Vorgang. MAX_CHARS begrenzt den vollen Dokumenttext im Prompt (deutlich
# grosszuegiger als FINDUS_ANALYSIS_MAX_CHARS: dieser Text wird gelesen
# und aufbewahrt statt nur die Datenbasis eines schnellen Drei-Saetze-Calls
# zu sein). MAX_COMMENTS begrenzt die "Notizen des Nutzers"-Sektion des
# einzelnen Dokuments.
# --------------------------------------------------------------------------
FINDUS_LONG_SUMMARY_MAX_CHARS = int(env("FINDUS_LONG_SUMMARY_MAX_CHARS", "40000"))
FINDUS_LONG_SUMMARY_MAX_COMMENTS = int(env("FINDUS_LONG_SUMMARY_MAX_COMMENTS", "30"))

# Output-Budget (#1096-Lehre: lieber grosszuegig, Output-Tokens werden nach
# Verbrauch abgerechnet, nicht nach Reservierung) -- die Antwort ist ein
# vollstaendiger Fliesstext, potenziell laenger als die Handlungsempfehlungen.
FINDUS_LONG_SUMMARY_MAX_OUTPUT_TOKENS = int(
    env("FINDUS_LONG_SUMMARY_MAX_OUTPUT_TOKENS", "4000")
)

# Task-/Poll-Timeout wie bei den Handlungsempfehlungen (#1134): derselbe
# Nesting-Grundsatz (HTTP-Timeout * Versuche < Task-Timeout < Poll-Timeout <
# Q_CLUSTER["retry"]) gilt hier unveraendert, siehe
# `apps.documents.test_contracts.QClusterRetryOutlivesTimeoutTests`.
FINDUS_LONG_SUMMARY_TASK_TIMEOUT_SECONDS = int(
    env("FINDUS_LONG_SUMMARY_TASK_TIMEOUT_SECONDS", "600")
)
FINDUS_LONG_SUMMARY_POLL_TIMEOUT_SECONDS = int(
    env("FINDUS_LONG_SUMMARY_POLL_TIMEOUT_SECONDS", "1200")
)

# Vorgang-Ebene derselben Funktion: Datenbasis sind die sichtbaren Dokumente
# des Vorgangs mit ihren Kurzzusammenfassungen (nicht deren Volltexte --
# ein Vorgang mit 30 Dokumenten wuerde sonst jeden Kontextrahmen sprengen)
# plus, sofern vorhanden, die bereits erzeugte ausfuehrliche Zusammenfassung
# einzelner Dokumente. MAX_DOCUMENT_LONG_SUMMARY_CHARS begrenzt Letztere
# eigens, weil eine ausfuehrliche Zusammenfassung deutlich laenger ist als
# die Kurzzusammenfassung, die MAX_SUMMARY_CHARS begrenzt.
FINDUS_VORGANG_LONG_SUMMARY_MAX_DOCUMENTS = int(
    env("FINDUS_VORGANG_LONG_SUMMARY_MAX_DOCUMENTS", "40")
)
FINDUS_VORGANG_LONG_SUMMARY_MAX_SUMMARY_CHARS = int(
    env("FINDUS_VORGANG_LONG_SUMMARY_MAX_SUMMARY_CHARS", "800")
)
FINDUS_VORGANG_LONG_SUMMARY_MAX_DOCUMENT_LONG_SUMMARY_CHARS = int(
    env("FINDUS_VORGANG_LONG_SUMMARY_MAX_DOCUMENT_LONG_SUMMARY_CHARS", "2000")
)
FINDUS_VORGANG_LONG_SUMMARY_MAX_COMMENTS = int(
    env("FINDUS_VORGANG_LONG_SUMMARY_MAX_COMMENTS", "30")
)
FINDUS_VORGANG_LONG_SUMMARY_MAX_OUTPUT_TOKENS = int(
    env("FINDUS_VORGANG_LONG_SUMMARY_MAX_OUTPUT_TOKENS", "6000")
)
FINDUS_VORGANG_LONG_SUMMARY_TASK_TIMEOUT_SECONDS = int(
    env("FINDUS_VORGANG_LONG_SUMMARY_TASK_TIMEOUT_SECONDS", "600")
)
FINDUS_VORGANG_LONG_SUMMARY_POLL_TIMEOUT_SECONDS = int(
    env("FINDUS_VORGANG_LONG_SUMMARY_POLL_TIMEOUT_SECONDS", "1200")
)

# --------------------------------------------------------------------------
# KI-Entwurf einer Brief-Vorlage (#1097,
# apps.documents.letter_template_ai): ein `generate()`-Call pro Klick auf
# „Mit KI erstellen", synchron im Request -- der Nutzer wartet ohnehin auf
# das Ergebnis, das nirgends gespeichert wird, sondern nur das Formular
# vorbefüllt. MAX_PLACEHOLDERS ist die Obergrenze an Platzhalter-
# Vorschlägen (im Prompt genannt und beim Parsen hart durchgesetzt): eine
# Vorlage mit 40 Bindungen wäre keine Hilfe, sondern Aufräumarbeit.
# MAX_OUTPUT_TOKENS deckt Anleitung (Markdown, der längste Teil) plus die
# Platzhalter-Liste; reicht es nicht, wiederholt `generate_json` den Call
# mit dem doppelten Budget statt abgeschnittenes JSON zu flicken (#1096).
# --------------------------------------------------------------------------
FINDUS_LETTER_TEMPLATE_DRAFT_MAX_PLACEHOLDERS = int(
    env("FINDUS_LETTER_TEMPLATE_DRAFT_MAX_PLACEHOLDERS", "12")
)
FINDUS_LETTER_TEMPLATE_DRAFT_MAX_OUTPUT_TOKENS = int(
    env("FINDUS_LETTER_TEMPLATE_DRAFT_MAX_OUTPUT_TOKENS", "3000")
)

# --------------------------------------------------------------------------
# KI-Brief aus einer Vorlage (#1095, apps.documents.letter_generation): ein
# `generate()`-Call pro Entwurf, async im Worker. CONTEXT_MAX_CHARS
# begrenzt, wie viel vom beantworteten Dokument mitgeht -- normalerweise
# dessen KI-Zusammenfassung (#1020), ersatzweise ein Ausschnitt des
# extrahierten Texts, wenn es noch keine gibt. MAX_OUTPUT_TOKENS deckt
# Betreff plus Brieftext; ein einseitiger Geschäftsbrief liegt deutlich
# darunter, aber ein abgeschnittener Brief wäre schlimmer als ein paar
# ungenutzte Tokens -- reicht es doch nicht, wiederholt `generate_json`
# den Call mit dem doppelten Budget, statt abgeschnittenes JSON zu flicken
# (#1096).
#
# Word (python-docx) und PDF (fpdf2) werden beide direkt gerendert, es gibt
# also KEINE Konverter-Systemabhängigkeit (kein LibreOffice headless) --
# siehe apps.documents.letter_render.
# --------------------------------------------------------------------------
FINDUS_LETTER_CONTEXT_MAX_CHARS = int(env("FINDUS_LETTER_CONTEXT_MAX_CHARS", "4000"))
FINDUS_LETTER_MAX_OUTPUT_TOKENS = int(env("FINDUS_LETTER_MAX_OUTPUT_TOKENS", "3000"))

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

# --------------------------------------------------------------------------
# Thumbnails (apps.documents.thumbnails, #1123): beim Ingest gerendertes
# Vorschaubild der ersten Seite (PDF) bzw. skaliertes Bild (image/*) fuer die
# Kachelansicht. Bewusst klein -- ein Kachelbild, keine Leseansicht --, daher
# die kurze laengste Kante. `document_thumbnail` liefert es auth-gestuetzt
# aus und cacht es MAX_AGE Sekunden, da es pro Dokument unveraenderlich ist.
# --------------------------------------------------------------------------
FINDUS_THUMBNAIL_MAX_EDGE = int(env("FINDUS_THUMBNAIL_MAX_EDGE", "400"))
FINDUS_THUMBNAIL_CACHE_MAX_AGE = int(env("FINDUS_THUMBNAIL_CACHE_MAX_AGE", str(60 * 60 * 24 * 30)))

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
        # Default-Output-Budget für Calls, die keins mitgeben. 1.024 war zu
        # knapp für jede strukturierte Antwort (#1096) -- Anthropic verlangt
        # das Feld zwingend, es ist eine Obergrenze und keine Reservierung,
        # also kostet ein großzügiger Default nichts.
        "max_tokens": int(env("FINDUS_ANTHROPIC_MAX_TOKENS", "4096")),
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

# Absolute Basis-URL fuer Links in Benachrichtigungsmails (#1125) -- ein
# Hintergrundjob hat keinen `request`, aus dem sich `build_absolute_uri`
# speisen liesse, deshalb ein eigenes, explizit konfiguriertes Setting statt
# eines aus ALLOWED_HOSTS geratenen Schemas/Hosts.
FINDUS_BASE_URL = env("FINDUS_BASE_URL", "http://localhost:8000")

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
