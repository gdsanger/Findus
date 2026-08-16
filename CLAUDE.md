# CLAUDE.md

Findus wird aus dem Live-Betrieb heraus weiterentwickelt: kurze Zyklen, Tickets
werden zügig umgesetzt. Das Risiko dabei ist nicht die UI — die ist billig zu
korrigieren. Das Risiko ist, dass **Model, Core-Services und UI auseinander-
laufen**, weil eine Architekturentscheidung nur in einer geschlossenen PR-
Beschreibung steht und der nächste Implementierer sie nie liest.

Diese Datei nennt die **Invarianten**, die kein Ticket neu verhandeln soll —
und nur die. Warum eine Regel gilt, steht dabei; wie sie umgesetzt ist, steht
im Code (Docstrings) und nicht hier. Die Langform zu Vision, Geschäftsmodell,
Datenhaltung und Tech-Stack steht in [`Architektur.md`](Architektur.md).

Vertragstests, die diese Regeln absichern, liegen in
`apps/documents/test_contracts.py` — nach der Regel benannt, nicht nach der
Funktion.

## Sichtbarkeit & Auslieferung

- Jeder dokumentbezogene Endpunkt läuft über `Document.objects.visible_to(user)`
  (siehe `views._visible_document`). Ein fremdes Dokument liefert **404, nicht
  403** — der Endpunkt darf nie verraten, dass eine PK überhaupt existiert.
- Originale und Thumbnails werden **nie** über eine Storage-/MEDIA-URL
  verlinkt, sondern immer über den auth-gestützten Stream (mit
  `X-Content-Type-Options: nosniff`). Die Storage-Backend-URL ist ein
  öffentlicher S3-/MinIO-Link, der die ACL vollständig umgeht.
- Binärdaten liegen im Object Storage, nicht in Postgres („Gehirn ↔ Regal").

## Zustand & Fachlogik

- `Document.action_status` ist die **einzige** Zustandsquelle für „hier ist
  noch etwas zu tun". Kein zweiter Erledigt-Status, insbesondere nicht je
  Kommentar — ein `DocumentComment` hat bewusst kein eigenes Erledigt-Feld.
- Eine Wiedervorlage ist ein **One-Shot-Ping**: `reminded_at` wird erst nach
  erfolgreichem Versand gesetzt, danach nie wieder erinnert.
- Datums- und Fälligkeitslogik lebt an **einer** Stelle — den Queryset-Methoden
  auf `DocumentQuerySet`/`DocumentCommentQuerySet` —, nicht parallel in View,
  Template und Job. Immer `timezone.localdate()`, nie UTC, sonst laufen
  Fälligkeiten je nach Serverzeitzone auseinander.

> **Offen, bewusst noch nicht entschieden:** `action_status` kennt neben
> „offen" und „erledigt" den dritten Wert „kein Handlungsbedarf". Ob dieser für
> Wiedervorlagen-Liste, Dashboard und Filter wie „erledigt" oder wie „offen"
> wirkt, ist nirgends festgelegt. Wer es zuerst braucht, entscheidet es —
> und trägt die Entscheidung hier ein.

## Pipelines & Services

- Ingest ausschließlich über `apps.ingest.service.ingest_file` — Dedup (global
  per `sha256`), Storage, Sichtbarkeit und Enqueue sind **ein** Vertrag; kein
  Connector baut das einzeln nach.
- Retrieval und Ähnlichkeit ausschließlich über `DocumentRetrievalService` —
  der einzige Ort, der `Chunk` direkt abfragen darf. Jeder Einstiegspunkt
  startet bei `Document.objects.visible_to(user)`, bevor irgendetwas anderes
  die Daten anfasst. RAG und MCP-Tools setzen auf diesem Service auf, nicht auf
  den Models.
- Mailversand ausschließlich über `apps.mail.service` + `get_mail_backend()` —
  kein direkter SMTP-/Graph-Aufruf an anderer Stelle.
- KI-Analyse und Thumbnail-Rendering sind **fehlertolerant**: Ein Fehlschlag
  wird protokolliert und die Verarbeitung läuft weiter; er setzt
  `processing_status` **nicht** auf `failed`. Nur Extraktion und Embedding
  dürfen die Pipeline auf `failed` stellen.
- Embeddings tragen Modell **und** Version; ein Modellwechsel bedeutet
  Re-Index über den gesamten Bestand — das ist der unterstützte
  Migrationsweg, kein Sonderfall.

## KI-Funktionen

### Kommentare: nicht indexieren, aber als Kontext verwenden

Die fehleranfälligste Regel im Projekt, weil sie zwei Hälften hat und beide
nötig sind:

- **Nicht indexieren.** Kommentare sind Notizen *über* ein Dokument, nicht
  dessen Inhalt. In den Chunks würden sie die semantische Suche über den
  Dokumentbestand verwässern. Chunking läuft ausschließlich über
  `Document.text_content`.
- **Aber als Kontext mitgeben.** Für KI-Analysen und Handlungsempfehlungen sind
  sie unverzichtbar — sie tragen den Handlungszustand, den das System sonst
  nirgends kennt. Wo Kommentare in einen Prompt einfließen, gehören sie in
  einen eigenen, benannten Abschnitt, nie vermischt mit Dokumentinhalten; bei
  Widerspruch gewinnt der Kommentar, weil er der aktuellere Stand ist.

Wer nur die halbe Regel kennt, „korrigiert" sie irgendwann in die falsche
Richtung — entweder landen Kommentare doch in den Chunks, oder sie gelten als
generell tabu und der Kontext geht verloren.

### Dokumentdatum: die Rangfolge steht im Code, nicht im Prompt

Anlass waren 40 Kontoauszüge, die alle das „Erstellt am" aus dem Fuß (= den
Upload-Tag) als Dokumentdatum trugen und damit die Timeline entwerteten.

