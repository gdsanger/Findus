from django.core.management.base import BaseCommand

from apps.documents.models import Document
from apps.documents.processing import process_document


class Command(BaseCommand):
    help = (
        "Chunked und embedded den Dokumentbestand neu -- der geplante "
        "Migrationsweg fuer einen Wechsel von FINDUS_AI_EMBEDDING_PROVIDER, "
        "Embedding-Modell oder FINDUS_EMBEDDING_DIMENSIONS, da Chunks aus "
        "unterschiedlichen Modellen/Dimensionen nicht vergleichbar sind "
        "(siehe Chunk.embedding_model/_version)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--document-id",
            type=int,
            action="append",
            dest="document_ids",
            help=(
                "Nur dieses Dokument neu indizieren (wiederholbar). Ohne "
                "Angabe: alle Dokumente mit text_content."
            ),
        )
        parser.add_argument(
            "--queue",
            action="store_true",
            help=(
                "Pro Dokument einen Django-Q2-Task einreihen statt synchron "
                "im Management-Command zu verarbeiten (fuer einen grossen "
                "Bestand, damit der Befehl nicht den ganzen Re-Index lang "
                "blockiert)."
            ),
        )

    def handle(self, *args, **options):
        document_ids = options["document_ids"]
        if document_ids:
            documents = Document.objects.filter(pk__in=document_ids)
        else:
            documents = Document.objects.exclude(text_content="")
        ids = list(documents.order_by("pk").values_list("pk", flat=True))

        if not ids:
            self.stdout.write("Keine Dokumente zum Re-Indexieren gefunden.")
            return

        if options["queue"]:
            from django_q.tasks import async_task

            from apps.documents.tasks import process_document_task

            for document_id in ids:
                async_task(process_document_task, document_id)
            self.stdout.write(f"{len(ids)} Dokument(e) zum Re-Index eingereiht.")
            return

        succeeded = 0
        failed = 0
        for document_id in ids:
            try:
                process_document(document_id)
            except Exception as exc:
                failed += 1
                self.stderr.write(f"Document {document_id}: fehlgeschlagen ({exc}).")
            else:
                succeeded += 1

        self.stdout.write(
            f"Re-Index abgeschlossen: {succeeded} erfolgreich, {failed} fehlgeschlagen."
        )
