from django.db import migrations


class Migration(migrations.Migration):
    """No-op: superseded by `0015_correspondent_address_vorgang_department_and_more`.

    This migration originally dropped `documents_correspondent.address` as
    an orphaned, unmodeled column. That premise was invalidated the moment
    the sibling `0015_correspondent_address_vorgang_department_and_more`
    (added independently, on another branch, with the same parent) turned
    `address` into a real modeled field with a form (`forms.py`) reading and
    writing it. Because that migration has no dependency on this one (both
    branch off `0014` in parallel), a fresh `migrate` could apply this one
    *after* the add -- silently dropping the column while the Django model
    state and `models.py` still declared it present. That is exactly what
    happened in production (#1055): `GET /` started raising
    `column documents_correspondent.address does not exist`.

    Kept as a no-op rather than deleted so environments that already have
    this migration's name recorded in `django_migrations` don't hit a
    missing-migration inconsistency; `0017_merge_20260803_1901` resolves
    the resulting two-leaf conflict.
    """

    dependencies = [
        ("documents", "0014_tasktemplate_tasktemplateitem"),
    ]

    operations = [
        migrations.RunSQL(
            sql=migrations.RunSQL.noop,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
