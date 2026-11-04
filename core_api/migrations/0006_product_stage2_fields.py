# Stage 2 SOW migration:
#   - category: CrowdLink-owned product category (CATEGORY_CHOICES); never set by sync
#   - external_product_id: parent Shopify Product GID / Square Item ID used for
#     grouping variant rows into a single Products-tab row without changing the
#     (connection, sku_or_barcode) unique identity that sync relies on

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core_api', '0005_auto_20260909_0617'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='category',
            field=models.CharField(
                blank=True,
                max_length=32,
                null=True,
                choices=[
                    ('Tee', 'Tee'),
                    ('Hoodie', 'Hoodie'),
                    ('Other Garment', 'Other Garment'),
                    ('Headwear', 'Headwear'),
                    ('Drinkware', 'Drinkware'),
                    ('Other Merch', 'Other Merch'),
                    ('Vinyl Album 1LP', 'Vinyl Album 1LP'),
                    ('Vinyl Album 2LP', 'Vinyl Album 2LP'),
                    ('Vinyl Single', 'Vinyl Single'),
                    ('CD Album', 'CD Album'),
                    ('CD Single', 'CD Single'),
                    ('Cassette Album', 'Cassette Album'),
                    ('Cassette Single', 'Cassette Single'),
                    ('Digital Album', 'Digital Album'),
                    ('Digital Single', 'Digital Single'),
                    ('Other Album', 'Other Album'),
                    ('Other Single', 'Other Single'),
                ],
            ),
        ),
        migrations.AddField(
            model_name='product',
            name='external_product_id',
            field=models.CharField(blank=True, db_index=True, max_length=255, null=True),
        ),
    ]
