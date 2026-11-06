"""Ex-tax product / variant prices (client item 16)."""
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from core_api.models import ClientStoreConnection, Product, Variant
from core_api.utils.product_sync import sync_upsert_product, to_price
from shopify_integration.services import shopify_catalog
from square_integration.services.square_catalog import _images, variation_price_ex_tax


def _conn():
    return ClientStoreConnection.objects.create(
        client_email="c@x.com", platform="SQUARE", store_identifier="M1", access_token="t")


def _up(conn, sku, price, pid="P1", cat="V" ):
    return sync_upsert_product(conn, sku, {
        "title": "Tee", "external_product_id": pid, "external_catalog_id": f"{cat}-{sku}",
        "variant_title": sku, "price": price})


class StorePrices(TestCase):
    def test_variant_and_product_price_stored(self):
        c = _conn(); _up(c, "A", 12.5)
        self.assertEqual(Variant.objects.get(sku_or_barcode="A").price, Decimal("12.50"))
        self.assertEqual(Product.objects.get().price, Decimal("12.50"))

    def test_product_price_is_lowest_variant_price(self):
        c = _conn(); _up(c, "A", 30); _up(c, "B", 20); _up(c, "C", 25)
        self.assertEqual(Product.objects.get().price, Decimal("20.00"))

    def test_price_updates_on_next_sync(self):
        c = _conn(); _up(c, "A", 10); _up(c, "A", 12)
        self.assertEqual(Variant.objects.get().price, Decimal("12.00"))
        self.assertEqual(Product.objects.get().price, Decimal("12.00"))

    def test_unknown_price_does_not_wipe_stored_price(self):
        c = _conn(); _up(c, "A", 10); _up(c, "A", None)
        self.assertEqual(Variant.objects.get().price, Decimal("10.00"))

    def test_to_price(self):
        self.assertEqual(to_price("19.999"), Decimal("20.00"))
        self.assertIsNone(to_price(None)); self.assertIsNone(to_price("abc"))


class SquareExTax(TestCase):
    def _taxes(self, inclusive):
        return _images([{"id": "T1", "type": "TAX", "tax_data": {
            "percentage": "10.0", "inclusion_type": "INCLUSIVE" if inclusive else "ADDITIVE"}}])

    def test_no_tax_price_as_listed(self):
        self.assertEqual(variation_price_ex_tax({}, {"price_money": {"amount": 1100}}, {}), 11.0)

    def test_additive_tax_price_is_already_ex_tax(self):
        self.assertEqual(variation_price_ex_tax(
            {"tax_ids": ["T1"]}, {"price_money": {"amount": 1100}}, self._taxes(False)), 11.0)

    def test_inclusive_tax_is_divided_out(self):
        self.assertEqual(variation_price_ex_tax(
            {"tax_ids": ["T1"]}, {"price_money": {"amount": 1100}}, self._taxes(True)), 10.0)

    def test_unloaded_tax_object_gives_none(self):
        self.assertIsNone(variation_price_ex_tax({"tax_ids": ["T9"]}, {"price_money": {"amount": 1100}}, {}))

    def test_variable_pricing_gives_none(self):
        self.assertIsNone(variation_price_ex_tax({}, {"pricing_type": "VARIABLE_PRICING"}, {}))


class ShopifyExTax(TestCase):
    def test_tax_inclusive_shop_divides_out_gst(self):
        with mock.patch.object(shopify_catalog, "shop_taxes_included", return_value=True):
            self.assertEqual(shopify_catalog.variant_price_ex_tax(None, "11.00", True), 10.0)

    def test_non_taxable_variant_unchanged(self):
        with mock.patch.object(shopify_catalog, "shop_taxes_included", return_value=True):
            self.assertEqual(shopify_catalog.variant_price_ex_tax(None, "11.00", False), 11.0)

    def test_tax_exclusive_shop_unchanged(self):
        with mock.patch.object(shopify_catalog, "shop_taxes_included", return_value=False):
            self.assertEqual(shopify_catalog.variant_price_ex_tax(None, "11.00", True), 11.0)

    def test_unknown_setting_gives_none(self):
        with mock.patch.object(shopify_catalog, "shop_taxes_included", return_value=None):
            self.assertIsNone(shopify_catalog.variant_price_ex_tax(None, "11.00", True))


class BackfillCommand(TestCase):
    def test_backfill_fills_existing_variant_only(self):
        from io import StringIO
        from django.core.management import call_command
        c = _conn(); _up(c, "A", None)
        self.assertIsNone(Variant.objects.get().price)
        feed = [("V-A", 9.99), ("V-ZZ", 5.0), ("V-A2", None)]
        with mock.patch("core_api.management.commands.backfill_prices._square_prices", return_value=iter(feed)):
            call_command("backfill_prices", "--apply", stdout=StringIO())
        self.assertEqual(Variant.objects.get().price, Decimal("9.99"))
        self.assertEqual(Product.objects.get().price, Decimal("9.99"))
        self.assertEqual(Variant.objects.count(), 1)
