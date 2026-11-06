"""Fold sale lines that still point at another connection's variants.

After a store was connected again, a sale's old lines kept the OLD connection's variants and the
new sync added its own lines next to them (one order showing 10 lines for 4 items). For every
sale that is not yet reported, each such line is mapped to the equivalent variant of the sale's
current connection (platform id, else SKU/barcode): deleted when the sale already has that line,
otherwise re-pointed. Lines with no equivalent are left alone. Reported sales are never touched.

Data only, no schema change. Safe to run more than once. Not reversible (deleted rows are exact
duplicates of lines that stay), so take a database backup before deploying.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    from core_api.utils.line_cleanup import fix_cross_connection_lines
    fix_cross_connection_lines(apps.get_model("core_api", "SaleDetail"), apps.get_model("core_api", "Variant"))


class Migration(migrations.Migration):

    dependencies = [
        ("core_api", "0027_unique_variant_catalog_id"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
