"""Sale header reporting date = the LATEST line date (client rule)."""
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone

from core_api.models import Product, Sale, SaleDetail
from core_api.utils.post_sync import client_today, refresh_reporting_dates, apply_reporting_dates
from core_api.test_sync_spec import make_conn, make_product, make_variant


def make_sale_lines(conn, oid, when, products, payment="CREDIT_CARD"):
    sale = Sale.objects.create(
        connection=conn, external_order_id=oid, sale_date=when, system_location="L",
        actual_location="L", sale_channel="POS", payment_type=payment, source_platform="Shopify")
    lines = []
    for i, p in enumerate(products):
        v = make_variant(conn, p, f"{oid}-{i}")
        lines.append(SaleDetail.objects.create(connection=conn, sale=sale, variant=v,
                                               external_order_id=oid, sales_units=1))
    return sale, lines


class HeaderDateIsLatestLineDate(TestCase):
    def setUp(self):
        self.conn = make_conn("SHOPIFY")
        self.today = client_today()
        self.sold = timezone.now() - timedelta(days=12)
        self.cd = make_product(self.conn, 1, release=self.today)                       # released today
        self.plain = make_product(self.conn, 2)                                        # no release date
        self.tee = make_product(self.conn, 3, report=False)                            # CLAU only
        self.tee_rel = make_product(self.conn, 4, report=False,
                                    release=self.today - timedelta(days=11))           # CLAU only, old release

    def header(self, sale):
        sale.refresh_from_db()
        return sale.aria_reporting_date, sale.clau_reporting_date

    def test_order_of_lines_does_not_matter(self):
        s1, _ = make_sale_lines(self.conn, "A", self.sold, [self.plain, self.cd])
        s2, _ = make_sale_lines(self.conn, "B", self.sold, [self.cd, self.plain])
        refresh_reporting_dates(self.conn)
        self.assertEqual(self.header(s1), self.header(s2))
        self.assertEqual(self.header(s1)[1], self.today)

    def test_aria_and_clau_headers_come_from_the_same_latest_line(self):
        # tee_rel (CLAU only, early) listed first, CD (ARIA + CLAU, today) second
        s, _ = make_sale_lines(self.conn, "C", self.sold, [self.tee_rel, self.cd])
        refresh_reporting_dates(self.conn)
        aria, clau = self.header(s)
        self.assertEqual(clau, self.today)
        self.assertEqual(aria, self.today)

    def test_sale_waits_for_its_last_item(self):
        future = make_product(self.conn, 5, release=self.today + timedelta(days=3))
        s, _ = make_sale_lines(self.conn, "D", self.sold, [self.cd, future])
        refresh_reporting_dates(self.conn)
        self.assertEqual(self.header(s), (None, None))
        Product.objects.filter(pk=future.pk).update(release_date=self.today + timedelta(days=1))
        Product.objects.filter(pk=future.pk).update(release_date=self.today)
        refresh_reporting_dates(self.conn)
        self.assertEqual(self.header(s)[1], self.today)

    def test_stale_early_header_is_corrected(self):
        s, _ = make_sale_lines(self.conn, "E", self.sold, [self.plain, self.cd])
        Sale.objects.filter(pk=s.pk).update(clau_reporting_date=self.sold.date())   # old "first line" value
        refresh_reporting_dates(self.conn)
        self.assertEqual(self.header(s)[1], self.today)

    def test_apply_reporting_dates_only_moves_header_forward(self):
        s, lines = make_sale_lines(self.conn, "F", self.sold, [self.cd, self.plain])
        s.refresh_from_db()
        apply_reporting_dates(s, lines[0], self.cd)        # today
        apply_reporting_dates(s, lines[1], self.plain)     # earlier (sale date) must NOT win
        s.refresh_from_db()
        self.assertEqual(s.clau_reporting_date, self.today)
