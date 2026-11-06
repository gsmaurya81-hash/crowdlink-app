"""
Tests for the client's sync spec items 3-5 (Sync tab / manual sync, historical
presale rule, post-sync refresh).   Run:  python manage.py test core_api -v 2
All platform HTTP calls are mocked.
"""
import datetime
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from core_api.models import (
    ClientStoreConnection, Product, Sale, SaleDetail, SalesLocation, SalesSummary,
    StandardLocality, Variant,
)
from core_api.tests import make_conn, s_order, s_product
from core_api.utils import post_sync, product_sync
from core_api.utils.post_sync import client_today, refresh_after_sync
from core_api.utils.product_sync import presale_backfill_products, run_connection_sync
from shopify_integration.services import shopify_catalog as shopify_sync


def make_product(conn, n, *, category="Vinyl Album 1LP", release=None, report=True,
                 webstore_created=None, loaded=False):
    return Product.objects.create(
        connection=conn, external_product_id=f"gid://P/{n}", title=f"P{n}", category=category,
        release_date=release, report_to_aria=report, webstore_creation_date=webstore_created,
        all_presales_loaded=loaded)


def make_variant(conn, product, sku, missing=False, n=None, catalog_id=None):
    return Variant.objects.create(
        connection=conn, product=product, sku_or_barcode=sku, has_missing_identifier=missing,
        external_catalog_id=catalog_id or f"gid://V/{n or sku}")


def make_sale(conn, variant, oid, when, *, payment="CREDIT_CARD", channel="POS", units=1,
              location="Shopify POS", actual=None):
    sale = Sale.objects.create(
        connection=conn, external_order_id=oid, sale_date=when, system_location=location,
        actual_location=actual or location, sale_channel=channel, payment_type=payment,
        source_platform="Shopify")
    detail = SaleDetail.objects.create(
        connection=conn,
        sale=sale,
        variant=variant,
        external_order_id=oid,
        sales_units=units
    )
    return sale, detail


# ---------------------------------------------------------------- sync 4
class PresaleWindow(TestCase):
    def setUp(self):
        self.conn = make_conn("SHOPIFY")
        self.today = client_today()
        self.web = timezone.now() - timedelta(days=90)

    def ids(self):
        return set(presale_backfill_products(self.conn, self.today).values_list("title", flat=True))

    def test_window_is_release_date_ge_today_minus_7(self):
        d = lambda n: self.today + timedelta(days=n)
        make_product(self.conn, 1, release=d(+5), webstore_created=self.web)    # future  -> yes
        make_product(self.conn, 2, release=d(0), webstore_created=self.web)     # today   -> yes
        make_product(self.conn, 3, release=d(-6), webstore_created=self.web)    # 6 ago   -> yes
        make_product(self.conn, 4, release=d(-7), webstore_created=self.web)    # boundary-> yes
        make_product(self.conn, 5, release=d(-8), webstore_created=self.web)    # too old  -> no
        make_product(self.conn, 6, release=d(+5), webstore_created=self.web, loaded=True)  # done -> no
        make_product(self.conn, 7, release=None, webstore_created=self.web)     # no date  -> no
        make_product(self.conn, 8, release=d(+5), webstore_created=None)        # no origin-> no
        self.assertEqual(self.ids(), {"P1", "P2", "P3", "P4"})


