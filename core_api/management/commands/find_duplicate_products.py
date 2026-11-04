"""
core_api/management/commands/find_duplicate_products.py

READ-ONLY diagnostic. Does not change any data.

For a given client email (and optional order id), finds Product/Variant
rows that look like duplicates of "the same real product" - same
connection + same sku_or_barcode on the Variant, but pointing at more
than one Product row (or the same Product row having its
sku_or_barcode duplicated under more than one Variant row).

Usage:
    python manage.py find_duplicate_products --email client@example.com
    python manage.py find_duplicate_products --email client@example.com --order 7347781402941
"""
from collections import defaultdict

from django.core.management.base import BaseCommand

from core_api.models import ClientStoreConnection, SaleDetail, Variant


class Command(BaseCommand):
    help = "Read-only: list duplicate Product/Variant rows sharing a SKU."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--order", default=None, help="Optional Shopify/Square order id to narrow the report")

    def handle(self, *args, **opts):
        email = opts["email"]
        order_id = opts["order"]

        connections = ClientStoreConnection.objects.filter(client_email=email)
        if not connections.exists():
            self.stdout.write(self.style.ERROR(f"No connection found for {email}"))
            return

        if order_id:
            details = (
                SaleDetail.objects.filter(connection__in=connections, external_order_id=order_id)
                .select_related("sale", "variant", "variant__product")
            )
            self.stdout.write(f"Order {order_id}: {details.count()} SaleDetail row(s)\n")
            for d in details:
                p = d.variant.product if d.variant else None
                self.stdout.write(
                    f"  SaleDetail={d.id}  variant={d.variant_id}  "
                    f"variant.sku={d.variant.sku_or_barcode if d.variant else None!r}  "
                    f"product={p.id if p else None}  product.title={p.title if p else None!r}  "
                    f"product.category={p.category if p else None!r}  "
                    f"product.sku={p.sku_or_barcode if p else None!r}"
                )
            self.stdout.write("")

        # Group variants by normalized SKU to spot near-duplicates
        # (case / whitespace differences that the DB constraint wouldn't catch
        # because they technically compare unequal).
        by_norm_sku = defaultdict(list)
        for v in Variant.objects.filter(connection__in=connections).select_related("product"):
            key = (v.sku_or_barcode or "").strip().lower()
            by_norm_sku[key].append(v)

        dupes = {k: v for k, v in by_norm_sku.items() if k and len(v) > 1}
        if not dupes:
            self.stdout.write(self.style.SUCCESS("No duplicate SKUs found across Variant rows for this client."))
            return

        self.stdout.write(self.style.WARNING(f"Found {len(dupes)} SKU(s) with more than one Variant row:\n"))
        for sku, variants in dupes.items():
            product_ids = {v.product_id for v in variants}
            self.stdout.write(f"SKU '{sku}' -> {len(variants)} variant row(s), {len(product_ids)} distinct product row(s)")
            for v in variants:
                p = v.product
                self.stdout.write(
                    f"    variant={v.id}  sku={v.sku_or_barcode!r}  product={p.id}  "
                    f"title={p.title!r}  category={p.category!r}  release_date={p.release_date}  "
                    f"created_at={v.created_at}"
                )
            self.stdout.write("")