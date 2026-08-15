from django.apps import AppConfig
from django.db.models.signals import post_migrate


class DocumentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.documents"
    label = "documents"
    verbose_name = "Documents"

    def ready(self):
        from . import schedules

        post_migrate.connect(schedules.sync_document_comment_reminder_schedule, sender=self)