class PresaleEngine(TestCase):
    def setUp(self):
        cache.clear()
        p = mock.patch.object(shopify_sync, "get_valid_access_token", return_value="tok")
        p.start(); self.addCleanup(p.stop)

    def test_incremental_sync_backfills_presales_to_webstore_creation_date(self):
        now = timezone.now()
        conn = make_conn("SHOPIFY", last_sync_at=now - timedelta(days=1))
        created = now - timedelta(days=45)
        prod = make_product(conn, 1, release=client_today() + timedelta(days=3),
                            webstore_created=created)
        make_variant(conn, prod, "SKU1", catalog_id="gid://shopify/ProductVariant/11")
        old_order = s_order(9001, 11, 0, now - timedelta(days=20))

        def fake_orders(connection, token, **kw):
            return [old_order] if kw.get("created_at_min") == created else []

        with mock.patch.object(shopify_sync, "fetch_orders", side_effect=fake_orders) as fo, \
             mock.patch.object(shopify_sync, "fetch_locations", return_value=[]), \
             mock.patch.object(shopify_sync, "fetch_products", return_value=iter([])), \
             mock.patch.object(shopify_sync, "fetch_variants_by_gid", side_effect=lambda *a: iter([])):
            res = run_connection_sync(conn)

        self.assertEqual(res["status"], "success", res)
        self.assertEqual(fo.call_count, 2)                       # watermark fetch + presale backfill
        self.assertEqual(Sale.objects.filter(external_order_id="9001").count(), 1)
        prod.refresh_from_db(); self.assertTrue(prod.all_presales_loaded)

    def _run_old_presale(self, has_scope):
        now = timezone.now()
        conn = make_conn("SHOPIFY", last_sync_at=now - timedelta(days=1))
        created = now - timedelta(days=100)
        prod = make_product(conn, 1, release=client_today() + timedelta(days=3),
                            webstore_created=created)
        with mock.patch.object(shopify_sync, "has_read_all_orders", return_value=has_scope), \
             mock.patch.object(shopify_sync, "fetch_orders", return_value=[]) as fo, \
             mock.patch.object(shopify_sync, "fetch_locations", return_value=[]), \
             mock.patch.object(shopify_sync, "fetch_products", return_value=iter([])), \
             mock.patch.object(shopify_sync, "fetch_variants_by_gid", side_effect=lambda *a: iter([])):
            res = run_connection_sync(conn)
        self.assertEqual(res["status"], "success", res)
        prod.refresh_from_db()
        return prod, res

    def test_history_beyond_60_days_loads_with_read_all_orders(self):
        prod, res = self._run_old_presale(True)
        self.assertTrue(prod.all_presales_loaded)
        self.assertFalse(res["presale_history_capped"])

    def test_history_beyond_60_days_stays_pending_without_scope(self):
        prod, res = self._run_old_presale(False)
        self.assertFalse(prod.all_presales_loaded)      # retried after the store reconnects
        self.assertTrue(res["presale_history_capped"])

    def test_old_release_date_is_not_backfilled(self):
        now = timezone.now()
        conn = make_conn("SHOPIFY", last_sync_at=now - timedelta(days=1))
        make_product(conn, 1, release=client_today() - timedelta(days=30),
                     webstore_created=now - timedelta(days=45))
        with mock.patch.object(shopify_sync, "fetch_orders", return_value=[]) as fo, \
             mock.patch.object(shopify_sync, "fetch_locations", return_value=[]), \
             mock.patch.object(shopify_sync, "fetch_products", return_value=iter([])), \
             mock.patch.object(shopify_sync, "fetch_variants_by_gid", side_effect=lambda *a: iter([])):
            run_connection_sync(conn)
        self.assertEqual(fo.call_count, 1)                       # only the watermark fetch


# ---------------------------------------------------------------- sync 5
class PostSyncIdentifiers(TestCase):

    def test_identifier_from_variants_and_missing_flag(self):
        conn = make_conn("SHOPIFY")
        a = make_product(conn, 1)
        b = make_product(conn, 2)
        c = make_product(conn, 3)
        make_variant(conn, a, "MISSING-SHOPIFY-x", missing=True, n="a1")
        make_variant(conn, a, "REAL-1", n="a2")
        make_variant(conn, b, "MISSING-SHOPIFY-y", missing=True, n="b1")
        b.combined_sku_bar = "STALE"
        b.has_missing_identifier = False
        b.save()
        c.combined_sku_bar = "KEEP"
        c.save()
        refresh_after_sync(conn)
        a.refresh_from_db()
        b.refresh_from_db()
        c.refresh_from_db()
        self.assertEqual(
            (a.combined_sku_bar, a.has_missing_identifier),
            ("REAL-1", False)
        )
        self.assertEqual(
            (b.combined_sku_bar, b.has_missing_identifier),
            (None, True)
        )
        self.assertEqual(c.combined_sku_bar, "KEEP")


