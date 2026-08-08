# Generated for #1069/#1070: Dokument-Hierarchie (parent/child_role) + kind.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('documents', '0020_merge_20260803_2135'),
    ]

    operations = [
        migrations.AddField(
            model_name='document',
            name='kind',
            field=models.CharField(
                choices=[('document', 'Dokument'), ('mail_body', 'Mail-Body')],
                db_index=True,
                default='document',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='document',
            name='child_role',
            field=models.CharField(
                blank=True,
                choices=[('mail_attachment', 'Mail-Anhang')],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='document',
            name='parent',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='children',
                to='documents.document',
            ),
        ),
    ]
