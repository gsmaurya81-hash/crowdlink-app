"""
Tests for the two sync rules.  Run:  python manage.py test core_api -v 2
All platform HTTP calls are mocked - nothing touches Shopify/Square.
"""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core_api.models import ClientStoreConnection, Product, Sale, SalesLocation, Variant
from core_api.utils.product_sync import MODE_REFRESH, WATERMARK_OVERLAP, run_connection_sync
from shopify_integration.services import shopify_catalog as shopify_sync
from square_integration.services import square_catalog as square_sync


def iso(dt):
    return dt.isoformat()


def make_conn(platform, last_sync_at=None, email="a@b.com"):
    return ClientStoreConnection.objects.create(
        client_email=email, platform=platform, store_identifier="shop.myshopify.com",
        access_token="tok", last_sync_at=last_sync_at)


# ------------------------------------------------------------------ SHOPIFY
def s_product(pid, vid, sku, created):
    return ({"id": f"gid://shopify/Product/{pid}", "title": f"P{pid}", "publishedAt": None,
             "createdAt": iso(created), "featuredImage": None},
            {"id": f"gid://shopify/ProductVariant/{vid}", "sku": sku, "barcode": "", "title": "v", "image": None})


def s_order(oid, vid, loc, created):
    return {"id": oid, "created_at": iso(created), "source_name": "pos", "location_id": loc,
            "payment_gateway_names": ["shopify_payments"],
            "line_items": [{"variant_id": vid, "sku": "", "quantity": 2, "price": "10.00"}]}


def s_loc(lid, created, updated):
    return {"id": lid, "name": f"Loc{lid}", "address1": "1 St", "city": "X", "province": "VIC",
            "zip": "3000", "country": "AU", "created_at": iso(created), "updated_at": iso(updated),
            "active": True}


