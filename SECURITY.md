# Security-Baseline

**Stand:** 2026-08-04 · Etabliert in #1052, Anlass war ein MCP-Dienst ganz ohne Auth.

Findus ist ein privates Prototyp-Projekt, nicht ISO-zertifiziert — diese Baseline ist
bewusst **pragmatisch**, aber **vollständig**: jeder Punkt ist entweder durch Code oder
durch einen Test belegt, keine reine Absichtserklärung. Künftige security-relevante
Issues bekommen einen kurzen „Security-Check"-Akzeptanzpunkt mit Verweis hierher.

## 1. Autorisierung/Sichtbarkeit

Jede Datenabfrage, die Inhalte an einen Nutzer liefert (Web **und** MCP), läuft durch
`visible_to(user)` (`DocumentQuerySet`/`TaskQuerySet`/`TaskTemplateQuerySet` in
`apps/documents/models.py`). Kein ungescoptes `.all()`/`.filter()`, das über die
Sichtbarkeit (Abteilung/privat) hinaus ausliefert.

Das gilt auch für IDs, die aus einem POST-Body kommen und ein M2M setzen (z. B. welche
Dokumente an eine Aufgabe verknüpft werden) — nicht nur für die Query, die den
Haupt-Datensatz lädt. Ein Formular, das nur sichtbare Optionen rendert, verhindert
nichts auf Serverseite; siehe `apps/documents/task_views.py::_set_task_documents` als
Referenzimplementierung (schneidet die POST-IDs mit `visible_to(user)`, statt sie
ungeprüft zu übernehmen).

`Vorgang`, `Correspondent` und `Tag` haben bewusst **keine** Sichtbarkeit (globale
Stammdaten, siehe `Architektur.md`) — Zuordnungen zu diesen Modellen brauchen daher
keine `visible_to`-Prüfung.

## 2. Originaldateien

Nur über die auth-gestützte, `visible_to`-geprüfte Streaming-View
(`document_original_download`/`document_original_preview` in
`apps/documents/views.py`), niemals über ein öffentliches `MEDIA_URL`. Der Storage-Layer
(S3/MinIO) hat eine eigene URL, die jede ACL umgeht — diese darf nie direkt verlinkt
werden.

## 3. MCP

- **Authentifizierung — Token im Query-String:** Der SSE-Endpoint verlangt einen Token
  im Query-String (`…/sse?token=<MCP_TOKEN>`), weil Claude Desktop als MCP-Client bei
  SSE keine eigenen Header senden kann. Ein `Authorization: Bearer`-Header wird
  zusätzlich akzeptiert, ist aber nicht der verbindliche Weg. Fehlender/falscher Token
  → `401`, bevor der Stream aufgeht. Umgesetzt als eigenständige ASGI-Middleware
  (`apps/mcp/auth.py::TokenAuthMiddleware`) vor `mcp_app.sse_app()` — FastMCPs
  eingebaute Auth ist header-/OAuth-orientiert und deckt Query-String-Tokens nicht ab.
  Der Vergleich läuft constant-time (`hmac.compare_digest`); ein leerer/fehlender
  `MCP_TOKEN` lässt **keine** Anfrage durch (fail closed).
- **Token-Hygiene:** `apps/mcp/server.py` startet uvicorn mit `access_log=False` —
  sonst landet der Token aus jeder Request-Zeile im Klartext im Access-Log. TLS
  (Reverse-Proxy) ist Aufgabe des Deployments, nicht dieses Repos.
- **Autorisierung:** MCP arbeitet unter einer festen User-Identität
  (`apps/mcp/auth.py::get_mcp_user`, konfiguriert über `MCP_USER_USERNAME`). Jedes
  künftige daten-liefernde Tool **muss** seine Query durch
  `.visible_to(get_mcp_user())` schicken, exakt wie eine Web-View — kein Tool liefert
  ungescopte Daten.
- **Netz:** Der Port ist in `docker-compose.yml` nur an das Loopback-Interface des
  Docker-**Hosts** gebunden (`127.0.0.1:8001:8001`), nicht an alle Interfaces. `ufw` auf
  dem Host ist eine zusätzliche Ebene, nicht die einzige. `MCP_HOST` selbst bleibt
  `0.0.0.0` — das ist die Bindeadresse **innerhalb** des Containers, und Dockers
  Portveröffentlichung kann einen Prozess, der nur auf Container-Loopback lauscht,
  gar nicht erreichen (siehe Kommentar in `config/settings/base.py`).

## 4. Mutationen

Alle create/edit/delete-Endpunkte sind CSRF-geschützt (Djangos globale
`CsrfViewMiddleware`, kein `@csrf_exempt` im gesamten `apps/`-Baum) und
permission-geprüft (`visible_to`/Owner). Ein Objekt außerhalb der Sichtbarkeit eines
Nutzers liefert **404**, nicht 403 — das bestätigt keine Existenz und ist die im
gesamten Code etablierte Konvention (`get_object_or_404(Model.objects.visible_to(user), pk=pk)`).
Eine fehlende/ungültige CSRF-Absicherung liefert **403** (Djangos Standardverhalten).
Löschen ist an dieselbe Sichtbarkeitsgrenze gebunden wie Lesen — bei
Abteilungs-Sichtbarkeit kann jedes Abteilungsmitglied löschen, nicht nur der
ursprüngliche Owner; das ist Produktentscheidung (siehe Sichtbarkeitsmodell in
`Architektur.md`), keine Lücke.

## 5. Secrets

API-Keys/Token nie loggen, ausschließlich aus `.env` (nicht versioniert, siehe
`.gitignore`). `.env.example` dokumentiert nur Platzhalter, nie echte Werte.

## 6. Header/Prod

`X-Frame-Options: DENY` global (Djangos `XFrameOptionsMiddleware`), `SAMEORIGIN` nur an
den beiden Original-Preview-Endpoints, die eigens dafür `@xframe_options_sameorigin`
tragen (#1042). In `config/settings/prod.py`: `DEBUG=False`, `ALLOWED_HOSTS` über
`DJANGO_ALLOWED_HOSTS` gesetzt, `SECURE_SSL_REDIRECT`/`SESSION_COOKIE_SECURE`/
`CSRF_COOKIE_SECURE`/HSTS aktiv. HTTPS-Terminierung selbst ist Aufgabe des
Reverse-Proxys vor dem Container, nicht dieses Repos.

## Regressionstests

- `apps/mcp/test_auth.py` — SSE-Endpoint ohne Token → 401, mit gültigem
  Query-String-Token → durchgelassen, mit `Authorization: Bearer` → durchgelassen,
  leerer `MCP_TOKEN` → alles abgelehnt.
- `apps/documents/test_task_views.py::test_invisible_document_is_not_linked` /
  `test_edit_does_not_link_invisible_document` — ein Fremd-Dokument wird nicht über
  einen manipulierten POST an die eigene Aufgabe verknüpft.
- `apps/documents/test_views.py::DocumentDeleteViewTests` — Löschen ohne Sichtbarkeit
  → 404 (`test_delete_is_scoped_by_visibility`), Löschen ohne CSRF-Token → 403
  (`test_delete_without_csrf_token_is_rejected`).
- Cross-User-Sichtbarkeit (Liste/Detail/Such-API/Original-Stream) ist bereits breit
  über `test_views.py` abgedeckt (`test_documents_outside_visibility_are_not_listed`,
  `test_document_outside_visibility_returns_404`,
  `test_original_download_is_scoped_by_visibility`,
  `test_original_preview_is_scoped_by_visibility`, u. a.).
