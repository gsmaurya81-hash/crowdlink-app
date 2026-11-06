"""A re-connected store must not show every order line twice (client point 1)."""
from django.db import connection as db_connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from core_api.management.commands.dedupe_connections import repair_group
from core_api.models import ClientStoreConnection, Product, Sale, SaleDetail, Variant
from core_api.utils.line_cleanup import fix_cross_connection_lines
from shopify_integration.services.shopify_catalog import process_orders as shopify_process_orders
from square_integration.services.square_catalog import process_orders as square_process_orders

ITEMS = 4
ORDER_ID = "7347778421053"


def _conn(email, platform="SHOPIFY", store="s.myshopify.com", active=True):
    return ClientStoreConnection.objects.create(
        client_email=email, platform=platform, store_identifier=store, access_token="t", is_active=active)


def _variants(conn, n=ITEMS, prefix="gid://shopify/ProductVariant/", with_sku=False):
    out = []
    for i in range(n):
        p = Product.objects.create(connection=conn, external_product_id=f"gid://shopify/Product/{i}", title=f"P{i}")
        out.append(Variant.objects.create(
            connection=conn, product=p, external_catalog_id=f"{prefix}{i}",
            sku_or_barcode=f"SKU{i}" if with_sku else None, has_missing_identifier=not with_sku))
    return out


def _old_sale(conn, variants, reported=False):
    sale = Sale.objects.create(connection=conn, external_order_id=ORDER_ID, sale_date=timezone.now(),
                               system_location="Shopify Webstore", payment_type="CASH", reported_flag=reported)
    for v in variants:
        SaleDetail.objects.create(connection=conn, sale=sale, variant=v, external_order_id=ORDER_ID,
                                  sales_units=1, price="10.00")
    return sale


def _shopify_order(n=ITEMS):
    return {"id": int(ORDER_ID), "created_at": "2026-10-06T01:00:00Z", "source_name": "web",
            "payment_gateway_names": ["cash"],
            "line_items": [{"variant_id": i, "sku": "", "quantity": 1, "price": "10.00"} for i in range(n)]}


class ReconnectedStoreSync(TestCase):
    def setUp(self):
        self.old = _conn("scott@x.com", active=False)
        self.new = _conn("scott@x.com")
        self.old_variants = _variants(self.old)
        self.new_variants = _variants(self.new)

    def test_four_items_stay_four_lines_after_the_new_connection_syncs(self):
        _old_sale(self.old, self.old_variants)
        shopify_process_orders(self.new, [_shopify_order()], timezone.now())
        sale = Sale.objects.get(external_order_id=ORDER_ID)
        self.assertEqual(sale.connection_id, self.new.pk)
        lines = SaleDetail.objects.filter(sale=sale)
        self.assertEqual(lines.count(), ITEMS)                       # was 8 before the fix
        self.assertTrue(all(l.variant.connection_id == self.new.pk for l in lines))

    def test_syncing_again_changes_nothing(self):
        _old_sale(self.old, self.old_variants)
        for _ in range(3):
            shopify_process_orders(self.new, [_shopify_order()], timezone.now())
        self.assertEqual(SaleDetail.objects.count(), ITEMS)

    def test_reported_sale_is_left_alone(self):
        _old_sale(self.old, self.old_variants, reported=True)
        shopify_process_orders(self.new, [_shopify_order()], timezone.now())
        self.assertEqual(SaleDetail.objects.filter(variant__connection=self.old).count(), ITEMS)

    def test_a_line_with_no_equivalent_variant_is_kept_not_guessed(self):
        orphan_product = Product.objects.create(connection=self.old, external_product_id="gid://shopify/Product/99", title="X")
        orphan = Variant.objects.create(connection=self.old, product=orphan_product,
                                        external_catalog_id="gid://shopify/ProductVariant/99")
        sale = _old_sale(self.old, [orphan])
        Sale.objects.filter(pk=sale.pk).update(connection=self.new)     # sale moved, line did not
        out = fix_cross_connection_lines(SaleDetail, Variant, sale_ids=[sale.pk])
        self.assertEqual(out, {"repointed": 0, "deleted": 0, "unmatched": 1})
        self.assertEqual(SaleDetail.objects.filter(sale=sale, variant=orphan).count(), 1)

    def test_a_line_whose_equivalent_is_not_on_the_sale_is_repointed(self):
        sale = _old_sale(self.old, self.old_variants[:1])
        Sale.objects.filter(pk=sale.pk).update(connection=self.new)     # sale moved, line did not
        out = fix_cross_connection_lines(SaleDetail, Variant, sale_ids=[sale.pk])
        self.assertEqual(out["repointed"], 1)
        self.assertEqual(SaleDetail.objects.get(sale=sale).variant_id, self.new_variants[0].pk)


