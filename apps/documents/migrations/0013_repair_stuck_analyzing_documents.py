from django.db import migrations


def repair_stuck_analyzing_documents(apps, schema_editor):
    """One-off cleanup for #1035: `analyze_documents` (#1029) set

    `processing_status="analyzing"` before running the KI-Analyse but
    never moved it on afterwards, so every document it touched got
    stuck showing "Analyse läuft" regardless of outcome. The command is
    fixed to finalize the status itself going forward; this repairs the
    documents it already left behind, using the same success/failure
    split (`metadata.analysis_error` present or not) the fixed command
    now applies.
    """
    Document = apps.get_model("documents", "Document")
    stuck = Document.objects.filter(processing_status="analyzing")
    stuck.filter(metadata__has_key="analysis_error").update(processing_status="failed")
    stuck.exclude(metadata__has_key="analysis_error").update(processing_status="ready")


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0012_repair_dimension_prefixed_tag_names"),
    ]

    operations = [
        migrations.RunPython(repair_stuck_analyzing_documents, migrations.RunPython.noop),
    ]
