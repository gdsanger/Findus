from django.db import migrations


class Migration(migrations.Migration):
    """Drop the orphaned `address` column on `documents_correspondent`.

    The column exists only in the physical database -- it was never part of
    any Django migration, model, or form (verified across the full history
    of every branch). It is a leftover from an earlier, hand-built schema
    that predates `0002_initial` (which defines Correspondent with just
    `name` + `email`). Because Django doesn't know the column exists, every
    INSERT omits it; the column is NOT NULL with no default, so Postgres
    rejects the row with an IntegrityError -- meaning *no* Correspondent
    could be created at all.

    Nothing in the codebase reads or writes a Correspondent address, so we
    reconcile the DB to the model by dropping the column. `IF EXISTS` keeps
    this a no-op on databases (e.g. freshly-migrated ones) that never had
    the drifted column. No `state_operations` are needed: the Django model
    state already lacks the field, so this only touches the database.
    """

    dependencies = [
        ("documents", "0014_tasktemplate_tasktemplateitem"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE documents_correspondent DROP COLUMN IF EXISTS address;",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
