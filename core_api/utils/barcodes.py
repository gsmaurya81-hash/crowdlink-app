"""
core_api/utils/barcodes.py

Shared barcodes. CrowdLink keys a Variant by the platform's own variant id
(Variant.external_catalog_id: Shopify ProductVariant gid / Square variation id),
NOT by its SKU/barcode. Clients cannot be stopped from typing the same barcode
on two products (bundles, re-listed or renamed products), so two products may
legitimately hold variants with the same sku_or_barcode, each with its own sales.

Two things follow:
  * a SKU/barcode may only be used to find a variant when it is unambiguous
    (unique_variant_by_sku) - never to pick "the first of several";
  * the client is warned on the Products tab (duplicate_barcode_info) so they can
    give the products distinct barcodes or set one up as a Define Bundle.
"""
from collections import defaultdict

from core_api.models import Product, Variant

BUNDLE_CATEGORY = "Define Bundle"


def normalize_barcode(value):
    """Comparison key: surrounding spaces and letter case are not a difference."""
    return (value or "").strip().casefold()


def is_real_identifier(variant):
    """False for variants with no SKU/barcode (stored as NULL, or an old MISSING-... placeholder)."""
    sku = (variant.sku_or_barcode or "").strip()
    return bool(sku) and not variant.has_missing_identifier and not sku.upper().startswith("MISSING")


def unique_variant_by_sku(connection, sku, queryset=None):
    """The connection's variant carrying this SKU/barcode, but ONLY when exactly one does.
    Zero or several matches -> None (the caller treats the line as unmatched rather than
    guessing which of the shared-barcode products the sale belongs to)."""
    sku = (sku or "").strip()
    if not sku:
        return None
    qs = queryset if queryset is not None else Variant.objects.filter(connection=connection)
    found = list(qs.filter(connection=connection, sku_or_barcode=sku).select_related("product")[:2])
    return found[0] if len(found) == 1 else None


def duplicate_barcode_info(connection):
    """{product_id: {"barcodes": [...], "with": [{"id", "title"}, ...]}} for every product that
    shares a barcode with ANOTHER product of the same connection.

    * Variants without a real SKU/barcode are ignored (that is the "missing" warning).
    * Products set up as "Define Bundle" are ignored: a bundle is the supported way for a
      product to reuse a barcode, so setting one up clears the warning.
    * Several variants of ONE product sharing a barcode is not flagged (no cross-product
      sales mix-up is possible)."""
    rows = (Variant.objects.filter(connection=connection)
            .exclude(product__category=BUNDLE_CATEGORY)
            .select_related("product"))
    by_code = defaultdict(lambda: {"display": None, "products": {}})
    for v in rows:
        if not is_real_identifier(v):
            continue
        entry = by_code[normalize_barcode(v.sku_or_barcode)]
        entry["display"] = entry["display"] or v.sku_or_barcode.strip()
        entry["products"][v.product_id] = v.product.title

    info = {}
    for entry in by_code.values():
        products = entry["products"]
        if len(products) < 2:
            continue
        for pid in products:
            item = info.setdefault(pid, {"barcodes": [], "with": {}})
            item["barcodes"].append(entry["display"])
            for other_id, other_title in products.items():
                if other_id != pid:
                    item["with"][other_id] = other_title
    return {
        pid: {"barcodes": sorted(set(item["barcodes"])),
              "with": [{"id": str(i), "title": t} for i, t in item["with"].items()]}
        for pid, item in info.items()
    }


def duplicate_barcode_fields(info, product):
    """The extra keys added to each product in the Products API payload."""
    item = info.get(product.id)
    return {
        "duplicate_barcode": item is not None,
        "duplicate_barcodes": item["barcodes"] if item else [],
        "duplicate_barcode_with": item["with"] if item else [],
    }
