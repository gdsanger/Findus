"""Die Ausführung der Scan-Korrektur (#1155): Plan -> Datei(en) -> Ablage.

Setzt auf `apps.documents.pdf_editing` (was mit den Seiten passiert) auf
und beantwortet die zweite Hälfte: wohin das Ergebnis geht. Zwei Fälle,
bewusst unterschiedlich:

* **Drehen/Löschen ohne Schnittmarke** -- das Dokument behält seine
  Identität. Dieselbe Datensatz-Kennung, dieselben Kommentare,
  Wiedervorlagen, Verknüpfungen, Vorgänge, Tags, Kontakt und
  Erledigungsstatus; nur die Datei dahinter wird ersetzt. Genau dafür gibt
  es diese Funktion überhaupt: der bisherige Ausweg ("löschen, außerhalb
  reparieren, neu hochladen") kostet all das.
* **Aufteilen** -- aus einem Fehlscan werden N Dokumente über
  `apps.ingest.service.ingest_file` (Dedup, Ablage, Sichtbarkeit, Enqueue
  sind dort *ein* Vertrag, der hier nicht nachgebaut wird), und das
  Original wird **danach** gelöscht. Reihenfolge ist der Kern: erst alle
  Teile vollständig anlegen, dann das Original weg -- es darf nie ein
  Zustand entstehen, in dem das Original fehlt und die Teile unvollständig
  sind.

Ein Lauf ist gegen Doppelausführung abgesichert (`claimed_at`, siehe
`DocumentPdfEditRun`): ein zweiter Anlauf desselben Laufs -- Django-Q
reiht einen Task erneut ein, jemand klickt zweimal -- findet ihn belegt
und tut nichts. Ohne das entstünden beim Aufteilen doppelte Teile.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile

from django.core.files import File
from django.db import transaction
from django.utils import timezone
from django_q.exceptions import TimeoutException

from .models import Document, DocumentPdfEditRun
from .page_previews import discard_page_previews
from .pdf_editing import (
    PdfEditError,
    PdfEditPlan,
    iter_edited_parts,
    part_filename,
    split_page_groups,
)
from .thumbnails import generate_thumbnail_for_document

logger = logging.getLogger(__name__)

_HASH_CHUNK_SIZE = 1024 * 1024
_SPOOL_MAX_BYTES = 8 * 1024 * 1024

#: Was ein Teil vom Original erbt -- ausschließlich, was am Papier hängt.
#: Kontakt, Titel, Datum, Typ, Tags und Vorgänge erbt es ausdrücklich
#: NICHT: der Grund des Aufteilens ist ja gerade, dass die Teile zu
#: verschiedenen Vorgängen und Absendern gehören. Die KI-Analyse ermittelt
#: sie je Teil neu; eine geerbte Fehlzuordnung müsste man an jedem Teil
#: einzeln wieder entfernen.
_INHERITED_FIELDS = ("sphere", "direction")


def _stream_to_temp(fileobj) -> tempfile.SpooledTemporaryFile:
    """Kopiert einen Storage-Stream in eine (ab einer Größe auf Platte
    ausgelagerte) temporäre Datei.

    pypdf braucht einen frei positionierbaren Strom; ein S3-/MinIO-Handle
    ist das nicht zuverlässig. Bewusst kein `read()` in den Speicher: ein
    300-Seiten-Scan soll den Worker nicht sprengen (siehe
    `pdf_editing.iter_edited_parts`, das für die Ausgabe dasselbe tut).
    """
    buffer = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_BYTES, suffix=".pdf")
    for chunk in iter(lambda: fileobj.read(_HASH_CHUNK_SIZE), b""):
        buffer.write(chunk)
    buffer.seek(0)
    return buffer


def _sha256_and_size(fileobj) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: fileobj.read(_HASH_CHUNK_SIZE), b""):
        digest.update(chunk)
        size += len(chunk)
    fileobj.seek(0)
    return digest.hexdigest(), size


def _claim(run_id: int) -> bool:
    """Belegt den Lauf für genau einen Ausführenden. Ein einzelnes
    `UPDATE ... WHERE claimed_at IS NULL` -- wer die Zeile bekommt, führt
    aus; alle anderen sehen 0 geänderte Zeilen und lassen es."""
    claimed = DocumentPdfEditRun.objects.filter(pk=run_id, claimed_at__isnull=True).update(
        claimed_at=timezone.now()
    )
    return bool(claimed)


def _fail(run: DocumentPdfEditRun, message: str) -> DocumentPdfEditRun:
    run.status = DocumentPdfEditRun.Status.FAILED
    run.error = message
    run.completed_at = timezone.now()
    run.save(update_fields=["status", "error", "completed_at", "updated_at"])
    return run


def _replace_original(document: Document, run: DocumentPdfEditRun, pages, buffer) -> None:
    """Fall 1: das bearbeitete PDF ersetzt die Datei des Dokuments.

    Die Prüfsumme wird neu berechnet, damit die Dublettenerkennung weiter
    stimmt -- ergibt sie eine Dublette zu einem *anderen* Dokument, wird
    die Bearbeitung abgelehnt und **nichts** verändert (nicht still
    zusammengeführt: welches der beiden gewinnt, hat niemand entschieden).

    Danach läuft die Verarbeitung vollständig neu (Extraktion -> KI-Analyse
    -> Chunking/Embedding) über denselben Task, den auch der Ingest
    einreiht. Die Chunks des alten Standes fallen sofort weg, nicht erst
    beim Neu-Einbetten: sonst bliebe der Text gelöschter Seiten bis zum
    Ende des Laufs (und bei einem Fehlschlag dauerhaft) durchsuchbar --
    ein Fehler, der niemandem auffällt, weil nichts kaputtgeht.
    """
    sha256, size = _sha256_and_size(buffer)
    duplicate = Document.objects.filter(sha256=sha256).exclude(pk=document.pk).first()
    if duplicate is not None:
        raise PdfEditError(
            f"Die bearbeitete Datei entspricht exakt dem bereits vorhandenen Dokument "
            f"„{duplicate.title}“ (#{duplicate.pk}). Die Bearbeitung wurde abgebrochen, "
            f"am Dokument wurde nichts verändert."
        )

    previous_name = document.original_file.name
    document.original_file.save(
        part_filename(document.original_filename, 1, 1), File(buffer), save=False
    )
    document.sha256 = sha256
    document.metadata = {
        **document.metadata,
        "size": size,
        "page_count": len(pages),
    }
    document.processing_status = Document.ProcessingStatus.PENDING
    document.processing_error = ""
    document.save(
        update_fields=[
            "original_file",
            "sha256",
            "metadata",
            "processing_status",
            "processing_error",
            "updated_at",
        ]
    )

    document.chunks.all().delete()
    discard_page_previews(document)
    if previous_name and previous_name != document.original_file.name:
        document.original_file.storage.delete(previous_name)

    # Erst nach dem Ersetzen und mit `force`: die erste Seite kann eine
    # andere sein als vorher (gelöscht oder gedreht), das alte Thumbnail
    # zeigt sonst eine Seite, die es nicht mehr gibt.
    generate_thumbnail_for_document(document.id, force=True)

    run.mode = DocumentPdfEditRun.Mode.EDIT
    run.result = {"document_id": document.pk, "pages": list(pages)}

    from django_q.tasks import async_task

    from .tasks import extract_document_task

    async_task(extract_document_task, document.id)


def _create_part(document: Document, pages, buffer, *, index: int, total: int):
    """Legt *ein* Teil über den Ingest-Dienst an und hängt daran, was am
    Papier hängt.

    `ingest_file` bleibt der einzige Weg ins Archiv (CLAUDE.md,
    "Pipelines & Services") -- inklusive seiner Dublettenerkennung: ist das
    Teil bereits als Dokument vorhanden, liefert er es zurück, ohne etwas
    anzulegen. Das wird dem Nutzer gemeldet, nicht verschluckt.
    """
    from apps.ingest.service import ingest_file

    result = ingest_file(
        buffer,
        filename=part_filename(document.original_filename, index, total),
        source=document.source,
        owner=document.owner,
        visibility=document.visibility,
        content_type="application/pdf",
        origin_metadata={
            "split_from_document_id": document.pk,
            "split_from_pages": list(pages),
        },
    )
    if result.created:
        part = result.document
        for field in _INHERITED_FIELDS:
            setattr(part, field, getattr(document, field))
        part.save(update_fields=[*_INHERITED_FIELDS, "updated_at"])
        part.departments.set(document.departments.all())
        # Eingangsdatum des Originals übernehmen, damit die Teile in der
        # Zeitachse nicht auf "heute" springen und dort als neu erscheinen.
        # `created_at` ist `auto_now_add`, lässt sich also nur per UPDATE
        # setzen -- und nur nach dem Anlegen, sonst überschreibt der Save
        # es wieder.
        Document.objects.filter(pk=part.pk).update(created_at=document.created_at)
    return result


def _split_into_parts(document: Document, run: DocumentPdfEditRun, parts) -> None:
    """Fall 2: aus dem Fehlscan werden N Dokumente, das Original geht.

    Reihenfolge und Rücknahme sind der eigentliche Inhalt dieser Funktion:
    alle Teile entstehen in *einer* Transaktion, das Original wird erst
    darin gelöscht, wenn jedes Teil steht. Scheitert eines, rollt die
    Transaktion die angelegten Zeilen zurück -- die bereits geschriebenen
    Dateien im Object Storage räumt dieser Code selbst weg, denn die kennt
    keine Transaktion.

    Die Datei des Originals wird bewusst **nach** dem Commit gelöscht: eine
    zurückgerollte Transaktion darf kein Dokument ohne Datei hinterlassen.

    Was ein Rollback nicht einfangen kann: `ingest_file` reiht die
    Extraktion jedes Teils sofort ein. Nach einer zurückgerollten Teilung
    laufen diese Tasks ins Leere (`Document.DoesNotExist`) und werden von
    Django-Q als fehlgeschlagen vermerkt -- Rauschen im Task-Log, aber
    kein Zustand am Archiv; das Original steht unverändert da.
    """
    created_results = []
    entries = []

    try:
        with transaction.atomic():
            total = len(parts)
            for index, (pages, buffer) in enumerate(parts, start=1):
                try:
                    result = _create_part(document, pages, buffer, index=index, total=total)
                finally:
                    buffer.close()
                created_results.append(result)
                entries.append(
                    {
                        "index": index,
                        "document_id": result.document.pk,
                        "title": result.document.title,
                        "pages": list(pages),
                        "duplicate": result.duplicate,
                    }
                )

            discard_page_previews(document)
            document.delete()
    except Exception:
        for result in created_results:
            if result.created and result.document.original_file:
                result.document.original_file.delete(save=False)
        raise

    # Nach dem Commit: die Zeile ist weg, die `FieldFile`-Namen stehen noch
    # am Objekt im Speicher -- dieselbe explizite Storage-Aufräumung wie in
    # `views.document_delete` (Django räumt FileField-Inhalte nie selbst).
    if document.original_file:
        document.original_file.delete(save=False)
    if document.thumbnail:
        document.thumbnail.delete(save=False)

    run.mode = DocumentPdfEditRun.Mode.SPLIT
    run.document = None
    run.result = {"parts": entries}


def apply_pdf_edit_run(run_id: int) -> DocumentPdfEditRun:
    """Führt den bestätigten Plan eines Laufs aus (#1155).

    Der Plan kommt aus `run.plan`, nicht aus dem Request -- ein erneut
    eingereihter Task täte damit exakt dasselbe. Ausgeführt wird er aber
    höchstens einmal (`claimed_at`).

    Terminaler Fehlerfall ist `run.status = failed` samt lesbarer Meldung:
    eine kaputte oder passwortgeschützte Datei, eine Dublette, ein
    abgebrochener Job -- die Oberfläche zeigt das statt endlos zu drehen.
    `processing_status` des Dokuments bleibt davon unberührt (CLAUDE.md,
    "Pipelines & Services": nur Extraktion und Embedding dürfen die
    Pipeline auf `failed` stellen; dieser Lauf steht davor).

    `TimeoutException` (Django-Q, erbt von `SystemExit`) wird aufgezeichnet
    und weitergereicht, damit Django-Q den Worker neu startet -- dieselbe
    Aufgabenteilung wie bei den LLM-Jobs (`long_summary`, `extraction`).
    """
    if not _claim(run_id):
        logger.info(
            "PDF-Bearbeitung: Lauf %s ist bereits in Ausfuehrung/ausgefuehrt, "
            "zweiter Anlauf uebersprungen",
            run_id,
        )
        return DocumentPdfEditRun.objects.get(pk=run_id)

    run = DocumentPdfEditRun.objects.get(pk=run_id)
    document = run.document
    if document is None:
        return _fail(run, "Das Dokument existiert nicht mehr.")

    plan = PdfEditPlan.from_dict(run.plan)
    try:
        document.original_file.open("rb")
        try:
            source = _stream_to_temp(document.original_file)
        finally:
            document.original_file.close()

        try:
            parts = list(iter_edited_parts(source, plan))
        finally:
            source.close()

        if len(parts) == 1:
            pages, buffer = parts[0]
            try:
                _replace_original(document, run, pages, buffer)
            finally:
                buffer.close()
        else:
            _split_into_parts(document, run, parts)

        run.status = DocumentPdfEditRun.Status.READY
        run.error = ""
        run.completed_at = timezone.now()
        run.save(
            update_fields=["status", "mode", "result", "error", "document", "completed_at", "updated_at"]
        )
        logger.info(
            "PDF-Bearbeitung: Lauf %s fertig (mode=%s, plan=%s)", run_id, run.mode, run.plan
        )
    except TimeoutException:
        logger.exception("PDF-Bearbeitung: Lauf %s hat sein Zeitbudget aufgebraucht", run_id)
        _fail(
            run,
            "Zeitüberschreitung – die Bearbeitung wurde abgebrochen. "
            "Am Dokument wurde nichts verändert.",
        )
        raise
    except PdfEditError as exc:
        logger.info("PDF-Bearbeitung: Lauf %s abgelehnt (%s)", run_id, exc)
        _fail(run, str(exc))
    except Exception as exc:
        logger.exception("PDF-Bearbeitung: Lauf %s fehlgeschlagen", run_id)
        _fail(run, f"Die Bearbeitung ist fehlgeschlagen: {exc}")

    return run


def plan_part_count(plan: PdfEditPlan, *, page_count: int) -> int:
    """Wie viele Dokumente der Plan hinterlässt -- 1 heißt "dasselbe
    Dokument, neue Datei", alles darüber heißt aufteilen (und damit: das
    Original wird gelöscht)."""
    return len(split_page_groups(page_count, plan))