- Die Analyse fragt eine **Liste typisierter Datumsangaben** ab (`dates`:
  `belegdatum`/`zeitraum_beginn`/`zeitraum_ende`/`briefkopf`/`erstellt`);
  welche davon `Document.document_date` wird, entscheidet
  `apps.documents.document_dates` nach fester Rangfolge — Belegdatum vor
  Zeitraum-Ende vor Briefkopf vor Erstell-/Druckdatum. Wer die Auswahl zurück
  in den Prompt verlagert, macht sie von der Tagesform des Modells abhängig.
- `zeitraum_beginn` steht **nicht** in der Rangfolge: ein Zeitraum datiert auf
  sein Ende. Beide Grenzen landen trotzdem als Key-Facts
  (`period_start`/`period_end`) — abgeleitet aus derselben Liste, nicht als
  zweites Antwortfeld, sonst gäbe es zwei Wege zum selben Wert.
- Die **Plausibilitätsprüfung** gegen den Upload-Tag gehört ebenfalls in den
  Code: sie braucht das Upload-Datum, das dem Modell gar nicht vorliegt. Sie
  biegt bewusst auch den seltenen echten Fall um („heute datiert *und* mit
  Zeitraum") — bei einem Archiv ist das die Ausnahme.
- `metadata["document_date_source"]` ist Herkunftsvermerk **und**
  Schreibschutz: die Analyse überschreibt nur, was sie selbst gesetzt hat.
  `"manuell"` und ein Datum ganz ohne Vermerk (EML-`Date`-Header, Bestand)
  bleiben unangetastet — ein Wartungslauf darf keine Handkorrektur
  einkassieren. Deshalb stempelt die Migration `0035` nur dort, wo
  `document_date` dem von der KI abgelegten `key_facts["document_date"]`
  exakt entspricht.

### Ausführliche Zusammenfassung

- Reiner **Fließtext** in Absätzen. Ratschläge stehen als Sätze darin, nie als
  Liste oder Datensatz: Strukturierte „nächste Schritte" mit Status, Quellen
  und Frist gibt es bereits je Vorgang. Dieses Feature dupliziert sie nicht.
- **Nur auf Knopfdruck**, nie beim Ingest — die meisten Dokumente werden nie
  wieder geöffnet; sie alle vorab ausführlich zusammenzufassen kostet Tokens
  für Text, den niemand liest.
- Gespeichert wird **mit Herkunft** (Zeitpunkt, Modell, Modellversion) und mit
  einer eingefrorenen Basis für den Veraltet-Hinweis. Automatisch neu erzeugt
  wird nie — das würde unbemerkt Kosten verursachen.
- Ein **eingebauter Chat** („Fragen zum Dokument stellen") ist bewusst
  verworfen: Diese Rolle übernimmt der MCP-Endpunkt mit dem KI-Client des
  Nutzers. Kein zweiter Dialogkanal innerhalb der App.

### Schreiben sind immer eine Antwort auf ein Dokument

Es gibt keinen zweiten Modus — der Einstieg aus dem Vorgang führt über die
Auswahl „Auf welches Dokument antworten?". Der Grund ist Eindeutigkeit: Ein
Vorgang bündelt mehrere Dokumente und Kontakte; ohne Anker bleiben genau die
Werte aus Kontakt und Dokument (Empfänger, Betreff, Key-Facts) leer, während
Vorgang und eigene Identität sich auflösen.

- **Kein Rückgriff auf andere Dokumente des Vorgangs.** Fehlt ein Wert, bleibt
  er sichtbar leer und wird nicht aus einem Nachbardokument beschafft — ein
  fremder Betrag in einem Schreiben mit Fristsetzung wäre ein gefährlicher
  Komfort. Wer das „nachrüstet", braucht Rateregeln („welches Dokument
  gewinnt?"), und genau die soll diese Festlegung abschaffen.
  *(Gilt für Schreiben. Die Vorgangs-Zusammenfassung sammelt bewusst über alle
  Dokumente — dort ist Zusammenschau der Zweck.)*
- Die Platzhalter-Auflösung ist **eine** Funktion und liefert je Platzhalter
  Wert **plus Herkunft plus Grund bei Fehlen**. Anzeige, Formularfelder und
  Prompt-Werte speisen sich daraus — nicht aus drei eigenen Schleifen.
- Jeder Platzhalter ohne Wert wird zum Eingabefeld: An einem fehlenden
  Stammdatum soll ein Entwurf nicht scheitern. Ein von Hand eingetragener Wert
  **gewinnt gegen seine Bindung**, sonst überschreibt das nächste Auflösen ihn
  wieder. Adresse und E-Mail lassen sich am Kontakt speichern, der Name
  bewusst nicht — er ist die Identität des Kontakts, das wäre eine Umbenennung.
- Das abgelegte Schreiben ist mit seinem Bezugsdokument verknüpft, trägt
  `direction=ausgang` und liegt in **genau dem** Vorgang, der beim Erzeugen
  gewählt wurde — nicht in allen Vorgängen des Bezugsdokuments. Die Richtung
  der Aussage steckt in den beiden Dokumenten; `DocumentLink` ist und bleibt
  ungerichtet.

## Hintergrundjobs mit LLM-Aufruf

- Solche Jobs brauchen einen **eigenen, großzügigeren Timeout** per
  `async_task(..., timeout=...)`, nie global — der knappe Cluster-Default ist
  bewusst kurz, damit ein hängender Ingest- oder Thumbnail-Job schnell
  auffällt.
- Zwingende Schachtelung: **HTTP-Timeout × Versuche < Task-Timeout <
  `Q_CLUSTER["retry"]`**. `retry` gilt clusterweit und muss größer bleiben als
  jeder verwendete Timeout, sonst stellt Django-Q den Task erneut in die
  Warteschlange, während der erste Versuch noch läuft — doppelte Ausführung,
  doppelte Kosten. Abgesichert durch einen Vertragstest.
- Ein Timeout allein genügt nicht: Ein abgebrochener Job muss seinen Zustand
  auf einen **terminalen Fehlerfall** setzen (vorhandenes Statusfeld
  wiederverwenden, nie ein zweites Zustandskonzept), sonst bleibt die
  Oberfläche auf einem endlosen Ladeindikator stehen.
- Achtung: Django-Q wirft `TimeoutException`, die von `SystemExit` erbt und
  einem `except Exception` entkommt. Wer sie behandeln will, fängt sie extra.

## UI-Konventionen

### HTMX

- Swap-Verträge sind bindend: `#document-list-region` für die Dokumentliste,
  `#comment-<pk>` für den Einzel-Swap je Kommentar. Partials bleiben klein und
  einzeln austauschbar.
- **Ein Swap-Partial rahmt sich nicht selbst ein.** Card- und Section-Rahmen
  gehören in die einbindende Seite, *um* die Swap-Region herum. Zwei Gründe,
  beide schon passiert: Der Pending-Poller tauscht sich per `outerHTML` gegen
  genau diese Antwort aus — ein Rahmen im Partial läge nach jedem Lauf eine
  Ebene tiefer in der vorigen Card; und ein Rahmen innerhalb eines `{% if %}`
  geht im leeren Zweig nicht auf, dessen schließendes Tag aber trotzdem zu.
  **Nicht zu verwechseln:** Das `id`-Element, auf das ein `hx-target` zeigt,
  gehört sehr wohl ins Partial — sonst verliert der Eintrag beim ersten Swap
  seine Kennung.
- **Kontext explizit, nie ambient:** `{% include ... with document=… only %}`.
  Mit `only` scheitert eine künftige Ansicht, die ihre Variable anders nennt,
  laut statt mit einer leeren `id` — genau dieser Fall hat schon einmal einen
  Klick wirkungslos gemacht. Kommt dasselbe Dokument in einer Ansicht mehrfach
  vor (zwei Wiedervorlagen an einem Dokument), reicht `document.pk` als
  Kennung nicht; das Partial nimmt dafür zusätzlich einen Eintrags-Schlüssel
  entgegen, und der Endpunkt reicht ihn unverändert zurück.
- `hx-sync` zeigt auf **sich selbst** (`this:abort`), nie auf einen
  Vorfahren-Selektor. Findet der Selektor kein Ziel, bricht HTMX vor dem
  Request mit einem TypeError ab und der Klick tut sichtbar nichts.
- Ein globaler `htmx:targetError`-Handler in `base.html` macht ein fehlendes
  Swap-Ziel sichtbar. Nicht neu bauen, nicht entfernen.
- Formulare je Bereich isolieren — ein leeres Pflichtfeld in einem inaktiven
  Tab oder Formular löst still `htmx:validation:halted` aus, und der Button im
  aktiven Bereich tut scheinbar nichts („toter Button").

### Ansichten der Dokumentliste

- **Ein Default, überall: Kachel** (`view=grid`) — auf Home wie auf allen
  Hub-Seiten. Weitere Werte: `""` (Tabelle), `timeline`, `wiedervorlagen`.
  Ein abweichender Default in einer der Ansichten lässt die Darstellung beim
  Wechsel Home ↔ Hub springen, obwohl der Umschalter unverändert dasteht.
  *Vorsicht bei `""`: Ein leerer Wert und ein fehlender Parameter sind im Code
  kaum zu unterscheiden. Wer die Auswertung anfasst, prüft beide Fälle
  ausdrücklich.*
- Die Ansicht ist **kein Filter, sondern eine Einstellung**: seitenübergreifend
  in `localStorage` unter `findus:documents:view`, nicht im Filter-State von
  Home. Ein `view` in der URL gewinnt immer — geteilte Links, Zurück-Navigation
  und Pagination müssen zeigen, was sie sagen; `localStorage` ist nur der
  Einstieg ohne Parameter. „Filter zurücksetzen" setzt die Ansicht deshalb
  nicht mit zurück.
- Nur Home kann die Wiedervorlagen-Ansicht rendern (sie listet
  `DocumentComment`). Ihr Umschalter erscheint nur dort; ein gespeicherter Wert
  wird auf einem Hub ignoriert statt still auf die Tabelle zurückzufallen.

### Hub-Seiten und Formulare

- Hub-Seiten (Vorgang, Kontakt, Tag) teilen ein Layout: Aktionszeile oben,
  darunter Stammdaten neben dem Dokumentbereich. Die Spaltenbreiten sind eine
  **vollständige Leiter**, nie ein nacktes `col-*`:
  `col-12 col-md-4 col-lg-3` links, `col-12 col-md-8 col-lg-9` rechts —
  darunter stapeln beide Spalten, sonst wird die Stammdatenspalte am Telefon
  unbedienbar schmal. Abgesichert durch einen Vertragstest.
- Eingaben immer über ein `ModelForm`/`Form` validieren, nie roh aus
  `request.POST` ins Model — ein unparsbarer Wert soll als Inline-Fehler
  landen, nicht als 500.
- **Mehrfachzuordnungen als Token-Eingabe** (Chips plus serverseitige
  Tippsuche statt `<select multiple>`): Das versteckte Feld je Chip trägt
  weiterhin den ursprünglichen Feldnamen — die Token-Eingabe ändert nur die
  Oberfläche, nicht den Speicherpfad. Der Such-Endpunkt schreibt nichts,
  schließt bereits Zugeordnetes aus und sortiert Präfix- vor Teiltreffern. Eine
  Neuanlage gibt **nur den neuen Chip** zurück, nie den ganzen Block — sonst
  verwirft das Rerender unbestätigte Eingaben an anderer Stelle. Kein
  JS-Paket, kein Build-Schritt; Skripte binden delegiert auf `document`-Ebene,
  damit sie jeden HTMX-Swap überstehen.
- **Ein-Klick-Umschalter für `action_status`** in den Übersichten laufen über
  einen eigenen Endpunkt, getrennt von der Select-Kontrolle im Detail — dort
  wird die feinere Ausprägung gepflegt. Der neue Zustand wird **immer** aus dem
  gespeicherten Wert abgeleitet, nie aus einem mitgeschickten Wunschzustand,
  sonst kippt er bei zwei schnellen Klicks unkontrolliert. Ein gemeinsames
  Partial bedient alle Ansichten, damit sie nicht auseinanderlaufen.
- Wer ein Collapse oder Tab auflöst, entfernt auch seinen Auslöser: ein Button,
  dessen Ziel-ID nicht mehr existiert, ist wieder ein toter Button.
- Determinismus vor Bequemlichkeit: Farb- und Zuordnungs-Hashes über
  `hashlib.md5`, nicht über Pythons `hash()` — der ist pro Prozess zufällig
  gesalzen und würde nach jedem Neustart andere Farben zuweisen.

## Bewusst nicht gebaut

Damit es niemand „nachrüstet":

- Findus ist Informationsspeicher, Verknüpfer und Finder — **kein DMS**. Kein
  Aktenplan, keine Versionierung, keine Freigabe-Workflows, keine
  Aufbewahrungsfristen, kein Peer-to-Peer-Teilen.
- Aufgaben (`Task`) sind auf dem Rückzug; neue Funktionalität hängt an
  Dokument-Kommentaren, nicht an Aufgaben. **Kein „Verknüpfte Aufgaben"-Block**
  mehr am Vorgang-Hub oder in der Dokument-Detailseite, und keine
  Aufgaben-Schnellanlage von dort. Aufgaben leben nur noch auf ihren eigenen
  Seiten; was aus einer Handlungsempfehlung übernommen wurde, verlinkt das
  Empfehlungs-Panel selbst.

## Tests

- **Während der Umsetzung**: nur die betroffenen Module ausführen
  (`python manage.py test apps.documents.test_views` o. Ä.), nicht die gesamte
  Suite. Der vollständige Lauf dauert derzeit deutlich über eine halbe Stunde
  und gehört nicht in jede Zwischenprüfung.
- **Einmal am Ende**, bevor die Arbeit als fertig gilt: vollständiger Lauf mit
  `python manage.py test`.
- **Bestehende Fehlschläge**, die nicht von der aktuellen Änderung stammen,
  werden benannt (Testname plus kurze Einordnung), nicht mitkorrigiert — sie
  gehören in ein eigenes Ticket.
- **Übersprungene Tests wegen fehlender Systemwerkzeuge sind normal** und kein
  Anlass, den Befund per zweitem Suite-Lauf zu überprüfen. Tests, die
  `tesseract` oder poppler (`pdftoppm`/`pdfinfo`) brauchen — beides kommt aus
  Systempaketen, nicht aus pip —, überspringen sich selbst mit einem Grund,
  der das fehlende Werkzeug nennt; die Zusammenfassung des Laufs zählt sie als
  `skipped`. Die Prüfungen dafür stehen in `config/test_requirements.py` und
  gehören **eng** an den einzelnen Test, nie pauschal an eine ganze Datei —
  sonst verschwindet Prüfumfang unbemerkt.
- Neue Tests gehören zu jeder Änderung dazu. Vertragstests zu
  Architekturregeln, Sichtbarkeitsprüfungen und Regressionstests zu behobenen
  Fehlern werden **nie** entfernt — auch nicht, um Laufzeit zu sparen.
- **Während ein Testlauf läuft, keine Arbeiten an der Datenbank** (Migrationen
  ausprobieren, Shell-Kommandos gegen die Test-DB). Der Lauf hält sie; man
  wartet sonst doppelt. Entweder erst fertig arbeiten und dann testen, oder
  den Lauf abwarten.

## Pflegeregel

Wer eine Entscheidung dieser Klasse trifft oder ändert, aktualisiert diese
Datei **im selben PR**. Sonst wandert die Erkenntnis wieder nur in eine
PR-Beschreibung und ist beim nächsten Ticket unsichtbar — genau das Problem,
das diese Datei lösen soll. Wer einen zugehörigen Vertragstest in
`apps/documents/test_contracts.py` bewusst ändert, ändert diese Datei mit.

Und umgekehrt: Was sich aus dem Code ablesen lässt, gehört **nicht** hierher,
sondern in einen Docstring. Eine Datei, die niemand mehr zu Ende liest, hat
ihren Zweck verfehlt.