class PostSyncLocations(TestCase):            # 5.3
    def setUp(self):
        self.conn = make_conn("SHOPIFY")
        StandardLocality.objects.create(
            locid=5, locality="MELBOURNE VIC 3000", suburb="MELBOURNE", postcode="3000",
            state="VIC", latitude=Decimal("-37.813600"), longitude=Decimal("144.963100"))

    def loc(self, ext, **kw):
        return SalesLocation.objects.create(connection=self.conn, external_location_id=ext,
                                            platform="SHOPIFY", name=f"L{ext}", **kw)

    def test_fills_locid_address_and_coordinates(self):
        hit = self.loc("1", city="melbourne", province="Victoria", postal_code="3000")
        miss = self.loc("2", city="Nowhere", province="Victoria", postal_code="9999")
        no_pc = self.loc("3", city="Melbourne", province="VIC", postal_code="")
        refresh_after_sync(self.conn)
        for o in (hit, miss, no_pc):
            o.refresh_from_db()
        self.assertEqual(hit.locid, 5)
        self.assertEqual(hit.formatted_address, "MELBOURNE VIC 3000")
        self.assertEqual(hit.latitude, Decimal("-37.813600"))
        self.assertIsNone(miss.locid); self.assertIsNone(no_pc.locid)

    def test_existing_platform_coordinates_and_set_locid_are_kept(self):
        precise = self.loc("1", city="Melbourne", province="VIC", postal_code="3000",
                           latitude=Decimal("-37.900000"), longitude=Decimal("144.900000"))
        done = self.loc("2", city="Melbourne", province="VIC", postal_code="3000",
                        locid=77, formatted_address="user text")
        refresh_after_sync(self.conn)
        precise.refresh_from_db(); done.refresh_from_db()
        self.assertEqual(precise.locid, 5)
        self.assertEqual(precise.latitude, Decimal("-37.900000"))     # not overwritten
        self.assertEqual((done.locid, done.formatted_address), (77, "user text"))


