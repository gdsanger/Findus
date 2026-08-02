from django.core.management.base import BaseCommand

from apps.ingest.connectors.folder import scan_all_folders, watch_forever


class Command(BaseCommand):
    help = (
        "Pollt die konfigurierten Ingest-Eingangsordner "
        "(settings.FINDUS_INGEST_WATCH_FOLDERS) und importiert neue Dateien."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Nur einen Durchlauf ausführen statt dauerhaft zu pollen (z. B. Cron).",
        )

    def handle(self, *args, **options):
        if options["once"]:
            scan_all_folders()
        else:
            watch_forever()
