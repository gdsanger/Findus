from django.core.management.base import BaseCommand

from apps.documents.models import Document
from apps.documents.thumbnails import generate_thumbnail


class Command(BaseCommand):
    help = (
        "Erzeugt Vorschaubilder (Seite 1) fuer den Dokumentbestand (#1123) -- "
        "der Backfill fuer Dokumente, die vor der Thumbnail-Pipeline "
        "eingeliefert wurden. Idempotent: bereits vorhandene Thumbnails "
        "werden uebersprungen, --force erzeugt sie neu. Nicht renderbare "
        "Formate (docx, xlsx, zip, …) werden uebersprungen, nicht als Fehler "
        "gezaehlt."
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
            help="Vorhandene Thumbnails neu erzeugen statt sie zu ueberspringen.",
        )
        parser.add_argument(
            "--queue",
            action="store_true",
            help=(
                "Pro Dokument einen Django-Q2-Task einreihen statt synchron "
                "im Command zu rendern (fuer einen grossen Bestand, damit der "
                "Befehl nicht den ganzen Backfill lang blockiert)."
            ),
        )

    def handle(self, *args, **options):
        document_ids = options["document_ids"]
        force = options["force"]
        if document_ids:
            documents = Document.objects.filter(pk__in=document_ids)
        else:
            documents = Document.objects.exclude(original_file="")
        ids = list(documents.order_by("pk").values_list("pk", flat=True))

        if not ids:
            self.stdout.write("Keine Dokumente fuer die Thumbnail-Erzeugung gefunden.")
            return

        if options["queue"]:
            from django_q.tasks import async_task

            from apps.documents.tasks import generate_thumbnail_task

            for document_id in ids:
                async_task(generate_thumbnail_task, document_id, force=force)
            self.stdout.write(f"{len(ids)} Dokument(e) zur Thumbnail-Erzeugung eingereiht.")
            return

        created = 0
        skipped = 0
        failed = 0
        for document_id in ids:
            document = Document.objects.get(pk=document_id)
            try:
                if generate_thumbnail(document, force=force):
                    created += 1
                else:
                    skipped += 1
            except Exception as exc:
                failed += 1
                self.stderr.write(f"Document {document_id}: fehlgeschlagen ({exc}).")

        self.stdout.write(
            f"Thumbnails abgeschlossen: {created} erzeugt, {skipped} uebersprungen, "
            f"{failed} fehlgeschlagen."
        )