class PostSyncReportingDates(TestCase):       # 5.5 - 5.8
    def setUp(self):
        self.conn = make_conn("SHOPIFY")
        self.today = client_today()
        self.sold = timezone.now() - timedelta(days=2)

    def run_refresh(self, product, **sale_kw):
        v = make_variant(self.conn, product, f"S{product.pk}")
        sale, detail = make_sale(self.conn, v, f"O{product.pk}", self.sold, **sale_kw)
        refresh_after_sync(self.conn)
        sale.refresh_from_db(); detail.refresh_from_db()
        return sale, detail

    def test_no_release_date_reports_on_sale_date(self):
        sale, detail = self.run_refresh(make_product(self.conn, 1))
        self.assertEqual(sale.aria_reporting_date, self.sold)
        self.assertEqual(sale.clau_reporting_date, self.sold.date())
        self.assertEqual(detail.aria_reporting_date, self.sold)

    def test_reached_release_date_reports_on_release_date(self):
        rel = self.today - timedelta(days=1)
        sale, detail = self.run_refresh(make_product(self.conn, 1, release=rel))
        self.assertEqual(sale.clau_reporting_date, rel)
        self.assertEqual(sale.aria_reporting_date.astimezone(post_sync._client_tz()).date(), rel)
        self.assertEqual(detail.clau_reporting_date, rel)

    def test_future_release_date_not_reportable_yet(self):
        sale, detail = self.run_refresh(make_product(self.conn, 1, release=self.today + timedelta(days=4)))
        for o in (sale, detail):
            self.assertIsNone(o.aria_reporting_date); self.assertIsNone(o.clau_reporting_date)

    def test_report_to_aria_false_still_gets_clau_date(self):
        sale, _ = self.run_refresh(make_product(self.conn, 1, report=False))
        self.assertIsNone(sale.aria_reporting_date); self.assertEqual(sale.clau_reporting_date, self.sold.date())

    def test_non_card_and_non_reportable_category_skip_aria_only(self):
        s1, _ = self.run_refresh(make_product(self.conn, 1), payment="CASH")
        s2, _ = self.run_refresh(make_product(self.conn, 2, category="Tee"))
        for s in (s1, s2):
            self.assertIsNone(s.aria_reporting_date); self.assertIsNotNone(s.clau_reporting_date)

    def test_existing_dates_never_overwritten(self):
        p = make_product(self.conn, 1)
        v = make_variant(self.conn, p, "S1")
        sale, detail = make_sale(self.conn, v, "O1", self.sold)
        keep_dt = timezone.now() - timedelta(days=30); keep_d = keep_dt.date()
        Sale.objects.filter(pk=sale.pk).update(aria_reporting_date=keep_dt, clau_reporting_date=keep_d)
        refresh_after_sync(self.conn)
        sale.refresh_from_db()
        self.assertEqual((sale.aria_reporting_date, sale.clau_reporting_date), (keep_dt, keep_d))

    def test_release_date_set_later_is_picked_up_by_next_refresh(self):
        p = make_product(self.conn, 1, release=self.today + timedelta(days=3))
        v = make_variant(self.conn, p, "S1"); sale, _ = make_sale(self.conn, v, "O1", self.sold)
        refresh_after_sync(self.conn); sale.refresh_from_db()
        self.assertIsNone(sale.clau_reporting_date)
        Product.objects.filter(pk=p.pk).update(release_date=self.today)      # release arrives
        refresh_after_sync(self.conn); sale.refresh_from_db()
        self.assertEqual(sale.clau_reporting_date, self.today)


class PostSyncSummaries(TestCase):            # 5.4
    def test_ssid_and_summary_refreshed_without_losing_user_data(self):
        conn = make_conn("SHOPIFY")
        album = make_product(conn, 1, category="CD Album"); tee = make_product(conn, 2, category="Tee")
        va = make_variant(conn, album, "A"); vt = make_variant(conn, tee, "T")
        when = timezone.now() - timedelta(days=1)
        s1, _ = make_sale(conn, va, "O1", when, units=2, actual="Sydney NSW")
        s2, _ = make_sale(conn, vt, "O2", when, units=3, actual="Sydney NSW")

        refresh_after_sync(conn)
        s1.refresh_from_db()
        summary = SalesSummary.objects.get()
        self.assertEqual(s1.ssid, f"{when.date().toordinal()}-Shopify-POS-Card")
        self.assertEqual(summary.ssid, s1.ssid)
        self.assertEqual((summary.albums_units, summary.merch_units, summary.location), (2, 3, "Sydney NSW"))

        # user edits the line, another order lands, sync refreshes again
        loc = SalesLocation.objects.create(connection=conn, name="My Gig", platform="SHOPIFY",
                                           external_location_id="9")
        SalesSummary.objects.filter(pk=summary.pk).update(
            audience_size=250, notes="great night", assigned_location=loc)
        make_sale(conn, va, "O3", when, units=1)
        refresh_after_sync(conn)
        summary.refresh_from_db()
        self.assertEqual((summary.audience_size, summary.notes, summary.assigned_location_id),
                         (250, "great night", loc.pk))
        self.assertEqual((summary.albums_units, summary.merch_units, summary.location), (3, 3, "My Gig"))
        self.assertEqual(SalesSummary.objects.count(), 1)

    def test_refresh_is_idempotent(self):
        conn = make_conn("SHOPIFY")
        p = make_product(conn, 1); v = make_variant(conn, p, "A")
        make_sale(conn, v, "O1", timezone.now() - timedelta(days=1))
        first = refresh_after_sync(conn); second = refresh_after_sync(conn)
        self.assertEqual(second["sale_lines_dated"], 0)
        self.assertEqual(first["summary_lines"], second["summary_lines"])
        self.assertEqual(SalesSummary.objects.count(), 1)