class ShopifyRules(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        p = mock.patch.object(shopify_sync, "get_valid_access_token", return_value="tok")
        p.start(); self.addCleanup(p.stop)

    def patch_api(self, orders=(), locations=(), products=(), by_gid=()):
        ps = {
            "fetch_orders": mock.patch.object(shopify_sync, "fetch_orders", return_value=list(orders)),
            "fetch_locations": mock.patch.object(shopify_sync, "fetch_locations", return_value=list(locations)),
            "fetch_products": mock.patch.object(shopify_sync, "fetch_products", return_value=iter(list(products))),
            "fetch_variants_by_gid": mock.patch.object(shopify_sync, "fetch_variants_by_gid",
                                                       side_effect=lambda c, t, g: iter(list(by_gid))),
        }
        m = {k: p.start() for k, p in ps.items()}
        for p in ps.values():
            self.addCleanup(p.stop)
        return m

    # ---- Rule 1 -------------------------------------------------------
    def test_new_connection_rule(self):
        conn = make_conn("SHOPIFY")                        # last_sync_at NULL => new
        recent = self.now - timedelta(days=10)
        old = self.now - timedelta(days=200)
        order = s_order(1001, 5, 111, self.now - timedelta(days=3))   # references OLD variant 5 + OLD loc 111
        m = self.patch_api(
            orders=[order],
            locations=[s_loc(111, old, old),      # old but referenced by sale  -> keep
                      s_loc(222, recent, recent), # created in last 60d         -> keep
                      s_loc(333, old, old)],      # old + unreferenced          -> skip
            products=[s_product(2, 20, "NEW-SKU", recent)],               # created in last 60d -> keep
            by_gid=[s_product(1, 5, "OLD-SKU", old)],                     # old but referenced -> keep
        )
        res = run_connection_sync(conn)
        self.assertEqual(res["status"], "success"); self.assertEqual(res["mode"], "initial")

        # sales window = last 30 days by created_at, no updated_at filter
        kw = m["fetch_orders"].call_args.kwargs
        self.assertAlmostEqual((self.now - kw["created_at_min"]).days, 30, delta=1)
        self.assertNotIn("updated_at_min", kw)
        # product query = created in last 60 days
        q = m["fetch_products"].call_args.args[2]
        self.assertTrue(q.startswith("created_at:>="), q)
        # referenced-but-missing variant fetched by id
        self.assertEqual(list(m["fetch_variants_by_gid"].call_args.args[2]), ["gid://shopify/ProductVariant/5"])

        self.assertEqual(set(SalesLocation.objects.values_list("external_location_id", flat=True)), {"111", "222"})
        self.assertEqual(set(Variant.objects.values_list("sku_or_barcode", flat=True)), {"NEW-SKU", "OLD-SKU"})
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(Sale.objects.get().details.get().sales_units, 2)
        conn.refresh_from_db(); self.assertIsNotNone(conn.last_sync_at)

    # ---- Rule 2 -------------------------------------------------------
    def test_existing_connection_uses_updated_at_watermark(self):
        last = self.now - timedelta(days=1)
        conn = make_conn("SHOPIFY", last_sync_at=last)
        m = self.patch_api(
            locations=[s_loc(1, self.now - timedelta(days=90), self.now - timedelta(hours=1)),   # changed -> sync
                       s_loc(2, self.now - timedelta(days=90), self.now - timedelta(days=30))],  # unchanged -> skip
            products=[s_product(3, 30, "CHG-SKU", self.now - timedelta(days=99))])
        res = run_connection_sync(conn)
        self.assertEqual(res["mode"], "incremental")

        since = last - WATERMARK_OVERLAP
        self.assertEqual(m["fetch_orders"].call_args.kwargs, {"updated_at_min": since})
        self.assertTrue(m["fetch_products"].call_args.args[2].startswith("updated_at:>'"))
        self.assertEqual(list(SalesLocation.objects.values_list("external_location_id", flat=True)), ["1"])
        self.assertEqual(Variant.objects.get().sku_or_barcode, "CHG-SKU")
        conn.refresh_from_db(); self.assertGreater(conn.last_sync_at, last)

    def test_failure_does_not_advance_watermark(self):
        last = self.now - timedelta(days=1)
        conn = make_conn("SHOPIFY", last_sync_at=last)
        m = self.patch_api()
        m["fetch_orders"].side_effect = RuntimeError("Shopify down")
        res = run_connection_sync(conn)
        self.assertEqual(res["status"], "error")
        conn.refresh_from_db(); self.assertEqual(conn.last_sync_at, last)

    def test_new_connection_failure_stays_new(self):
        conn = make_conn("SHOPIFY")
        m = self.patch_api(); m["fetch_locations"].side_effect = RuntimeError("boom")
        self.assertEqual(run_connection_sync(conn)["status"], "error")
        conn.refresh_from_db(); self.assertIsNone(conn.last_sync_at)   # will retry as "new"

    def test_updated_order_is_upserted_not_duplicated_and_crowdlink_fields_kept(self):
        conn = make_conn("SHOPIFY", last_sync_at=self.now - timedelta(days=1))
        prod = s_product(1, 5, "SKU5", self.now - timedelta(days=5))
        order = s_order(9, 5, None, self.now - timedelta(days=2))
        self.patch_api(orders=[order], products=[prod]); run_connection_sync(conn)
        Product.objects.update(category="Tee", report_to_aria=False)      # client edits in CrowdLink
        order["line_items"][0]["quantity"] = 7                             # order edited on Shopify
        self.patch_api(orders=[order], products=[prod]); run_connection_sync(conn)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(Sale.objects.get().details.get().sales_units, 7)
        p = Product.objects.get(); self.assertEqual((p.category, p.report_to_aria), ("Tee", False))

    def test_lock_blocks_concurrent_run(self):
        conn = make_conn("SHOPIFY", last_sync_at=self.now)
        cache.add(f"sync_lock_{conn.pk}", "1", 60)
        self.assertEqual(run_connection_sync(conn)["status"], "skipped")

    # ---- Manual "refresh" resync -------------------------------------
    def test_refresh_uses_connection_created_at_not_last_sync_at(self):
        conn = make_conn("SHOPIFY", last_sync_at=self.now - timedelta(hours=2))
        old_created = self.now - timedelta(days=45)
        ClientStoreConnection.objects.filter(pk=conn.pk).update(created_at=old_created)
        conn.refresh_from_db()
        m = self.patch_api()

        res = run_connection_sync(conn, refresh=True)

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["mode"], MODE_REFRESH)
        # exact created_at, no watermark overlap subtracted
        self.assertEqual(m["fetch_orders"].call_args.kwargs, {"updated_at_min": old_created})
        self.assertTrue(m["fetch_products"].call_args.args[2].startswith("updated_at:>'"))

    def test_refresh_still_advances_last_sync_at_to_now(self):
        conn = make_conn("SHOPIFY", last_sync_at=self.now - timedelta(days=5))
        self.patch_api()
        run_connection_sync(conn, refresh=True)
        conn.refresh_from_db()
        # daily sync should carry on from "now" afterwards, not jump back to created_at
        self.assertGreater(conn.last_sync_at, self.now - timedelta(minutes=1))

    def test_force_initial_wins_over_refresh_when_both_passed(self):
        conn = make_conn("SHOPIFY", last_sync_at=self.now - timedelta(days=5))
        m = self.patch_api()
        res = run_connection_sync(conn, force_initial=True, refresh=True)
        self.assertEqual(res["mode"], "initial")
        self.assertNotIn("updated_at_min", m["fetch_orders"].call_args.kwargs)

    def test_refresh_on_never_synced_connection_falls_back_to_initial(self):
        conn = make_conn("SHOPIFY")  # last_sync_at is NULL -> still a "new" connection
        m = self.patch_api()
        res = run_connection_sync(conn, refresh=True)
        self.assertEqual(res["mode"], "initial")


