"""aria_reporting_date: DateTimeField -> DateField (same type as clau_reporting_date).

It was a datetime that held either a UTC timestamp or a date written as UTC
midnight, so a morning sale in Sydney could read as the previous day. It now
holds the plain AEST/AEDT calendar date.

Step 1 normalises existing values: a value at exactly 00:00:00 is already a
date (written from a reporting date) and is kept; anything else is a real UTC
timestamp and is converted to its Sydney calendar date.
Step 2 changes the column type.
Reported (frozen) rows keep whatever date they now hold; unreported rows are
re-derived from the sale on the next sync anyway.
"""
import datetime

from django.db import migrations, models

try:
    from zoneinfo import ZoneInfo
    SYDNEY = ZoneInfo("Australia/Sydney")
except Exception:  # pragma: no cover
    SYDNEY = None


def to_local_date(value):
    if value is None:
        return None
    if value.time() == datetime.time(0, 0):
        return value.date()
    if SYDNEY is None:
        return value.date()
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(SYDNEY).date()


def normalise(apps, schema_editor):
    for name in ("Sale", "SaleDetail"):
        Model = apps.get_model("core_api", name)
        for row in Model.objects.exclude(aria_reporting_date__isnull=True).only("pk", "aria_reporting_date").iterator():
            d = to_local_date(row.aria_reporting_date)
            new = datetime.datetime.combine(d, datetime.time.min, tzinfo=datetime.timezone.utc)
            if new != row.aria_reporting_date:
                Model.objects.filter(pk=row.pk).update(aria_reporting_date=new)


class Migration(migrations.Migration):

    dependencies = [
        ("core_api", "0028_fold_stale_connection_lines"),
    ]

    operations = [
        migrations.RunPython(normalise, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="sale",
            name="aria_reporting_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="saledetail",
            name="aria_reporting_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
