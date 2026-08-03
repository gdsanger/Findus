from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils.dateparse import parse_date

from apps.documents.analysis import analyze_and_finalize as _analyze_and_finalize
from apps.documents.models import Document


class Command(BaseCommand):
    help = (
        "Fuehrt die KI-Analyse (#1020) nachtraeglich auf bereits vorhandene "
        "Dokumente aus -- z. B. um Dokumente mit metadata.analysis_error "
        "nach einem Fix (etwa #1028, robustes JSON-Parsing) neu zu "
        "analysieren, oder nach einer Prompt-/Modelaenderung. Ruft denselben "
        "analyze_document() auf, den auch der Ingest-Worker nutzt -- keine "
        "Logik-Dublette."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--id",
            type=int,
            action="append",
            dest="document_ids",
            help="Nur dieses Dokument neu analysieren (wiederholbar).",
        )
        parser.add_argument(
            "--failed",
            action="store_true",
            help=(
                "Nur Dokumente mit metadata.analysis_error oder leerer "
                "summary."
            ),
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="select_all",
            help="Alle Dokumente neu analysieren, auch bereits erfolgreiche.",
        )
        parser.add_argument(
            "--since",
            type=str,
            default=None,
            help=(
                "Zusaetzlicher Filter: nur Dokumente, die seit diesem Datum "
                "(YYYY-MM-DD) angelegt wurden."
            ),
        )
        parser.add_argument(
            "--queue",
            action="store_true",
            help=(
                "Pro Dokument einen Django-Q2-Task einreihen statt synchron "
                "im Management-Command zu verarbeiten."
            ),
        )

    def handle(self, *args, **options):
        document_ids = options["document_ids"]
        failed = options["failed"]
        select_all = options["select_all"]
        since = options["since"]

        selected_modes = sum(bool(value) for value in (document_ids, failed, select_all))
        if selected_modes == 0:
            raise CommandError("Bitte --failed, --all oder --id angeben.")
        if selected_modes > 1:
            raise CommandError("--failed, --all und --id schliessen sich gegenseitig aus.")

        if document_ids:
            documents = Document.objects.filter(pk__in=document_ids)
        elif failed:
            documents = Document.objects.filter(
                Q(metadata__has_key="analysis_error") | Q(summary="")
            ).exclude(text_content="")
        else:
            documents = Document.objects.exclude(text_content="")

        if since:
            since_date = parse_date(since)
            if since_date is None:
                raise CommandError(f"Ungueltiges Datum fuer --since: {since!r} (erwartet YYYY-MM-DD).")
            documents = documents.filter(created_at__date__gte=since_date)

        ids = list(documents.order_by("pk").values_list("pk", flat=True))

        if not ids:
            self.stdout.write("Keine Dokumente zum Analysieren gefunden.")
            return

        if options["queue"]:
            from django_q.tasks import async_task

            for document_id in ids:
                async_task(_analyze_and_finalize, document_id)
            self.stdout.write(f"{len(ids)} Dokument(e) zur KI-Analyse eingereiht.")
            return

        succeeded = 0
        failed_count = 0
        for document_id in ids:
            try:
                document = _analyze_and_finalize(document_id)
            except Exception as exc:
                failed_count += 1
                self.stderr.write(f"Document {document_id}: fehlgeschlagen ({exc}).")
                continue

            if "analysis_error" in document.metadata:
                failed_count += 1
                self.stderr.write(
                    f"Document {document_id}: KI-Analyse fehlgeschlagen "
                    f"({document.metadata['analysis_error']})."
                )
            else:
                succeeded += 1
                self.stdout.write(f"Document {document_id}: erfolgreich analysiert.")

        self.stdout.write(
            f"KI-Analyse abgeschlossen: {succeeded} erfolgreich, {failed_count} fehlgeschlagen."
        )
