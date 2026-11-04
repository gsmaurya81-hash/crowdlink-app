from django.db import migrations, models


def null_placeholders(apps, schema_editor):
    Variant = apps.get_model("core_api", "Variant")
    Product = apps.get_model("core_api", "Product")
    Variant.objects.filter(sku_or_barcode__startswith="MISSING-").update(
        sku_or_barcode=None, has_missing_identifier=True)
    Product.objects.filter(sku_or_barcode__startswith="MISSING-").update(
        sku_or_barcode=None, has_missing_identifier=True)


class Migration(migrations.Migration):

    dependencies = [
        ("core_api", "0022_auto_20261011_0717"),
    ]

    operations = [
        migrations.AlterField(
            model_name="variant",
            name="sku_or_barcode",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.RunPython(null_placeholders, migrations.RunPython.noop),
    ]