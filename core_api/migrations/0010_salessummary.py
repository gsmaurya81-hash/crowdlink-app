import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core_api', '0009_auto_20260921_0603'),
    ]

    operations = [
        migrations.CreateModel(
            name='SalesSummary',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('summary_date', models.DateField(db_index=True)),
                ('sale_channel', models.CharField(choices=[('POS', 'POS'), ('WEBSTORE', 'Webstore')], max_length=20)),
                ('audience_size', models.IntegerField(blank=True, null=True)),
                ('notes', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('assigned_location', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='core_api.saleslocation')),
                ('connection', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='core_api.clientstoreconnection')),
            ],
        ),
        migrations.AddConstraint(
            model_name='salessummary',
            constraint=models.UniqueConstraint(fields=('connection', 'summary_date', 'sale_channel'), name='unique_sales_summary_per_day_channel'),
        ),
    ]