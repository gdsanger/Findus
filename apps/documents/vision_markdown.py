"""Ausgabekontrakt der KI-Vision-Extraktion nach Markdown (#1148).

Der erzwungene Vision-Lauf (`apps.documents.extraction.
reextract_document_with_vision`, #1143) transkribierte bisher in Fliesstext.
Bei tabellarischen Vorlagen -- Laborbefund, Kontoauszug, Positionsliste --
zerstoert das genau die Information, wegen der man ueberhaupt ein Vision-
Modell fragt: Bezeichnung, Wert, Referenzbereich und Einheit landen als vier
voneinander getrennte Bloecke untereinander, und die Zuordnung "Wert X gehoert
zu Zeile Y" ist weg. Ein Mensch rekonstruiert sie muehsam, ein LLM raet.

Dieses Modul haelt deshalb den *Kontrakt* der Antwort fest -- Prompt,
Normalisierung, Seitenmarker -- getrennt vom Lauf selbst (der bleibt in
`extraction.py`, damit es weiterhin genau einen erzwungenen Vision-Lauf gibt
und nicht zwei fast gleiche). Was hier steht, ist die Antwort auf "wie muss
die Ausgabe aussehen"; wie sie erzeugt und gespeichert wird, steht dort.

Die Regeln des Prompts, mit ihrem Grund:

* **Tabellen bleiben Tabellen** -- eine Markdown-Tabellenzeile je fachlicher
  Zeile. Das ist der eigentliche Zweck des Features.
* **Handschrift getrennt** -- handschriftliche Vermerke, Haken und
  Einkreisungen sind Aussagen *ueber* das Dokument, nicht Teil seines
  gedruckten Inhalts. In die Tabelle gemischt waeren sie spaeter nicht mehr
  von den Druckwerten unterscheidbar (dieselbe Ueberlegung wie bei
  Kommentaren vs. Dokumentinhalt in CLAUDE.md).
* **Unsicheres kennzeichnen statt raten** -- ein falsch geratener Betrag ist
  schaedlicher als eine markierte Luecke.
* **Leere/unlesbare Seiten ausweisen** -- eine still uebersprungene Seite
  sieht spaeter aus wie eine Seite ohne Inhalt; eine halluzinierte ist noch
  schlimmer.
* **Keine Interpretation** -- Auswertung, Bewertung und Zusammenfassung sind
  eigene Features (KI-Analyse, ausfuehrliche Zusammenfassung). Dieser Lauf
  transkribiert nur.

`PROMPT_VERSION` geht in den Idempotenz-Fingerabdruck ein (siehe
`source_fingerprint`): Wer den Prompt fachlich aendert, zaehlt ihn hoch und
macht damit alle vorhandenen Markdown-Fassungen wieder erneuerungsfaehig,
ohne dass jemand von Hand nachhalten muss, welches Dokument mit welcher
Fassung erzeugt wurde.
"""

from __future__ import annotations

import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)

# Hochzaehlen, wenn sich `PAGE_PROMPT` fachlich aendert (siehe Modul-Docstring).
PROMPT_VERSION = "1"

# Genau die Zeile, die das Modell fuer eine leere/unlesbare Seite ausgeben
# soll -- und die `page_is_unreadable()` wiedererkennt. Bewusst als
# kursiver Markdown-Text formuliert: er darf so, wie er ist, in der
# Markdown-Ansicht stehenbleiben.
UNREADABLE_PAGE_MARKER = "_Keine lesbaren Inhalte auf dieser Seite._"

# Platzhalter fuer eine Seite, deren *Modellaufruf* fehlgeschlagen ist --
# etwas anderes als eine leere Seite (oben): dort hat das Modell geantwortet,
# hier gar nicht. Eine fehlerhafte Seite darf die uebrigen nicht entwerten
# (#1148), also bleibt sie als benannte Luecke stehen, statt den ganzen Lauf
# zu verwerfen oder -- schlimmer -- lautlos zu verschwinden und die
# Seitenzaehlung zu verschieben.
FAILED_PAGE_PLACEHOLDER = "_Diese Seite konnte nicht verarbeitet werden ({reason})._"

