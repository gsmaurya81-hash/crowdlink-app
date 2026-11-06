"""
One-off: fill Variant.price / Product.price (ex-tax) for products that were synced before
the price fields existed. Normal syncs only re-read products changed on the platform, so
untouched products would otherwise stay NULL.

  python manage.py backfill_prices                       # dry run, all active connections
  python manage.py backfill_prices --apply               # write the prices
  python manage.py backfill_prices --email a@b.com --apply

Only EXISTING variants are updated (matched by platform id); nothing new is created, and a
variant whose price cannot be determined (variable pricing, unknown tax setting) is skipped.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Min

from core_api.models import ClientStoreConnection, Product, Variant
from core_api.utils.product_sync import to_price


def _square_prices(connection):
    from square_integration.services.square_catalog import fetch_all_items, variation_price_ex_tax
    from square_integration.services.square_oauth import get_valid_access_token
    items, images = fetch_all_items(get_valid_access_token(connection))
    for item in items:
        item_data = item.get("item_data") or {}
        for var in item_data.get("variations", []):
            yield var.get("id"), variation_price_ex_tax(item_data, var.get("item_variation_data") or {}, images)


def _shopify_prices(connection):
    from shopify_integration.services.shopify_catalog import fetch_products, variant_price_ex_tax
    from shopify_integration.services.shopify_oauth import get_valid_access_token
    token = get_valid_access_token(connection)
    for _prod, var in fetch_products(connection, token, ""):
        yield var.get("id"), variant_price_ex_tax(connection, var.get("price"), var.get("taxable"), token)


class Command(BaseCommand):
    help = "Fill ex-tax Variant/Product prices for already-synced products (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--email", help="Only this client_email")

    def handle(self, *args, **opts):
        qs = ClientStoreConnection.objects.filter(is_active=True)
        if opts["email"]:
            qs = qs.filter(client_email__iexact=opts["email"])
        for conn in qs.order_by("platform", "client_email"):
            source = _square_prices if conn.platform == "SQUARE" else _shopify_prices
            changed = skipped = 0
            touched_products = set()
            try:
                for catalog_id, price in source(conn):
                    price = to_price(price)
                    if price is None or not catalog_id:
                        skipped += 1
                        continue
                    variant = Variant.objects.filter(connection=conn, external_catalog_id=catalog_id).first()
                    if variant is None:
                        continue
                    if variant.price != price:
                        changed += 1
                        touched_products.add(variant.product_id)
                        if opts["apply"]:
                            variant.price = price
                            variant.save(update_fields=["price"])
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"{conn.platform} {conn.client_email}: failed - {exc}"))
                continue
            if opts["apply"]:
                for product in Product.objects.filter(pk__in=touched_products):
                    low = product.variants.aggregate(low=Min("price"))["low"]
                    if low is not None and product.price != low:
                        product.price = low
                        product.save(update_fields=["price"])
            mode = "updated" if opts["apply"] else "would update"
            self.stdout.write(f"{conn.platform} {conn.client_email} ({str(conn.pk)[:8]}): "
                              f"{mode} {changed} variant price(s), skipped {skipped} (price unknown)")
        if not opts["apply"]:
            self.stdout.write("Dry run only - re-run with --apply to write the prices.")