class SquareReconnect(TestCase):
    def test_square_sync_folds_old_connection_lines(self):
        old = _conn("scott@x.com", "SQUARE", "MERCH1", active=False)
        new = _conn("scott@x.com", "SQUARE", "MERCH1")
        ov = _variants(old, 2, prefix="SQCAT")
        _variants(new, 2, prefix="SQCAT")
        _old_sale(old, ov)
        order = {"id": ORDER_ID, "created_at": "2026-10-06T01:00:00Z", "source": {"name": "Square POS"},
                 "tenders": [{"type": "CASH"}],
                 "line_items": [{"catalog_object_id": f"SQCAT{i}", "quantity": "1",
                                 "base_price_money": {"amount": 1000}} for i in range(2)]}
        square_process_orders(new, [order], timezone.now())
        self.assertEqual(SaleDetail.objects.filter(sale__external_order_id=ORDER_ID).count(), 2)


class DedupeCommandMatchesVariantsWithoutSku(TestCase):
    def test_repair_group_matches_by_platform_id_when_there_is_no_sku(self):
        old = _conn("a@x.com", active=False)
        keeper = _conn("a@x.com")
        ov, nv = _variants(old, 2), _variants(keeper, 2)
        sale = _old_sale(old, ov)
        stats = repair_group(keeper, [old])
        self.assertEqual(stats["lines_unmatched"], 0)               # was 2: NULL sku could never match
        self.assertEqual(stats["lines_repointed"], 2)
        self.assertEqual({d.variant_id for d in SaleDetail.objects.filter(sale=sale)}, {v.pk for v in nv})


class FoldMigration(TransactionTestCase):
    before = [("core_api", "0027_unique_variant_catalog_id")]
    after = [("core_api", "0028_fold_stale_connection_lines")]

    def _migrate(self, targets):
        ex = MigrationExecutor(db_connection)
        ex.loader.build_graph()
        ex.migrate(targets)
        return ex.loader.project_state(targets).apps

    def tearDown(self):
        self._migrate(MigrationExecutor(db_connection).loader.graph.leaf_nodes())

    def test_ten_lines_for_four_items_become_four(self):
        apps = self._migrate(self.before)
        Conn, Prod, Var = (apps.get_model("core_api", n) for n in ("ClientStoreConnection", "Product", "Variant"))
        Sale_, Detail = apps.get_model("core_api", "Sale"), apps.get_model("core_api", "SaleDetail")

        def mk(email, active):
            return Conn.objects.create(client_email=email, platform="SQUARE", store_identifier="M", access_token="t",
                                       is_active=active)
        old, new = mk("s@x.com", False), mk("s@x.com", True)
        ov, nv = [], []
        for i in range(ITEMS):
            for conn, bucket in ((old, ov), (new, nv)):
                p = Prod.objects.create(connection=conn, external_product_id=f"P{i}", title=f"P{i}")
                bucket.append(Var.objects.create(connection=conn, product=p, external_catalog_id=f"CAT{i}"))
        sale = Sale_.objects.create(connection=new, external_order_id="O1", sale_date=timezone.now(),
                                    system_location="Square POS", payment_type="CASH")
        for v in ov + nv:                                           # 4 stale + 4 current
            Detail.objects.create(connection=v.connection, sale=sale, variant=v, external_order_id="O1", sales_units=1)
        # two more stale lines whose current copy is missing -> re-pointed, not deleted
        extra_old = []
        for i in (10, 11):
            p = Prod.objects.create(connection=old, external_product_id=f"P{i}", title=f"P{i}")
            extra_old.append(Var.objects.create(connection=old, product=p, external_catalog_id=f"CAT{i}"))
            np_ = Prod.objects.create(connection=new, external_product_id=f"P{i}", title=f"P{i}")
            Var.objects.create(connection=new, product=np_, external_catalog_id=f"CAT{i}")
            Detail.objects.create(connection=old, sale=sale, variant=extra_old[-1], external_order_id="O1", sales_units=1)
        self.assertEqual(Detail.objects.filter(sale=sale).count(), 10)

        apps = self._migrate(self.after)
        Detail, Var = apps.get_model("core_api", "SaleDetail"), apps.get_model("core_api", "Variant")
        lines = Detail.objects.filter(sale__external_order_id="O1")
        self.assertEqual(lines.count(), 6)                          # 4 items + the 2 re-pointed
        self.assertFalse(lines.exclude(variant__connection_id=sale.connection_id).exists())
        self.assertEqual(len({l.variant_id for l in lines}), 6)
