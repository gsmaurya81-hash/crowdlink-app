"""Shared barcodes (20/21): a Variant is keyed by its platform id, never by its barcode.

Two products that carry the same barcode must each keep their own variant and their own
sales, and the Products API must warn about it."""
import json
from decimal import Decimal
from unittest import mock

from django.test import RequestFactory, TestCase
from django.utils import timezone

from core_api.models import ClientStoreConnection, Product, Sale, SaleDetail, Variant
from core_api.utils.barcodes import duplicate_barcode_info, unique_variant_by_sku
from core_api.utils.line_cleanup import drop_misattributed_lines, equivalent_variant
from core_api.utils.product_sync import sync_upsert_product

BARCODE = "9300000000017"


def _upsert(conn, n, sku=BARCODE, title=None, **kw):
    """Sync product #n with one variant, the way both engines call it."""
    d = {"title": title or f"Product {n}", "variant_title": "Default",
         "has_missing_identifier": not sku,
         "external_catalog_id": f"var-{n}", "external_product_id": f"prod-{n}"}
    d.update(kw)
    return sync_upsert_product(conn, sku or f"MISSING-{n}", d)[0]


class SharedBarcodeSyncTests(TestCase):
    def setUp(self):
        self.c = ClientStoreConnection.objects.create(
            client_email="a@b.com", platform="SHOPIFY", store_identifier="s.myshopify.com", access_token="t")

    def test_two_products_with_one_barcode_keep_their_own_variant(self):
        a, b = _upsert(self.c, 1), _upsert(self.c, 2)
        self.assertEqual(Variant.objects.filter(connection=self.c).count(), 2)
        self.assertEqual(a.variants.get().external_catalog_id, "var-1")
        self.assertEqual(b.variants.get().external_catalog_id, "var-2")
        self.assertEqual(a.variants.get().sku_or_barcode, BARCODE)
        self.assertEqual(b.variants.get().sku_or_barcode, BARCODE)

    def test_resync_in_either_order_does_not_move_variants_between_products(self):
        a, b = _upsert(self.c, 1), _upsert(self.c, 2)
        for _ in range(2):
            _upsert(self.c, 2)
            _upsert(self.c, 1)
        self.assertEqual(Variant.objects.get(external_catalog_id="var-1").product_id, a.pk)
        self.assertEqual(Variant.objects.get(external_catalog_id="var-2").product_id, b.pk)
        self.assertEqual(Variant.objects.count(), 2)

    def test_both_products_still_show_the_barcode(self):
        a, b = _upsert(self.c, 1), _upsert(self.c, 2)
        a.refresh_from_db(), b.refresh_from_db()
        self.assertEqual(a.combined_sku_bar, BARCODE)
        self.assertEqual(b.combined_sku_bar, BARCODE)
        self.assertFalse(a.has_missing_identifier or b.has_missing_identifier)

    def test_changing_the_barcode_on_one_product_updates_only_its_variant(self):
        _upsert(self.c, 1), _upsert(self.c, 2)
        _upsert(self.c, 2, sku="9300000000024")
        self.assertEqual(Variant.objects.get(external_catalog_id="var-1").sku_or_barcode, BARCODE)
        self.assertEqual(Variant.objects.get(external_catalog_id="var-2").sku_or_barcode, "9300000000024")

    def test_legacy_variant_without_platform_id_is_adopted_by_its_own_product_only(self):
        p = Product.objects.create(connection=self.c, external_product_id="prod-1", title="Product 1")
        old = Variant.objects.create(connection=self.c, product=p, sku_or_barcode=BARCODE, external_catalog_id=None)
        _upsert(self.c, 1)
        old.refresh_from_db()
        self.assertEqual(old.external_catalog_id, "var-1")
        self.assertEqual(Variant.objects.count(), 1)
        # another product with the same barcode must NOT take it over
        _upsert(self.c, 2)
        self.assertEqual(Variant.objects.count(), 2)
        old.refresh_from_db()
        self.assertEqual(old.product_id, p.pk)

    def test_variants_of_two_connections_may_share_a_barcode(self):
        other = ClientStoreConnection.objects.create(
            client_email="c@d.com", platform="SQUARE", store_identifier="m1", access_token="t")
        _upsert(self.c, 1), _upsert(other, 1)
        self.assertEqual(Variant.objects.filter(sku_or_barcode=BARCODE).count(), 2)


