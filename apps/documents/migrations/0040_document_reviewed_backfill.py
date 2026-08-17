"""Bestand bei Einführung des Prüfschritts auf "Geprüft" heben (#1153).

`ProcessingStatus` bekommt mit diesem Ticket den neuen Endzustand
`reviewed` nach `ready`; `ready` selbst heißt ab sofort "Zu prüfen". Ohne
diesen Backfill stünden alle ~400 im Bestand bereits fertig verarbeiteten
Dokumente am ersten Tag in der neuen Liste "Zu prüfen" -- niemand klickt
400 Dokumente durch, nur um die Funktion in Betrieb zu nehmen. Wer ein
einzelnes Altdokument doch prüfen will, setzt es über den Umschalter
zurück (`reviewed -> ready`).

Reine Statusverschiebung per `.update()` (kein Feld hängt an einer
Python-Berechnung) -- `processing_reviewed_at`/`_by` bleiben für diese
gehobenen Bestandsdokumente bewusst leer: es hat niemand tatsächlich
geprüft, ein erfundener Zeitstempel wäre irreführend.

Kein Import aus `apps.documents.models`: eine Migration muss auch dann
noch laufen, wenn `ProcessingStatus` im Anwendungscode später umbenannt
wird -- deshalb rohe Stringwerte statt der Enum-Konstanten.
"""

from django.db import migrations


def mark_ready_as_reviewed(apps, schema_editor):
    Document = apps.get_model("documents", "Document")
    Document.objects.filter(processing_status="ready").update(
        processing_status="reviewed"
    )


def mark_reviewed_as_ready(apps, schema_editor):
    Document = apps.get_model("documents", "Document")
    Document.objects.filter(processing_status="reviewed").update(
        processing_status="ready",
        processing_reviewed_at=None,
        processing_reviewed_by=None,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0039_document_processing_reviewed_at_and_more"),
    ]

    operations = [
        migrations.RunPython(mark_ready_as_reviewed, mark_reviewed_as_ready),
    ]
