"""
core_api/debug_views.py

READ-ONLY diagnostic endpoints. These never create, update, or delete
any row - they only query and return JSON. Safe to open in a browser
as many times as you like.

Does not touch product_sync.py, shopify_catalog.py, or square_catalog.py,
so it has zero effect on the live sync behaviour (new-connection sync,
daily sync, manual sync, presale backfill, post-sync field refresh).
"""
from collections import defaultdict

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from core_api.models import ClientStoreConnection, SaleDetail, Variant


@csrf_exempt
def find_duplicate_products(request):
    """
    GET /api/debug/duplicate-products/?email=<client email>[&order=<order id>]

    Reports Variant/Product rows that share the same SKU across more
    than one Product row - the cause of a single order line appearing
    more than once on the Sales tab. Read-only.
    """
    email = request.GET.get("email")
    if not email:
        return JsonResponse({"error": "Pass ?email=<client email> in the URL"}, status=400)
    order_id = request.GET.get("order")

    connections = ClientStoreConnection.objects.filter(client_email=email)
    if not connections.exists():
        return JsonResponse({"error": f"No connection found for {email}"}, status=404)

    result = {"email": email}

    if order_id:
        details = (
            SaleDetail.objects.filter(connection__in=connections, external_order_id=order_id)
            .select_related("sale", "variant", "variant__product")
        )
        order_rows = []
        for d in details:
            p = d.variant.product if d.variant else None
            order_rows.append({
                "sale_detail_id": str(d.id),
                "variant_id": str(d.variant_id) if d.variant_id else None,
                "variant_sku": d.variant.sku_or_barcode if d.variant else None,
                "product_id": str(p.id) if p else None,
                "product_title": p.title if p else None,
                "product_category": p.category if p else None,
                "product_sku": p.sku_or_barcode if p else None,
            })
        result["order_id"] = order_id
        result["order_row_count"] = len(order_rows)
        result["order_rows"] = order_rows

    # Group variants by normalized SKU (trim + lowercase) to find duplicates.
    by_norm_sku = defaultdict(list)
    for v in Variant.objects.filter(connection__in=connections).select_related("product"):
        key = (v.sku_or_barcode or "").strip().lower()
        by_norm_sku[key].append(v)

    dupes = []
    for sku, variants in by_norm_sku.items():
        if not sku or len(variants) <= 1:
            continue
        product_ids = {v.product_id for v in variants}
        dupes.append({
            "sku": sku,
            "variant_count": len(variants),
            "distinct_product_count": len(product_ids),
            "variants": [
                {
                    "variant_id": str(v.id),
                    "sku_or_barcode": v.sku_or_barcode,
                    "product_id": str(v.product_id),
                    "product_title": v.product.title,
                    "product_category": v.product.category,
                    "product_release_date": v.product.release_date.isoformat() if v.product.release_date else None,
                    "created_at": v.created_at.isoformat() if v.created_at else None,
                }
                for v in variants
            ],
        })

    result["duplicate_sku_count"] = len(dupes)
    result["duplicate_skus"] = dupes

    return JsonResponse(result)