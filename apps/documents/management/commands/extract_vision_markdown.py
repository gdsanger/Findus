from django.conf import settings
from django.core.management.base import BaseCommand

from apps.documents.extraction import extract_vision_markdown
from apps.documents.models import Document
from apps.documents.vision_markdown import is_up_to_date


class Command(BaseCommand):
    help = (
        "Transkribiert PDF-/Bild-Dokumente per KI-Vision strukturerhaltend "
        "nach Markdown (#1148) -- der Backfill fuer den Bestand und der "
        "Weg, nach einem Modell- oder Prompt-Wechsel neu zu erzeugen. "
        "Idempotent: unveraenderte Dateien, fuer die die aktuelle "
        "Prompt-Fassung schon vorliegt, werden ohne Modellaufruf "
        "uebersprungen; --force erzeugt sie neu. Nicht seitenweise "
        "renderbare Formate (docx, eml, zip, …) werden uebersprungen, nicht "
        "als Fehler gezaehlt.\n"
        "\n"
        "Achtung: jeder Lauf schickt jede Seite an den konfigurierten "
        "Vision-Provider (FINDUS_AI_VISION_PROVIDER) und kostet "
        "entsprechend. Der Aufruf dieses Befehls ist -- wie der Knopf am "
        "Dokument -- die bewusste Entscheidung dafuer und richtet sich "
        "deshalb nicht nach FINDUS_VISION_MARKDOWN_AUTO_SCOPE; der schaltet "
        "nur die Automatik nach dem Ingest.\n"
        "\n"
        "Der Nachlauf (KI-Analyse -> Chunking/Embedding auf dem neuen Text) "
        "wird in beiden Betriebsarten als Django-Q2-Task eingereiht -- ohne "
        "laufenden Worker steht das neue Markdown zwar am Dokument, aber "
        "noch nicht im Index."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--document-id",
            type=int,
            action="append",
            dest="document_ids",
            help=(
                "Nur dieses Dokument (wiederholbar). Ohne Angabe: alle "
                "Dokumente mit Originaldatei."
            ),
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Auch Dokumente neu transkribieren, deren Markdown-Fassung "
                "zur aktuellen Datei und Prompt-Fassung bereits vorliegt."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            help=(
                "Hoechstens so viele Dokumente verarbeiten -- damit sich ein "
                "grosser Bestand in kalkulierbaren Portionen abarbeiten "
                "laesst, statt in einem Lauf unbekannte Kosten zu erzeugen."
            ),
        )
        parser.add_argument(
            "--queue",
            action="store_true",
            help=(
                "Pro Dokument einen Django-Q2-Task einreihen statt synchron "
                "im Command zu transkribieren (fuer einen grossen Bestand, "
                "damit der Befehl nicht den ganzen Backfill lang blockiert)."
            ),
        )

    def handle(self, *args, **options):
        force = options["force"]
        if options["document_ids"]:
            documents = Document.objects.filter(pk__in=options["document_ids"])
        else:
            documents = Document.objects.exclude(original_file="")

        # Formatkreis und Idempotenz schon hier pruefen, nicht erst im Lauf:
        # sonst reiht `--queue` fuer einen ganzen Bestand Tasks ein, die
        # samt und sonders nichts tun.
        candidates = [
            document
            for document in documents.order_by("pk")
            if document.supports_vision_reextraction and (force or not is_up_to_date(document))
        ]
        if options["limit"] is not None:
            candidates = candidates[: options["limit"]]

        if not candidates:
            self.stdout.write("Keine Dokumente fuer die KI-Vision-Extraktion gefunden.")
            return

        ids = [document.pk for document in candidates]

        if options["queue"]:
            from django_q.tasks import async_task

            from apps.documents.tasks import extract_vision_markdown_task

            for document_id in ids:
                async_task(
                    extract_vision_markdown_task,
                    document_id,
                    timeout=settings.FINDUS_VISION_REEXTRACT_TASK_TIMEOUT_SECONDS,
                )
            self.stdout.write(f"{len(ids)} Dokument(e) zur KI-Vision-Extraktion eingereiht.")
            return

        from django_q.tasks import async_task

        from apps.documents.tasks import analyze_document_task

        extracted = 0
        skipped = 0
        failed = 0
        for document_id in ids:
            result = extract_vision_markdown(document_id, force=force)
            if result is None:
                skipped += 1
            elif result.vision_reextraction_status == Document.VisionReextractionStatus.READY:
                extracted += 1
                # Nachlauf wie beim Knopf am Dokument: erst die erneute
                # Analyse, die ihrerseits Chunking/Embedding anschliesst --
                # sonst durchsucht Findus weiterhin den alten Text. Bewusst
                # eingereiht statt hier synchron aufgerufen, damit es genau
                # eine Stelle gibt, die die Reihenfolge der Pipeline kennt.
                async_task(analyze_document_task, document_id)
            else:
                failed += 1
                self.stderr.write(
                    f"Document {document_id}: fehlgeschlagen "
                    f"({result.vision_reextraction_error})."
                )

        self.stdout.write(
            f"KI-Vision-Extraktion abgeschlossen: {extracted} transkribiert, "
            f"{skipped} uebersprungen, {failed} fehlgeschlagen."
        )
