from django.apps import AppConfig
from django.db.models.signals import post_migrate


class IngestConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ingest"
    label = "ingest"
    verbose_name = "Ingest"

    def ready(self):
        from . import schedules

        post_migrate.connect(schedules.sync_mail_ingest_schedule, sender=self)