PAGE_PROMPT = (
    "Du transkribierst eine einzelne Seite eines Dokuments nach Markdown. "
    "Gib ausschliesslich die Transkription aus: keine Einleitung, keine "
    "Erklaerung, keine abschliessende Bemerkung, keine Code-Fences.\n"
    "\n"
    "1. Struktur erhalten. Tabellarische Bereiche werden echte "
    "Markdown-Tabellen (Pipe-Schreibweise) mit genau einer Tabellenzeile je "
    "fachlicher Zeile: Bezeichnung, Wert, Referenzbereich und Einheit "
    "gehoeren in dieselbe Zeile, niemals als getrennte Listen untereinander. "
    "Uebernimm die Spaltenueberschriften der Vorlage; gibt es zu einer Zelle "
    "keinen Wert, lass sie leer.\n"
    "2. Fliesstext bleibt Fliesstext, Aufzaehlungen bleiben Aufzaehlungen. "
    "Ueberschriften der Seite ab Ebene 3 ('### '), weil Dokumenttitel und "
    "Seitennummer ausserhalb ergaenzt werden.\n"
    "3. Handschriftliche Vermerke, Haken, Unterstreichungen, Einkreisungen "
    "und Stempel gehoeren NICHT in die Tabelle und nicht in den Fliesstext. "
    "Sammle sie am Ende der Seite unter der Ueberschrift "
    "'### Handschriftliche Vermerke', je Vermerk eine Aufzaehlungszeile mit "
    "dem Bezug, auf den er zeigt. Gibt es keine, lass den Abschnitt weg.\n"
    "4. Unsichere Lesungen kennzeichnen statt raten: '[unsicher: Wortlaut]'. "
    "Ist eine Stelle gar nicht zu entziffern: '[unleserlich]'.\n"
    "5. Ist die Seite leer, ueberbelichtet, verdeckt oder aus einem anderen "
    f"Grund nicht lesbar, gib genau diese eine Zeile aus: "
    f"'{UNREADABLE_PAGE_MARKER}' und dahinter in Klammern den Grund. "
    "Erfinde in diesem Fall keinen Inhalt.\n"
    "6. Nicht kommentieren, nicht zusammenfassen, nicht interpretieren, "
    "nichts ergaenzen, was nicht auf der Seite steht. Zahlen, Einheiten, "
    "Schreibweisen und Reihenfolge bleiben unveraendert."
)

# Ein in ```-Fences gewickeltes Ergebnis ist das haeufigste Abweichen vom
# Kontrakt oben ("keine Code-Fences") -- und das schaedlichste, weil die
# Markdown-Ansicht die ganze Seite dann als Quelltextblock rendert statt als
# Tabelle. Deshalb wird es abgeraeumt statt reklamiert.
_FENCE_RE = re.compile(r"\A```[a-zA-Z0-9_-]*\s*\n(?P<body>.*?)\n?```\s*\Z", re.DOTALL)

# Zulaessige Werte von `FINDUS_VISION_MARKDOWN_AUTO_SCOPE` -- siehe
# `auto_scope_includes`.
AUTO_SCOPE_OFF = "off"
AUTO_SCOPE_SCANS = "scans"
AUTO_SCOPE_ALL = "all"
AUTO_SCOPES = (AUTO_SCOPE_OFF, AUTO_SCOPE_SCANS, AUTO_SCOPE_ALL)


def normalize_page_markdown(text: str) -> str:
    """Bringt eine Modellantwort auf die im Kontrakt zugesagte Form.

    Nur zwei Eingriffe, beide verlustfrei: umschliessende Code-Fences
    entfernen (siehe `_FENCE_RE`) und Rand-Leerraum abschneiden. Alles
    andere -- auch eine Antwort, die sich nicht an die Regeln haelt --
    bleibt so stehen, wie das Modell sie geliefert hat: eine "Reparatur"
    des Markdowns waere genau das stille Umdeuten, das der Kontrakt
    verhindern soll.
    """
    stripped = (text or "").strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group("body").strip()
    return stripped


def page_is_unreadable(markdown: str) -> bool:
    """True, wenn die Seite als leer/unlesbar ausgewiesen wurde (Regel 5).

    Nur fuers Logging und die Statistik eines Laufs gedacht -- der Marker
    selbst bleibt im Text stehen, damit die Luecke auch in der Ansicht und
    im Index sichtbar ist statt nur in einer Zahl.
    """
    return markdown.strip().startswith(UNREADABLE_PAGE_MARKER)


def failed_page_markdown(reason: str) -> str:
    """Platzhalter fuer eine Seite, deren Modellaufruf fehlgeschlagen ist."""
    return FAILED_PAGE_PLACEHOLDER.format(reason=reason)


def join_pages_as_text(pages: list[str]) -> str:
    """Seiten zu `Document.text_content` verketten.

    Die Seitengrenze bleibt im Klartext kenntlich ('--- Seite N ---', #1143),
    damit Absaetze nicht ueber Seiten hinweg verkleben und ein Retrieval-
    Treffer sagen kann, von welcher Seite er stammt. Anders als die
    '## Seite N'-Ueberschriften in `extraction.build_markdown`, die zur
    Markdown-*Ansicht* gehoeren.

    Eine einzelne Seite bekommt keinen Marker: ein Bild hat keine
    Seitengrenze, die man kenntlich machen muesste.
    """
    if len(pages) > 1:
        return "\n\n".join(
            f"--- Seite {index} ---\n\n{page}" for index, page in enumerate(pages, start=1)
        )
    return pages[0] if pages else ""


