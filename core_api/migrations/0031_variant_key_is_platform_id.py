"""A Variant is one row per platform variant id, not per SKU/barcode.

Drops unique(connection, sku_or_barcode) so two products can share a barcode (bundles,
re-listed / renamed products) and each keeps its own variant and sales. The platform-id
constraint from 0027 stays. A plain index keeps SKU look-ups fast.

No data is changed. (The new index is added BEFORE the unique key is dropped so MySQL
always has an index backing the connection foreign key.)
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core_api", "0030_product_variant_price"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="variant",
            index=models.Index(fields=["connection", "sku_or_barcode"], name="variant_conn_sku_idx"),
        ),
        migrations.AlterUniqueTogether(
            name="variant",
            unique_together=set(),
        ),
    ]