# ------------------------------------------------------------------- SQUARE
def q_item(iid, vid, sku, created):
    return {"id": iid, "type": "ITEM", "created_at": iso(created), "updated_at": iso(created),
            "item_data": {"name": f"Item{iid}", "variations": [
                {"id": vid, "type": "ITEM_VARIATION",
                 "item_variation_data": {"item_id": iid, "sku": sku, "upc": "", "name": "reg"}}]}}


def q_order(oid, vid, loc, created):
    return {"id": oid, "created_at": iso(created), "location_id": loc, "source": {"name": "Square POS"},
            "tenders": [{"type": "CARD"}],
            "line_items": [{"catalog_object_id": vid, "quantity": "3", "base_price_money": {"amount": 1500}}]}


class SquareRules(TestCase):
    def setUp(self):
        cache.clear(); self.now = timezone.now()

    def test_orders_search_chunks_locations_and_follows_cursor(self):
        calls = []
        def fake(method, path, token, **kw):
            calls.append(kw["json"])
            first_page = "cursor" not in kw["json"]
            return {"orders": [{"id": f"o{len(calls)}"}], **({"cursor": "c2"} if first_page and len(calls) == 1 else {})}
        with mock.patch.object(square_sync, "_request", side_effect=fake):
            orders = square_sync.fetch_orders("tok", [f"L{i}" for i in range(12)], since=self.now, by="updated_at")
        self.assertEqual([len(c["location_ids"]) for c in calls], [10, 10, 2])       # max 10 per call
        self.assertEqual(calls[1]["cursor"], "c2")                                    # pagination followed
        f = calls[0]["query"]
        self.assertIn("updated_at", f["filter"]["date_time_filter"])
        self.assertEqual(f["sort"]["sort_field"], "UPDATED_AT")
        self.assertEqual(len(orders), 3)

    def _patch(self, **fns):
        for name, val in fns.items():
            p = mock.patch.object(square_sync, name, **(val if isinstance(val, dict) else {"return_value": val}))
            p.start(); self.addCleanup(p.stop)

    def test_new_connection_rule(self):
        conn = make_conn("SQUARE")
        recent, old = self.now - timedelta(days=10), self.now - timedelta(days=300)
        locs = [{"id": "LOLD", "name": "Old", "status": "ACTIVE", "created_at": iso(old), "address": {}},
                {"id": "LNEW", "name": "New", "status": "ACTIVE", "created_at": iso(recent), "address": {}},
                {"id": "LUNREF", "name": "Unref", "status": "ACTIVE", "created_at": iso(old), "address": {}}]
        items = [q_item("I1", "V1", "OLD-REF", old),        # old but referenced by sale -> keep
                 q_item("I2", "V2", "RECENT", recent),      # created in last 60d        -> keep
                 q_item("I3", "V3", "OLD-UNREF", old)]      # neither                    -> skip
        self._patch(fetch_locations=locs, fetch_all_items=(items, {}),
                    fetch_orders=[q_order("O1", "V1", "LOLD", self.now - timedelta(days=2))])
        res = run_connection_sync(conn)
        self.assertEqual(res["mode"], "initial")
        kw = square_sync.fetch_orders.call_args
        self.assertEqual(kw.kwargs["by"], "created_at")
        self.assertAlmostEqual((self.now - kw.kwargs["since"]).days, 30, delta=1)
        self.assertEqual(set(Variant.objects.values_list("sku_or_barcode", flat=True)), {"OLD-REF", "RECENT"})
        self.assertEqual(set(SalesLocation.objects.values_list("external_location_id", flat=True)), {"LOLD", "LNEW"})
        self.assertEqual(Sale.objects.count(), 1)

    def test_existing_connection_uses_updated_at(self):
        last = self.now - timedelta(days=1)
        conn = make_conn("SQUARE", last_sync_at=last)
        locs = [{"id": "L1", "name": "One", "status": "ACTIVE", "created_at": iso(self.now), "address": {}}]
        self._patch(fetch_locations=locs, fetch_orders=[],
                    fetch_items_changed_since=([q_item("I9", "V9", "CHANGED", self.now - timedelta(days=400))], {}))
        res = run_connection_sync(conn)
        self.assertEqual(res["mode"], "incremental")
        kw = square_sync.fetch_orders.call_args.kwargs
        self.assertEqual((kw["by"], kw["since"]), ("updated_at", last - WATERMARK_OVERLAP))
        self.assertEqual(square_sync.fetch_items_changed_since.call_args.args[1], last - WATERMARK_OVERLAP)
        self.assertEqual(Variant.objects.get().sku_or_barcode, "CHANGED")
        conn.refresh_from_db(); self.assertGreater(conn.last_sync_at, last)

    def test_refresh_uses_connection_created_at(self):
        conn = make_conn("SQUARE", last_sync_at=self.now - timedelta(hours=1))
        old_created = self.now - timedelta(days=20)
        ClientStoreConnection.objects.filter(pk=conn.pk).update(created_at=old_created)
        conn.refresh_from_db()
        locs = [{"id": "L1", "name": "One", "status": "ACTIVE", "created_at": iso(self.now), "address": {}}]
        self._patch(fetch_locations=locs, fetch_orders=[],
                    fetch_items_changed_since=([], {}))
        res = run_connection_sync(conn, refresh=True)
        self.assertEqual(res["mode"], MODE_REFRESH)
        kw = square_sync.fetch_orders.call_args.kwargs
        self.assertEqual((kw["by"], kw["since"]), ("updated_at", old_created))
        self.assertEqual(square_sync.fetch_items_changed_since.call_args.args[1], old_created)


# ------------------------------------------------------------ CRON COMMAND
class DailyCommand(TestCase):
    def test_only_active_connections_are_synced(self):
        a = make_conn("SHOPIFY", email="a@x.com")
        b = make_conn("SQUARE", email="b@x.com")
        b.is_active = False; b.save()
        with mock.patch("core_api.management.commands.sync_connections.run_connection_sync",
                        return_value={"status": "success"}) as run:
            call_command("sync_connections")
        self.assertEqual([c.args[0].pk for c in run.call_args_list], [a.pk])