class SkuFallbackTests(TestCase):
    def setUp(self):
        self.c = ClientStoreConnection.objects.create(
            client_email="a@b.com", platform="SHOPIFY", store_identifier="s.myshopify.com", access_token="t")

    def test_unique_sku_is_found(self):
        _upsert(self.c, 1)
        self.assertEqual(unique_variant_by_sku(self.c, BARCODE).external_catalog_id, "var-1")

    def test_shared_sku_is_never_guessed(self):
        _upsert(self.c, 1), _upsert(self.c, 2)
        self.assertIsNone(unique_variant_by_sku(self.c, BARCODE))

    def test_unknown_or_blank_sku(self):
        _upsert(self.c, 1)
        self.assertIsNone(unique_variant_by_sku(self.c, "nope"))
        self.assertIsNone(unique_variant_by_sku(self.c, ""))
        self.assertIsNone(unique_variant_by_sku(self.c, None))

    def test_reconnect_repair_does_not_guess_between_shared_barcode_variants(self):
        old = ClientStoreConnection.objects.create(
            client_email="a@b.com", platform="SHOPIFY", store_identifier="old.myshopify.com", access_token="t")
        _upsert(self.c, 1), _upsert(self.c, 2)
        # old-connection variant with a platform id the new connection does not have
        pv = Product.objects.create(connection=old, external_product_id="old-p", title="Old")
        ov = Variant.objects.create(connection=old, product=pv, sku_or_barcode=BARCODE, external_catalog_id="old-v")
        self.assertIsNone(equivalent_variant(Variant, self.c.pk, ov))
        # but the platform id still wins when it exists
        ov2 = Variant.objects.create(connection=old, product=pv, sku_or_barcode=BARCODE, external_catalog_id="var-2")
        self.assertEqual(equivalent_variant(Variant, self.c.pk, ov2).external_catalog_id, "var-2")