class PostSyncIsPartOfEverySync(TestCase):
    def test_failed_refresh_does_not_advance_last_sync_at(self):
        last = timezone.now() - timedelta(days=1)
        conn = make_conn("SHOPIFY", last_sync_at=last)
        with mock.patch.object(shopify_sync, "run_sync", return_value={"mode": "incremental"}), \
             mock.patch.object(product_sync, "refresh_after_sync", side_effect=RuntimeError("boom")):
            res = run_connection_sync(conn)
        self.assertEqual(res["status"], "error")
        conn.refresh_from_db(); self.assertEqual(conn.last_sync_at, last)


# ---------------------------------------------------------------- sync 3 / Sync tab
class _InlineThread:
    """Runs the worker immediately so the test can assert on the final state."""
    def __init__(self, target=None, daemon=None, **kw): self.target = target
    def start(self): self.target()


class ManualSync(TestCase):
    def setUp(self):
        cache.clear()
        p = mock.patch("django.db.connections.close_all"); p.start(); self.addCleanup(p.stop)

    def test_runs_refresh_sync_for_every_active_connection(self):
        make_conn("SHOPIFY", last_sync_at=timezone.now()); make_conn("SQUARE", last_sync_at=timezone.now())
        ClientStoreConnection.objects.create(client_email="a@b.com", platform="SQUARE", is_active=False,
                                             store_identifier="old", access_token="t")
        with mock.patch.object(product_sync.threading, "Thread", _InlineThread), \
             mock.patch.object(product_sync, "run_connection_sync",
                               return_value={"status": "success"}) as run:
            self.assertEqual(product_sync.start_manual_sync("a@b.com")["state"], "running")
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(c.kwargs == {"refresh": True} for c in run.call_args_list))
        st = product_sync.manual_sync_status("a@b.com")
        self.assertEqual(st["state"], "done"); self.assertEqual(len(st["results"]), 2)

    def test_error_is_reported_and_status_never_stuck_running(self):
        make_conn("SHOPIFY", last_sync_at=timezone.now())
        with mock.patch.object(product_sync.threading, "Thread", _InlineThread), \
             mock.patch.object(product_sync, "run_connection_sync", side_effect=RuntimeError("x")):
            product_sync.start_manual_sync("a@b.com")
        self.assertEqual(product_sync.manual_sync_status("a@b.com")["state"], "error")

    def test_second_click_while_running_does_not_start_another(self):
        make_conn("SHOPIFY", last_sync_at=timezone.now())
        with mock.patch.object(product_sync.threading, "Thread") as thread:
            product_sync.start_manual_sync("a@b.com")
            again = product_sync.start_manual_sync("a@b.com")
        self.assertEqual(again["state"], "running"); self.assertEqual(thread.call_count, 1)

    def test_endpoint(self):
        url = "/api/account/sync/manual/?email=a@b.com"
        self.assertEqual(self.client.post(url).status_code, 404)                 # nothing connected
        self.assertEqual(self.client.post("/api/account/sync/manual/").status_code, 400)
        make_conn("SHOPIFY", last_sync_at=timezone.now())
        with mock.patch.object(product_sync.threading, "Thread"):
            r = self.client.post(url)
        self.assertEqual((r.status_code, r.json()["state"]), (202, "running"))
        g = self.client.get(url).json()
        self.assertEqual(g["state"], "running"); self.assertIsNotNone(g["last_sync_at"])
        self.assertEqual(self.client.get("/api/account/sync/manual/?email=zzz@b.com").json()["state"], "idle")
