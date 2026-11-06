"""Ex-tax price on Product (lowest variant price) and Variant."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core_api", "0029_aria_reporting_date_to_date"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="price",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="variant",
            name="price",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
    ]
