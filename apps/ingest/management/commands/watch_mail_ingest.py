from django.core.management.base import BaseCommand

from apps.ingest.connectors.mail import scan_all_mail_sources, watch_mail_forever


class Command(BaseCommand):
    help = (
        "Pollt die konfigurierten Mail-Postfächer (IMAP + Microsoft Graph, "
        "settings.FINDUS_INGEST_MAIL_SOURCES) und importiert Anhänge/Mailbody "
        "neuer Mails als Document."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Nur einen Durchlauf ausführen statt dauerhaft zu pollen (z. B. Cron).",
        )

    def handle(self, *args, **options):
        if options["once"]:
            scan_all_mail_sources()
        else:
            watch_mail_forever()
