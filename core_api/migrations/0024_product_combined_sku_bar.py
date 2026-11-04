from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core_api', '0023_variant_sku_nullable'),
    ]

    operations = [
        # Keeps existing data: the column is renamed, not dropped/re-added.
        migrations.RenameField(
            model_name='product',
            old_name='sku_or_barcode',
            new_name='combined_sku_bar',
        ),
        # A product's variants are combined into one value, so 100 chars is not enough.
        migrations.AlterField(
            model_name='product',
            name='combined_sku_bar',
            field=models.TextField(blank=True, null=True),
        ),
    ]