class SalesStayOnTheirOwnProductTests(TestCase):
    def setUp(self):
        self.c = ClientStoreConnection.objects.create(
            client_email="a@b.com", platform="SHOPIFY", store_identifier="s.myshopify.com", access_token="t")
        self.a, self.b = _upsert(self.c, 1), _upsert(self.c, 2)
        self.va, self.vb = self.a.variants.get(), self.b.variants.get()

    def _sale(self, oid, reported=False):
        return Sale.objects.create(connection=self.c, external_order_id=oid, sale_date=timezone.now(),
                                   system_location="Shopify POS", payment_type="CASH",
                                   source_platform="Shopify", reported_flag=reported)

    def _line(self, sale, variant, units=1, category=None):
        return SaleDetail.objects.create(connection=self.c, sale=sale, variant=variant,
                                         external_order_id=sale.external_order_id, sales_units=units,
                                         product_title=variant.product.title, product_category=category)

    def _order(self, oid, items):
        return {"id": oid, "created_at": timezone.now().isoformat(), "source_name": "pos",
                "payment_gateway_names": ["cash"], "line_items": items}

    def test_orders_land_on_the_product_whose_variant_id_they_name(self):
        from shopify_integration.services.shopify_catalog import process_orders
        orders = [self._order("o1", [{"variant_id": "1", "sku": BARCODE, "quantity": 2, "price": "5.00"}]),
                  self._order("o2", [{"variant_id": "2", "sku": BARCODE, "quantity": 3, "price": "7.00"}])]
        # Variant ids in the DB are "gid://shopify/ProductVariant/<id>"
        Variant.objects.filter(pk=self.va.pk).update(external_catalog_id="gid://shopify/ProductVariant/1")
        Variant.objects.filter(pk=self.vb.pk).update(external_catalog_id="gid://shopify/ProductVariant/2")
        process_orders(self.c, orders, timezone.now())
        units = {d.variant_id: d.sales_units for d in SaleDetail.objects.all()}
        self.assertEqual(units, {self.va.pk: 2, self.vb.pk: 3})

    def test_line_without_variant_id_and_a_shared_sku_is_skipped_not_guessed(self):
        from shopify_integration.services.shopify_catalog import process_orders
        orders = [self._order("o3", [{"variant_id": None, "sku": BARCODE, "quantity": 1, "price": "5.00"}])]
        saved, skipped = process_orders(self.c, orders, timezone.now())
        self.assertEqual((saved, skipped), (0, 1))
        self.assertEqual(SaleDetail.objects.count(), 0)

    def test_line_without_variant_id_and_a_unique_sku_still_matches(self):
        from shopify_integration.services.shopify_catalog import process_orders
        Variant.objects.filter(pk=self.vb.pk).update(sku_or_barcode="UNIQUE-2")
        orders = [self._order("o4", [{"variant_id": None, "sku": "UNIQUE-2", "quantity": 1, "price": "5.00"}])]
        saved, _ = process_orders(self.c, orders, timezone.now())
        self.assertEqual(saved, 1)
        self.assertEqual(SaleDetail.objects.get().variant_id, self.vb.pk)

    def test_old_misattributed_line_is_dropped_when_the_order_is_re_read(self):
        # an old sync parked product B's order line on product A's variant (shared barcode)
        sale = self._sale("o5")
        self._line(sale, self.va, units=2)       # wrong: parked on A
        self._line(sale, self.vb, units=2)       # what this re-read just wrote (correct, B)
        n = drop_misattributed_lines(sale, [self.vb.pk])
        self.assertEqual(n, 1)
        self.assertEqual(list(sale.details.values_list("variant_id", flat=True)), [self.vb.pk])

    def test_misattributed_line_cleanup_leaves_reported_sales_alone(self):
        sale = self._sale("o6", reported=True)
        self._line(sale, self.va), self._line(sale, self.vb)
        self.assertEqual(drop_misattributed_lines(sale, [self.vb.pk]), 0)
        self.assertEqual(sale.details.count(), 2)

    def test_misattributed_cleanup_keeps_genuine_lines_and_bundle_rows(self):
        other = _upsert(self.c, 3, sku="OTHER-SKU")
        sale = self._sale("o7")
        self._line(sale, self.vb)                                    # in the order
        self._line(sale, other.variants.get())                       # different barcode: not ours to judge
        self._line(sale, self.va, category="Bundle-Merch")           # unpacked bundle row
        self.assertEqual(drop_misattributed_lines(sale, [self.vb.pk]), 0)
        self.assertEqual(sale.details.count(), 3)

    def test_both_products_in_one_order_keep_their_lines(self):
        sale = self._sale("o8")
        self._line(sale, self.va), self._line(sale, self.vb)
        self.assertEqual(drop_misattributed_lines(sale, [self.va.pk, self.vb.pk]), 0)
        self.assertEqual(sale.details.count(), 2)


