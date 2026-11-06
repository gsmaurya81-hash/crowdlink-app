"""Duplicate Variant rows -> the same order line shown 2-3 times on the Sales page."""
import uuid

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from core_api.models import ClientStoreConnection, Product, Sale, SaleDetail, Variant
from core_api.utils.product_sync import sync_upsert_product

GID = "gid://shopify/ProductVariant/44193145323816"
PGID = "gid://shopify/Product/8041695379752"


def _defaults(**kw):
    d = {"title": "TOUR", "variant_title": "Default Title", "has_missing_identifier": True,
         "external_catalog_id": GID, "external_product_id": PGID}
    d.update(kw)
    return d


class UpsertDoesNotDuplicate(TestCase):
    def setUp(self):
        self.c = ClientStoreConnection.objects.create(
            client_email="a@b.com", platform="SHOPIFY", store_identifier="s.myshopify.com", access_token="t")

    def test_no_sku_variant_upserted_repeatedly_stays_one_row(self):
        # products/create, products/update and the daily sync all call this for the same variant
        for _ in range(3):
            sync_upsert_product(self.c, f"MISSING-SHOPIFY-{GID}", _defaults())
        self.assertEqual(Variant.objects.filter(connection=self.c).count(), 1)
        self.assertIsNone(Variant.objects.get().sku_or_barcode)

    def test_variant_that_later_gets_a_sku_keeps_its_row(self):
        sync_upsert_product(self.c, f"MISSING-SHOPIFY-{GID}", _defaults())
        sync_upsert_product(self.c, "SKU-1", _defaults(has_missing_identifier=False))
        v = Variant.objects.get(connection=self.c)
        self.assertEqual(v.sku_or_barcode, "SKU-1")
        self.assertFalse(v.has_missing_identifier)

    def test_sku_changed_in_shopify_updates_the_same_variant(self):
        sync_upsert_product(self.c, "OLD", _defaults(has_missing_identifier=False))
        sync_upsert_product(self.c, "NEW", _defaults(has_missing_identifier=False))
        self.assertEqual(Variant.objects.filter(connection=self.c).count(), 1)
        self.assertEqual(Variant.objects.get().sku_or_barcode, "NEW")

    def test_database_refuses_a_second_variant_with_the_same_platform_id(self):
        p = Product.objects.create(connection=self.c, external_product_id=PGID, title="TOUR")
        Variant.objects.create(connection=self.c, product=p, external_catalog_id=GID)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Variant.objects.create(connection=self.c, product=p, external_catalog_id=GID)

    def test_many_variants_without_any_platform_id_are_still_allowed(self):
        p = Product.objects.create(connection=self.c, external_product_id=PGID, title="TOUR")
        for sku in ("A", "B"):
            Variant.objects.create(connection=self.c, product=p, sku_or_barcode=sku, external_catalog_id=None)
        self.assertEqual(Variant.objects.count(), 2)


class MergeMigration(TransactionTestCase):
    """Builds the production situation on the schema BEFORE 0027, then migrates."""
    before = [("core_api", "0026_remove_saledetail_sku_or_barcode")]
    after = [("core_api", "0027_unique_variant_catalog_id")]

    def _migrate(self, targets):
        ex = MigrationExecutor(connection)
        ex.loader.build_graph()
        ex.migrate(targets)
        return ex.loader.project_state(targets).apps

    def tearDown(self):
        self._migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    def test_three_copies_of_one_variant_collapse_to_one_order_line(self):
        apps = self._migrate(self.before)
        Conn, Prod, Var = (apps.get_model("core_api", n) for n in ("ClientStoreConnection", "Product", "Variant"))
        Sale_, Detail = apps.get_model("core_api", "Sale"), apps.get_model("core_api", "SaleDetail")

        conn = Conn.objects.create(client_email="a@b.com", platform="SHOPIFY", store_identifier="s", access_token="t")
        prod = Prod.objects.create(connection=conn, external_product_id=PGID, title="TOUR")
        # the three ids from production, newest-uuid-first on purpose
        ids = ["f12c23fd-db39-41a6-a628-37a85e188552", "7cbb3ee2-17a8-46f3-af8d-edf8d8af0000", "65a9519b-7b0b-414c-8d2c-40eacb8c9ec4"]
        variants = [Var.objects.create(id=uuid.UUID(i), connection=conn, product=prod, external_catalog_id=GID,
                                       sku_or_barcode=None, has_missing_identifier=True) for i in ids]
        sale = Sale_.objects.create(connection=conn, external_order_id="18919371571496", sale_date=timezone.now(),
                                    system_location="Shopify Webstore", payment_type="CASH")
        for v in variants:
            Detail.objects.create(connection=conn, sale=sale, variant=v, external_order_id="18919371571496",
                                  sales_units=1, price="1650.00")
        # a second, unrelated order that only used one of the copies must be kept and re-pointed
        other = Sale_.objects.create(connection=conn, external_order_id="999", sale_date=timezone.now(),
                                     system_location="Shopify Webstore", payment_type="CASH")
        Detail.objects.create(connection=conn, sale=other, variant=variants[2], external_order_id="999",
                              sales_units=2, price="1650.00")

        apps = self._migrate(self.after)
        Var, Detail = apps.get_model("core_api", "Variant"), apps.get_model("core_api", "SaleDetail")

        self.assertEqual(Var.objects.count(), 1)
        keeper = Var.objects.get()
        self.assertEqual(Detail.objects.filter(sale__external_order_id="18919371571496").count(), 1)
        self.assertEqual(Detail.objects.get(sale__external_order_id="999").variant_id, keeper.pk)
        self.assertEqual(Detail.objects.get(sale__external_order_id="999").sales_units, 2)

    def test_copy_with_a_real_sku_is_the_one_kept(self):
        apps = self._migrate(self.before)
        Conn, Prod, Var = (apps.get_model("core_api", n) for n in ("ClientStoreConnection", "Product", "Variant"))
        conn = Conn.objects.create(client_email="a@b.com", platform="SHOPIFY", store_identifier="s", access_token="t")
        prod = Prod.objects.create(connection=conn, external_product_id=PGID, title="TOUR")
        Var.objects.create(connection=conn, product=prod, external_catalog_id=GID, sku_or_barcode=None)
        Var.objects.create(connection=conn, product=prod, external_catalog_id=GID, sku_or_barcode="REAL-1")
        apps = self._migrate(self.after)
        self.assertEqual(apps.get_model("core_api", "Variant").objects.get().sku_or_barcode, "REAL-1")
