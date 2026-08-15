# CLAUDE.md

Findus wird aus dem Live-Betrieb heraus weiterentwickelt: kurze Zyklen, Tickets
werden zügig umgesetzt. Das Risiko dabei ist nicht die UI — die ist billig zu
korrigieren. Das Risiko ist, dass **Model, Core-Services und UI auseinander-
laufen**, weil eine Architekturentscheidung nur in einer geschlossenen PR-
Beschreibung steht und der nächste Implementierer sie nie liest. Diese Datei
ist der Ort, an dem diese Entscheidungen stattdessen stehen — dort, wo als
Nächstes ohnehin hingeschaut wird.

Die Langform (Vision, Geschäftsmodell, Datenhaltung, DSGVO, Tech-Stack-
Begründungen) steht in [`Architektur.md`](Architektur.md). Diese Datei
wiederholt sie nicht, sondern nennt nur die Invarianten, die kein Ticket neu
verhandeln soll. Vertragstests, die diese Regeln absichern, liegen in
`apps/documents/test_contracts.py` — nach der Regel benannt, nicht nach der
Funktion.

## Sichtbarkeit & Auslieferung

- Jeder dokumentbezogene Endpunkt läuft über `Document.objects.visible_to(user)`
  (siehe `views._visible_document`). Ein fremdes Dokument liefert **404, nicht
  403** — der Endpunkt darf nie verraten, dass eine PK überhaupt existiert.
- Originale und Thumbnails werden **nie** über eine Storage-/MEDIA-URL
  verlinkt, sondern immer über den auth-gestützten Stream
  (`document_original_download`/`_preview`/`document_thumbnail`,
  `X-Content-Type-Options: nosniff`). Die Storage-Backend-URL ist ein
  öffentlicher S3-/MinIO-Link, der die ACL vollständig umgeht.
- Binärdaten liegen im Object Storage, nicht in Postgres („Gehirn ↔ Regal",
  siehe Architektur.md) — Postgres bläht sonst Backups auf und quält Vacuum.

## Zustand & Fachlogik

- `Document.action_status` ist die **einzige** Zustandsquelle für „hier ist
  noch etwas zu tun". Kein zweiter Erledigt-Status, insbesondere nicht je
  Kommentar — ein `DocumentComment` hat bewusst kein eigenes Erledigt-Feld.
- Eine Wiedervorlage ist ein **One-Shot-Ping**: `reminded_at` wird erst nach
  erfolgreichem Versand gesetzt, danach nie wieder erinnert (kein
  Doppelversand bei einem zweiten Lauf desselben oder eines späteren Tages).
- Datums-/Fälligkeitslogik lebt an **einer** Stelle — den Queryset-Methoden
  auf `DocumentQuerySet`/`DocumentCommentQuerySet` (`open_visible_to`, `due`,
  `overdue`, `due_today`, `due_this_week`, …) — nicht parallel in View,
  Template und Job. Immer `timezone.localdate()`, nie UTC, sonst laufen
  Fälligkeiten je nach Serverzeitzone auseinander.

## Pipelines & Services

- Ingest ausschließlich über `apps.ingest.service.ingest_file` — Dedup (global
  per `sha256`), Storage, Sichtbarkeit und Enqueue sind ein Vertrag, kein
  Connector baut das einzeln nach.
- Retrieval/Ähnlichkeit ausschließlich über `apps.documents.retrieval.
  DocumentRetrievalService` — der einzige Ort, der `Chunk` direkt abfragen
  darf. Jeder Einstiegspunkt (strukturierte Filterung, Vektorsuche,
  `similar_documents`) startet bei `Document.objects.visible_to(user)`, bevor
  irgendetwas anderes die Daten anfasst. RAG und MCP-Tools setzen auf diesem
  Service auf, nicht auf den Models selbst.
- Mailversand ausschließlich über `apps.mail.service` + `get_mail_backend()`
  — kein direkter SMTP-/Graph-Aufruf an anderer Stelle.
