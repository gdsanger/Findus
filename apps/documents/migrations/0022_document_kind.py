from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('documents', '0021_document_child_role_document_parent_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='document',
            name='kind',
            field=models.CharField(
                choices=[('document', 'Dokument'), ('mail_body', 'Mail-Text')],
                db_index=True,
                default='document',
                max_length=20,
            ),
        ),
    ]
