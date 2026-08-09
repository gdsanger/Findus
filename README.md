# Findus

Gedächtnis-/Retrieval-Schicht für Dokumente: Django + HTMX, PostgreSQL/pgvector,
Redis, Background-Worker und ein eigener MCP-Service (SSE). Siehe
[`Architektur.md`](Architektur.md) für die Produkt- und Architekturentscheidungen.

Dieses Repo enthält aktuell nur das lauffähige Grundgerüst (Step 1 des
Build-Plans) — noch keine Fachlogik/Models für Dokumente, Absender, Tags etc.

## Stack

- **Django** (Python), UI server-rendered mit **HTMX** + **Bootstrap**, beide
  als vendored Static-Assets ausgeliefert (`static/vendor/`) — kein
  npm/Webpack/Vite. Dasselbe gilt für **Cytoscape.js**, das den Fokus-Graphen
  unter `/graph` zeichnet.
- **PostgreSQL + pgvector** als einziger Datenspeicher (`db`-Service nutzt das
  offizielle `pgvector/pgvector`-Image).
- **Redis** als App-Cache und Broker für **Django-Q2** (Background-Worker,
  bewusst nicht Celery).
- **MCP-Service** als eigener Prozess/Entrypoint, über SSE erreichbar, teilt
  sich Django-Models/-Services (kein paralleler DB-Layer).
- **MinIO** (S3-kompatibel) für Original-Dateien, lokal via Compose.

## Setup

```bash
cp .env.example .env
docker compose up --build
```

Das startet `web` (Django, http://localhost:8000), `db`, `redis`, `worker`
(Django-Q2), `mcp` (SSE, http://localhost:8001/sse) und `minio`
(Konsole: http://localhost:9001). Migrationen laufen beim Start des
`web`-Containers automatisch (inkl. `CREATE EXTENSION vector`).

Admin-User anlegen:

```bash
docker compose exec web python manage.py createsuperuser
```

Danach:
- Django-Admin: http://localhost:8000/admin/
- App (Login erforderlich): http://localhost:8000/

## HTMX-Beispiel

Die Startseite (`/`) zeigt zwei verdrahtete HTMX-Muster:

1. Ein Suchfeld mit `hx-get`, das eine gefilterte, server-gerenderte Tabelle
   in `#example-list` swapt (Muster für spätere Such-/Listen-Interaktionen).
2. Ein Button mit `hx-post`, der einen Django-Q2-Task über Redis einreiht;
   das Ergebnis erscheint im Log des `worker`-Containers:

   ```bash
   docker compose logs -f worker
   ```

CSRF für HTMX ist über ein kleines Snippet in `templates/base.html` gelöst
(`htmx:configRequest` hängt den Django-CSRF-Token an jeden Request).

## MCP-Service prüfen

Der MCP-Service beantwortet `ping`/`health` über SSE (`/sse`), z. B. mit dem
offiziellen MCP-Python-Client:

```python
import asyncio
from mcp import ClientSession
from mcp.client.sse import sse_client

async def main():
    async with sse_client("http://localhost:8001/sse") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print(await session.call_tool("ping", {}))
            print(await session.call_tool("health", {}))

asyncio.run(main())
```

`health` prüft zusätzlich die geteilte PostgreSQL-Verbindung.

## Projektstruktur

```
config/            Django-Projekt (Settings base/dev/prod, URLs, WSGI/ASGI)
apps/accounts/      Custom User-Model (AUTH_USER_MODEL) + Department
apps/documents/      Kern-App; aktuell nur pgvector-Extension-Migration + HTMX-Demo
apps/ai/             Platzhalter für die KI-Provider-Schicht (Folge-Issue)
apps/mcp/            MCP-SSE-Entrypoint + Tools (ping/health)
templates/, static/  Server-rendered UI, vendored HTMX/Bootstrap/Cytoscape
docker-compose.yml   web, db, redis, worker, mcp, minio
```

## Template-Konventionen

Djangos `{# … #}`-Syntax funktioniert nur **einzeilig** — enthält der
Kommentar einen Zeilenumbruch, wird er nicht entfernt, sondern als
Klartext gerendert. Deshalb gilt:

- Mehrzeilige Erläuterungen im Template ausschließlich mit
  `{% comment %} … {% endcomment %}`.
- `{# … #}` nur für einzeilige Kommentare verwenden.

Ein Regressionstest (`apps/documents/tests_template_comment_lint.py`)
durchsucht alle `templates/`-Verzeichnisse nach mehrzeiligen `{# … #}`-
Blöcken und schlägt fehl, falls einer auftaucht.

## Lokale Entwicklung ohne Docker

Voraussetzung: lokale PostgreSQL-Instanz **mit installierter pgvector-
Extension** und ein Redis-Server.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # POSTGRES_HOST/REDIS_URL auf localhost anpassen
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Worker und MCP-Service separat starten:

```bash
python manage.py qcluster
python -m apps.mcp.server
```
