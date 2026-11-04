from django.db import migrations, models


class Migration(migrations.Migration):
    """Adds Sale.ssid (client spec: "refresh sales.SSID" after every sync).
    Nullable + no data migration: existing rows are filled by the first sync /
    Sales-tab load after deploy (core_api/utils/post_sync.py)."""

    dependencies = [
        ('core_api', '0018_auto_20260929_0655'),
    ]

    operations = [
        migrations.AddField(
            model_name='sale',
            name='ssid',
            field=models.CharField(blank=True, db_index=True, max_length=150, null=True),
        ),
    ]