def source_fingerprint(document) -> str:
    """Fingerabdruck von "diese Datei, mit diesem Prompt transkribiert".

    Ist er identisch mit `Document.vision_reextraction_fingerprint`, waere
    ein erneuter Lauf ein Modellaufruf mit garantiert demselben Ergebnis --
    also Kosten ohne Gegenwert (#1148). Der Datei-Hash steckt schon in
    `Document.sha256` (global eindeutig, siehe `apps.ingest.service`); dazu
    kommt `PROMPT_VERSION`, damit eine Prompt-Aenderung den Bestand
    erneuerungsfaehig macht, ohne dass jemand von Hand nachhaelt, welches
    Dokument mit welcher Fassung erzeugt wurde.

    Das *Modell* geht bewusst nicht ein: es wird nicht am Dokument
    gespeichert, sondern in der Instanz-Konfiguration gesetzt, und ein
    Modellwechsel ist eine Betriebsentscheidung -- die zugehoerige
    Neuerzeugung laeuft ueber den Knopf am Dokument bzw.
    `manage.py extract_vision_markdown --force`, nicht ueber einen
    Automatismus, der bei der naechsten Konfigurationsaenderung ungefragt
    den ganzen Bestand neu durch ein Vision-Modell schickt.
    """
    return f"{document.sha256}:{PROMPT_VERSION}"


def is_up_to_date(document) -> bool:
    """True, wenn fuer genau diese Datei + Prompt-Fassung bereits eine
    erfolgreiche Markdown-Fassung vorliegt.
    """
    from .models import Document

    if document.vision_reextraction_status != Document.VisionReextractionStatus.READY:
        return False
    if not document.vision_reextraction_fingerprint:
        return False
    return document.vision_reextraction_fingerprint == source_fingerprint(document)


def auto_scope_includes(document) -> bool:
    """Datenschutz-Schalter: Darf dieses Dokument *automatisch* an den
    konfigurierten Vision-Provider gehen? (`FINDUS_VISION_MARKDOWN_AUTO_SCOPE`)

    Anhaenge tragen personenbezogene und besonders sensible Inhalte
    (Befunde, Kontoauszuege, Bescheide). Ob sie an einen Provider gehen, ist
    deshalb eine bewusste Entscheidung und kein Nebeneffekt eines Uploads --
    der Default ist `off`, also kein automatischer Egress.

    * `off`   -- gar keine automatische Extraktion. Nur der Knopf am
                 Dokument bzw. das Management-Command loesen dann noch einen
                 Lauf aus; beides ist eine ausgesprochene Einzelfall-
                 Entscheidung eines Menschen.
    * `scans` -- nur Dokumente, bei denen die Kaskade ueberhaupt eskalieren
                 musste (`extraction_method` = OCR oder Vision), also Scans
                 und Fotos ohne brauchbaren Text-Layer. Genau der Fall, den
                 #1148 beschreibt; ein digital erzeugtes PDF mit sauberem
                 Text-Layer hat nichts davon und wuerde nur Tokens kosten.
    * `all`   -- jedes seitenweise renderbare Dokument (PDF/Bild).

    Bewusst *keine* Steuerung je Vorgang: ein Dokument haengt an beliebig
    vielen Vorgaengen (n:n), zwei davon koennten die Frage "darf diese Datei
    raus?" unterschiedlich beantworten -- und die Antwort waere dann davon
    abhaengig, ueber welchen Vorgang man gerade schaut. Die Instanz-weite
    Einstellung plus die Einzelfall-Entscheidung am Dokument sind beide
    eindeutig.
    """
    scope = getattr(settings, "FINDUS_VISION_MARKDOWN_AUTO_SCOPE", AUTO_SCOPE_OFF)
    if scope not in AUTO_SCOPES:
        logger.warning(
            "FINDUS_VISION_MARKDOWN_AUTO_SCOPE=%r ist kein gueltiger Wert (%s) -- "
            "es wird nicht automatisch extrahiert.",
            scope,
            ", ".join(AUTO_SCOPES),
        )
        return False
    if scope == AUTO_SCOPE_OFF:
        return False
    if not document.supports_vision_reextraction:
        return False
    if scope == AUTO_SCOPE_ALL:
        return True

    from .models import Document

    return document.extraction_method in (
        Document.ExtractionMethod.OCR,
        Document.ExtractionMethod.VISION,
    )


def should_extract_automatically(document) -> bool:
    """Der vollstaendige Automatik-Test: Format + Datenschutz-Schalter +
    Idempotenz. Wird von `tasks.extract_document_task` gefragt, *bevor* ein
    Task eingereiht wird -- ein reiner DB-Blick, kein Modellaufruf.
    """
    return auto_scope_includes(document) and not is_up_to_date(document)
