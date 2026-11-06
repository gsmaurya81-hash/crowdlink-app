"""
core_api/utils/line_cleanup.py

Lines (SaleDetail) that still point at a variant of ANOTHER connection.

How they appear: Sale.external_order_id is unique across the whole table, so when a store
is connected again (a replaced / re-created connection) the next sync re-stamps the existing
Sale with the NEW connection - but the lines it already had keep pointing at the OLD
connection's Variant rows. The new sync then adds its own lines for the same order lines
(SaleDetail is unique per (sale, variant)), so every item shows up twice, e.g. 10 lines for
4 items. A presale / initial load over old orders does the same to those orders.

The fix is to map every such line to the equivalent variant of the sale's current connection
(same platform id, else same SKU/barcode): drop it when the sale already has that line,
otherwise re-point it.

This module only uses the ORM through the model classes it is handed, so the same code runs
from the sync (real models) and from a data migration (historical models).
"""
from django.db.models import F


def equivalent_variant(Variant, connection_id, old_variant):
    """The variant of `connection_id` that is the same platform variant as `old_variant`:
    platform catalog id first (works for variants without a SKU), then SKU/barcode."""
    qs = Variant.objects.filter(connection_id=connection_id)
    target = None
    if old_variant.external_catalog_id:
        target = qs.filter(external_catalog_id=old_variant.external_catalog_id).first()
    if target is None and old_variant.sku_or_barcode:
        # SKU is only a fallback, and only when it points at ONE variant: products can
        # share a barcode, and a line must not be moved onto the wrong one.
        matches = list(qs.filter(sku_or_barcode=old_variant.sku_or_barcode)[:2])
        target = matches[0] if len(matches) == 1 else None
    return target


def fix_cross_connection_lines(SaleDetail, Variant, *, sale_ids=None, connection_id=None,
                               include_reported=False):
    """Repair lines whose variant belongs to a different connection than their sale.

    sale_ids          only these sales (the sync passes the order it just saved)
    connection_id     only sales currently owned by this connection
    include_reported  sales with reported_flag=True are frozen and skipped unless True

    Returns {"repointed": n, "deleted": n, "unmatched": n}. A line with no equivalent variant
    is left alone and counted as unmatched (nothing is guessed)."""
    stale = (SaleDetail.objects
             .exclude(variant__connection_id=F("sale__connection_id"))
             .select_related("sale", "variant"))
    if sale_ids is not None:
        stale = stale.filter(sale_id__in=list(sale_ids))
    if connection_id is not None:
        stale = stale.filter(sale__connection_id=connection_id)
    if not include_reported:
        stale = stale.filter(sale__reported_flag=False)

    stats = {"repointed": 0, "deleted": 0, "unmatched": 0}
    for line in list(stale):
        owner_id = line.sale.connection_id
        target = equivalent_variant(Variant, owner_id, line.variant)
        if target is None:
            stats["unmatched"] += 1
            continue
        if SaleDetail.objects.filter(sale_id=line.sale_id, variant_id=target.pk).exclude(pk=line.pk).exists():
            line.delete()
            stats["deleted"] += 1
        else:
            line.variant_id = target.pk
            line.connection_id = owner_id
            line.save(update_fields=["variant", "connection"])
            stats["repointed"] += 1
    return stats


def cleanup_sale_lines(sale):
    """Called by the sync right after it saved an order's own lines."""
    from core_api.models import SaleDetail, Variant
    return fix_cross_connection_lines(SaleDetail, Variant, sale_ids=[sale.pk])


def drop_misattributed_lines(sale, order_variant_ids):
    """Remove lines an OLD sync parked on the wrong shared-barcode variant.

    Before variants were keyed by platform id, an order line for product B could be saved on
    product A's variant because both carried the same barcode. When the order is re-read, its
    line now lands on the correct variant, which would leave the old line behind (a doubled line).

    Deliberately narrow: only on an unreported sale, only lines of this connection that are not
    Bundle- rows, only when the line's variant is NOT one of the order's own variants AND
    carries the same SKU/barcode as one of them. Everything else is left alone."""
    from core_api.models import SaleDetail, Variant
    from core_api.utils.barcodes import is_real_identifier

    if sale.reported_flag or not order_variant_ids:
        return 0
    skus = {v.sku_or_barcode for v in Variant.objects.filter(pk__in=order_variant_ids)
            if is_real_identifier(v)}
    if not skus:
        return 0
    stale = (SaleDetail.objects
             .filter(sale=sale, variant__connection_id=sale.connection_id, variant__sku_or_barcode__in=skus)
             .exclude(variant_id__in=list(order_variant_ids))
             .exclude(product_category__startswith="Bundle-"))
    n = stale.count()
    if n:
        stale.delete()
    return n