- **Kommentare werden nicht embedded — aber sehr wohl als Prompt-Kontext
  verwendet.** Das ist die fehleranfälligste Regel im Projekt, weil sie zwei
  Hälften hat und beide nötig sind:
  - *Nicht indexieren:* Kommentare sind Notizen **über** ein Dokument, nicht
    dessen Inhalt. In den Chunks würden sie die semantische Suche über den
    Dokumentbestand verwässern (#1125). `chunk_text`/`process_document`
    laufen ausschließlich über `Document.text_content`.
  - *Aber als Kontext mitgeben:* Für KI-Analysen und Handlungsempfehlungen
    sind sie unverzichtbar — sie tragen den Handlungszustand, den das System
    sonst nirgends kennt (#1132). Deshalb ein eigener MCP-Tool-Endpunkt
    (`document_comments`) statt eines Umwegs über die Chunk-Suche. Wo
    Kommentare in einen Prompt einfließen, gehören sie in einen eigenen,
    benannten Abschnitt, nie vermischt mit Dokumentinhalten; bei Widerspruch
    gewinnt der Kommentar, weil er der aktuellere Stand ist.
  - Wer nur die halbe Regel kennt, „korrigiert" sie irgendwann in die falsche
    Richtung — entweder landen Kommentare doch in den Chunks, oder sie gelten
    als generell tabu und der Kontext geht verloren.
- KI-Analyse (`analysis.analyze_document`) und Thumbnail-Rendering
  (`thumbnails.generate_thumbnail_for_document`) sind **fehlertolerant**: Ein
  Fehlschlag loggt und läuft weiter (Fehler landet in `metadata["analysis_
  error"]` bzw. gar nicht erst sichtbar), er setzt `processing_status` nicht
  auf `failed`. Nur Extraktion/Embedding dürfen die Pipeline auf `failed`
  stellen.
- Embeddings tragen Modell **und** Version (`Chunk.embedding_model` /
  `_version`); ein Modellwechsel bedeutet Re-Index über den gesamten Bestand
  — das ist der unterstützte Migrationsweg, kein Sonderfall.

## UI-Konventionen

- HTMX-Swap-Verträge sind bindend: `#document-list-region` für die
  Dokumentliste, `#comment-<pk>` für den Einzel-Swap je Kommentar. Partials
  bleiben klein und einzeln austauschbar.
- **Ein Swap-Partial rahmt sich nicht selbst ein.** Card-/Section-Rahmen
  gehören in die einbindende Seite, *um* die Swap-Region herum, nie in das
  Partial, das in sie hineingetauscht wird. Zwei Gründe, beide schon
  passiert: der Pending-Poller in `_document_list.html` tauscht sich per
  `hx-swap="outerHTML"` gegen genau diese Antwort aus — ein Rahmen im
  Partial läge nach jedem 4-Sekunden-Lauf eine Ebene tiefer in der vorigen
  Card; und ein Rahmen innerhalb eines `{% if %}` geht im leeren Zweig gar
  nicht erst auf, dessen `</div>` aber trotzdem zu. Am Rahmen der Seite
  hängen zudem automatisch alle Partials, die in dieselbe Region kommen
  können (`_document_list`, `_document_followups`, `_search_results`).
- **Die Dokumentliste hat genau einen Ansicht-Default: Kachel.** Er gilt auf
  Home *und* auf allen Hub-Seiten (`view=grid`); "" ist die Tabelle,
  `timeline` die Zeitleiste, `wiedervorlagen` die Terminliste. Ein
  abweichender Default in einer der vier Views lässt die Ansicht beim
  Wechsel Home ↔ Hub springen, obwohl der Umschalter unverändert dasteht.
- Die gewählte Ansicht ist **kein Filter, sondern eine Einstellung**: sie
  liegt seitenübergreifend in `localStorage` unter `findus:documents:view`
  (gepflegt in `_filter_bar.html`, damit die Hub-Seiten nichts nachbauen),
  *nicht* im Filter-State von Home (`findus:documents:filters`). Ein `view`
  in der URL gewinnt immer — geteilte Links, Zurück-Navigation und
  Pagination müssen zeigen, was sie sagen; localStorage ist nur der
  Einstieg ohne Parameter. „Filter zurücksetzen" setzt die Ansicht deshalb
  auch nicht mit zurück. Nur Home kann die Wiedervorlagen-Ansicht rendern
  (sie listet `DocumentComment`), deshalb erscheint ihr Umschalter nur dort
  (`show_followup_view`) und ein dort gespeicherter Wert wird auf einem Hub
  ignoriert statt still auf die Tabelle zurückzufallen.
- Hub-Seiten (Vorgang, Kontakt, Tag) teilen ein Layout: Aktionszeile oben,
  darunter `col-lg-3` Stammdaten (dauerhaft offenes Formular, Chips und
  Kennzahlen im `card-footer`) neben `col-lg-9` Dokumentbereich. Immer
  `col-lg-*`, nie nacktes `col-*` — sonst bleibt die Stammdatenspalte auch
  auf dem Telefon ein Viertel breit und ihre Felder unbedienbar.
- Wer ein Collapse/Tab auflöst, entfernt auch seinen Auslöser: ein Button
  mit `data-bs-target` auf eine nicht mehr existierende ID ist wieder ein
  „toter Button" — er tut sichtbar nichts und meldet nichts.
- Formulare je Bereich isolieren — ein leeres Pflichtfeld in einem inaktiven
  Tab/Formular löst sonst still `htmx:validation:halted` aus und der Button
  im aktiven Tab tut scheinbar nichts (#1103, „toter Button").
- Eingaben immer über ein `ModelForm`/`Form` validieren, nie roh aus
  `request.POST` ins Model — ein unparsbarer Wert soll als Inline-Fehler
  landen, nicht als 500 (#1045).
- Determinismus vor Bequemlichkeit: Farb-/Zuordnungs-Hashes über `hashlib.md5`,
  nicht über Pythons `hash()` (der ist pro Prozess zufällig gesalzen und
  würde bei jedem Neustart andere Farben zuweisen).

## Bewusst nicht gebaut

Damit es niemand „nachrüstet":

- Findus ist Informationsspeicher, Verknüpfer und Finder — **kein DMS**. Kein
  Aktenplan, keine Versionierung, keine Freigabe-Workflows, keine
  Aufbewahrungsfristen, kein Peer-to-Peer-Teilen.
- Aufgaben (`Task`) sind auf dem Rückzug; neue Funktionalität hängt an
  Dokument-Kommentaren, nicht an Aufgaben. **Kein „Verknüpfte
  Aufgaben"-Block mehr** — weder am Vorgang-Hub noch an der
  Dokument-Detailseite, und damit auch keine Aufgaben-Schnellanlage von
  dort. Aufgaben leben nur noch auf ihren eigenen Seiten (`/tasks/…`); was
  aus einer Handlungsempfehlung übernommen wurde, verlinkt das
  Empfehlungs-Panel selbst („Aufgabe öffnen").

## Pflegeregel

Wer eine Entscheidung dieser Klasse trifft oder ändert, aktualisiert diese
Datei **im selben PR**. Sonst wandert die Erkenntnis wieder nur in eine
PR-Beschreibung und ist beim nächsten Ticket unsichtbar — genau das Problem,
das diese Datei lösen soll. Wer einen der zugehörigen Vertragstests in
`apps/documents/test_contracts.py` bewusst ändert, ändert diese Datei mit.
