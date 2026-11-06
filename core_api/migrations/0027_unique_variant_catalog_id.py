"""Merge duplicate Variant rows, then forbid them.

Bug: a Shopify variant with no SKU was stored as sku_or_barcode = NULL (0023), which the
(connection, sku_or_barcode) unique key does not police. Overlapping product webhooks / syncs
each inserted their own copy, and every re-processed order then attached a SaleDetail to a
different copy - so one order line showed up 2-3 times on the Sales page.

This migration, per (connection, external_catalog_id) group with more than one Variant:
  * keeps ONE variant (prefers one with a real SKU/barcode, then the oldest);
  * re-points the other copies' SaleDetail rows to it; when the sale already has a line for the
    keeper, the extra copy is a duplicate of that same order line and is deleted;
  * deletes the extra Variant rows.
Then it adds the unique constraint so this cannot happen again.
Sale reporting dates / reported_flag are untouched; the Sales page recomputes its totals itself.
"""
from django.db import migrations, models


def merge_duplicate_variants(Variant, SaleDetail):
    """Returns (variants_deleted, details_repointed, details_deleted)."""
    # '' is not a platform id; storing NULL keeps it out of the unique constraint.
    Variant.objects.filter(external_catalog_id="").update(external_catalog_id=None)

    groups = {}
    for v in (Variant.objects.exclude(external_catalog_id__isnull=True)
              .order_by("connection_id", "external_catalog_id", "created_at", "id")):
        groups.setdefault((v.connection_id, v.external_catalog_id), []).append(v)

    variants_deleted = details_repointed = details_deleted = 0
    for variants in groups.values():
        if len(variants) < 2:
            continue
        # real SKU first, then oldest (list is already oldest-first)
        keeper = sorted(variants, key=lambda v: v.sku_or_barcode is None)[0]
        for dup in variants:
            if dup.pk == keeper.pk:
                continue
            for detail in SaleDetail.objects.filter(variant_id=dup.pk):
                if SaleDetail.objects.filter(sale_id=detail.sale_id, variant_id=keeper.pk).exists():
                    detail.delete()
                    details_deleted += 1
                else:
                    detail.variant_id = keeper.pk
                    detail.save(update_fields=["variant"])
                    details_repointed += 1
            dup.delete()
            variants_deleted += 1
    return variants_deleted, details_repointed, details_deleted


def forwards(apps, schema_editor):
    merge_duplicate_variants(apps.get_model("core_api", "Variant"), apps.get_model("core_api", "SaleDetail"))


class Migration(migrations.Migration):

    dependencies = [
        ("core_api", "0026_remove_saledetail_sku_or_barcode"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="variant",
            constraint=models.UniqueConstraint(
                fields=("connection", "external_catalog_id"), name="unique_variant_per_catalog_id"),
        ),
    ]
