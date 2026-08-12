# Generated for #1113 -- private ESt-Absetzbarkeit (Ja/Nein/Vielleicht).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('documents', '0030_correspondent_is_own_business_document_sphere'),
    ]

    operations = [
        migrations.AddField(
            model_name='document',
            name='tax_relevance',
            field=models.CharField(
                choices=[
                    ('ja', 'Ja'),
                    ('nein', 'Nein'),
                    ('vielleicht', 'Vielleicht'),
                    ('nicht_zutreffend', 'Nicht zutreffend'),
                    ('unbekannt', 'Unbekannt'),
                ],
                db_index=True,
                default='unbekannt',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='document',
            name='tax_relevance_reason',
            field=models.TextField(blank=True),
        ),
    ]
