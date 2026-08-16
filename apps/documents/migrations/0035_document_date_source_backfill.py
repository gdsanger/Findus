"""Herkunftsvermerk für bestehende Dokumentdaten nachtragen (#1141).

Ab #1141 entscheidet `metadata["document_date_source"]`, ob eine erneute
KI-Analyse `document_date` neu bestimmen darf: nur was die Analyse selbst
gesetzt hat, wird überschrieben. Ohne diesen Backfill trüge der Bestand gar
keinen Vermerk und wäre damit vollständig gesperrt -- ausgerechnet die 40
falsch datierten Kontoauszüge, die den Anlass für das Ticket gaben, ließen
sich dann nicht mehr korrigieren.

Der Vermerk wird deshalb genau dort gesetzt, wo nachweisbar die KI das Datum
gesetzt hat: wenn `document_date` exakt dem entspricht, was der letzte
Analyse-Lauf in `key_facts["document_date"]` abgelegt hat. Weicht es ab (von
Hand korrigiert) oder gibt es keine Key-Facts (z. B. `document_date` aus dem
`Date`-Header eines EML-Imports), bleibt das Dokument unmarkiert und damit
geschützt -- im Zweifel für die Nutzerarbeit.

`update_fields=["metadata"]` lässt `updated_at` (auto_now) bewusst
unangetastet: ein Wartungslauf soll den Bestand nicht als "gerade bearbeitet"
erscheinen lassen.
"""

import datetime

from django.db import migrations

# Kein Import aus `document_dates`: eine Migration muss auch dann noch
# laufen, wenn die Konstante im Anwendungscode später umbenannt wird.
_AI_SOURCE = "modell"


def stamp_ai_document_dates(apps, schema_editor):
    Document = apps.get_model("documents", "Document")
    queryset = Document.objects.exclude(document_date=None).only(
        "id", "document_date", "key_facts", "metadata"
    )
    for document in queryset.iterator():
        metadata = document.metadata if isinstance(document.metadata, dict) else {}
        if metadata.get("document_date_source"):
            continue
        key_facts = document.key_facts if isinstance(document.key_facts, dict) else {}
        try:
            ki_date = datetime.date.fromisoformat(
                str(key_facts.get("document_date") or "").strip()
            )
        except ValueError:
            continue
        if ki_date != document.document_date:
            continue
        document.metadata = {**metadata, "document_date_source": _AI_SOURCE}
        document.save(update_fields=["metadata"])


def drop_document_date_source(apps, schema_editor):
    Document = apps.get_model("documents", "Document")
    for document in Document.objects.iterator():
        metadata = document.metadata if isinstance(document.metadata, dict) else {}
        if metadata.get("document_date_source") != _AI_SOURCE:
            continue
        metadata = {**metadata}
        metadata.pop("document_date_source", None)
        document.metadata = metadata
        document.save(update_fields=["metadata"])


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0034_document_long_summary_document_long_summary_ai_model_and_more"),
    ]

    operations = [
        migrations.RunPython(stamp_ai_document_dates, drop_document_date_source),
    ]
