from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('core_api', '0019_sale_ssid')]
    operations = [
        migrations.AddField(
            model_name='sale', name='reported_flag',
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]