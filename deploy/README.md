# Deploy

Verbindlicher Ablauf für jedes Deployment (Docker Compose oder ein
äquivalentes Setup mit separaten Prozessen für `web`, `worker`, `mcp`):

```bash
git pull
python manage.py migrate
docker compose restart web worker mcp
```

**`migrate` und der Restart sind kein optionaler Schritt.** Ohne `migrate`
laufen Code (Models) und DB-Schema auseinander; ohne Restart bleibt der
alte Code (inkl. alter Migrations-State im laufenden Prozess) aktiv, selbst
wenn `migrate` bereits gelaufen ist.

## Warum dieser Schritt Pflicht ist (#1055)

Ein Merge zweier gegenläufiger `0015`-Migrationen (eine fügte
`Correspondent.address` hinzu, eine andere droppte dieselbe Spalte als
vermeintlich "orphaned") hat die Spalte aus der DB entfernt, während Modell
und Migrations-State sie weiterhin erwarteten. Ergebnis: `GET /` → 500
(`column documents_correspondent.address does not exist`), die App war
komplett down. Ein fehlender/verzögerter `migrate`-Schritt hätte densel­ben
Fehler auch durch reines Auseinanderlaufen von Code und Schema auslösen
können, unabhängig vom Migrations-Konflikt selbst.

## Konflikt-/Doppelnummern-Check

Vor jedem Merge nach `main` (lokal oder in CI) muss laufen:

```bash
python manage.py makemigrations --check --dry-run
```

Das schlägt u. a. genau dann fehl, wenn zwei Branches unabhängig
voneinander dieselbe Migrationsnummer für dieselbe App vergeben haben
(`Conflicting migrations detected; multiple leaf nodes in the migration
graph`) — der Fehler, der zu diesem Incident geführt hat. Der Check läuft
ohne echte DB-Verbindung (ein Verbindungsfehler wird nur als Warnung
geloggt) und ist Teil der CI-Pipeline (`.github/workflows/ci.yml`).

Wird ein solcher Konflikt gemeldet, **nicht** einfach `--merge` laufen
lassen und committen: erst prüfen, ob eine der beiden Migrationen die
andere widerspricht (z. B. add vs. drop derselben Spalte). Eine reine
Merge-Migration löst nur den Graph-Konflikt, verhindert aber nicht, dass
die widersprüchliche Migration in falscher Reihenfolge nach der anderen
läuft.