class DuplicateBarcodeWarningTests(TestCase):
    def setUp(self):
        self.c = ClientStoreConnection.objects.create(
            client_email="a@b.com", platform="SHOPIFY", store_identifier="s.myshopify.com", access_token="t")

    def test_shared_barcode_flags_both_products_and_names_the_other(self):
        a, b = _upsert(self.c, 1, title="Album"), _upsert(self.c, 2, title="Album (re-listed)")
        info = duplicate_barcode_info(self.c)
        self.assertEqual(set(info), {a.pk, b.pk})
        self.assertEqual(info[a.pk]["barcodes"], [BARCODE])
        self.assertEqual(info[a.pk]["with"], [{"id": str(b.pk), "title": "Album (re-listed)"}])

    def test_unique_barcodes_are_not_flagged(self):
        _upsert(self.c, 1), _upsert(self.c, 2, sku="OTHER")
        self.assertEqual(duplicate_barcode_info(self.c), {})

    def test_comparison_ignores_case_and_surrounding_spaces(self):
        a, b = _upsert(self.c, 1, sku="abc-1"), _upsert(self.c, 2, sku=" ABC-1 ")
        self.assertEqual(set(duplicate_barcode_info(self.c)), {a.pk, b.pk})

    def test_missing_identifiers_never_count_as_shared(self):
        _upsert(self.c, 1, sku=""), _upsert(self.c, 2, sku="")
        self.assertEqual(duplicate_barcode_info(self.c), {})

    def test_setting_one_up_as_define_bundle_clears_the_warning(self):
        a, b = _upsert(self.c, 1), _upsert(self.c, 2)
        Product.objects.filter(pk=b.pk).update(category="Define Bundle")
        self.assertEqual(duplicate_barcode_info(self.c), {})

    def test_three_products_share_and_a_bundle_among_them_leaves_the_other_two_flagged(self):
        a, b, c = _upsert(self.c, 1), _upsert(self.c, 2), _upsert(self.c, 3)
        Product.objects.filter(pk=c.pk).update(category="Define Bundle")
        self.assertEqual(set(duplicate_barcode_info(self.c)), {a.pk, b.pk})

    def test_two_variants_of_one_product_sharing_a_barcode_are_not_flagged(self):
        p = _upsert(self.c, 1)
        Variant.objects.create(connection=self.c, product=p, sku_or_barcode=BARCODE, external_catalog_id="var-1b")
        self.assertEqual(duplicate_barcode_info(self.c), {})

    def test_other_connections_are_not_mixed_in(self):
        other = ClientStoreConnection.objects.create(
            client_email="c@d.com", platform="SQUARE", store_identifier="m1", access_token="t")
        _upsert(self.c, 1), _upsert(other, 1)
        self.assertEqual(duplicate_barcode_info(self.c), {})
        self.assertEqual(duplicate_barcode_info(other), {})


class ProductsApiWarningTests(TestCase):
    def _call(self, view_cls, **params):
        request = RequestFactory().get("/api/products/", params)
        return json.loads(view_cls.as_view()(request).content)["products"]

    def test_shopify_products_endpoint_carries_the_warning(self):
        from shopify_integration.views import ProductsView
        c = ClientStoreConnection.objects.create(
            client_email="a@b.com", platform="SHOPIFY", store_identifier="s.myshopify.com", access_token="t")
        _upsert(c, 1, title="Album"), _upsert(c, 2, title="Album 2"), _upsert(c, 3, sku="UNIQUE", title="Tee")
        rows = {r["title"]: r for r in self._call(ProductsView, email="a@b.com")}
        self.assertTrue(rows["Album"]["duplicate_barcode"])
        self.assertEqual(rows["Album"]["duplicate_barcodes"], [BARCODE])
        self.assertEqual([w["title"] for w in rows["Album"]["duplicate_barcode_with"]], ["Album 2"])
        self.assertFalse(rows["Tee"]["duplicate_barcode"])
        self.assertEqual(rows["Tee"]["duplicate_barcode_with"], [])
        # both products still report their own variant
        self.assertEqual(len(rows["Album"]["variants"]), 1)
        self.assertEqual(len(rows["Album 2"]["variants"]), 1)

    def test_square_products_endpoint_carries_the_warning(self):
        from square_integration.views import SquareProductsView
        c = ClientStoreConnection.objects.create(
            client_email="s@b.com", platform="SQUARE", store_identifier="m1", access_token="t")
        _upsert(c, 1, title="CD"), _upsert(c, 2, title="CD copy")
        rows = {r["title"]: r for r in self._call(SquareProductsView, email="s@b.com")}
        self.assertTrue(rows["CD"]["duplicate_barcode"] and rows["CD copy"]["duplicate_barcode"])
