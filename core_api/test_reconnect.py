"""Reconnect must land on the account's OWN connection (Scott / CLXL01)."""
from django.test import TestCase

from core_api.models import (ClientStoreConnection, Product, SalesLocation)
from core_api.utils.connection_guard import find_existing_connection
from core_api.utils.post_sync import build_formatted_address

STORE = "MERCHANT1"


def _conn(email, active=False, store=STORE):
    return ClientStoreConnection.objects.create(
        client_email=email, platform="SQUARE", store_identifier=store,
        access_token="t", is_active=active)


def _setup(conn, n=2):
    for i in range(n):
        Product.objects.create(connection=conn, external_product_id=f"p{i}", title=f"P{i}",
                               category="Vinyl Single")


class ReconnectPicksOwnConnection(TestCase):
    def test_own_row_beats_newer_other_row(self):
        scott = _conn("scott@x.com"); _setup(scott)
        soham = _conn("soham@x.com")          # created later
        self.assertEqual(find_existing_connection("SQUARE", STORE, "scott@x.com").pk, scott.pk)

    def test_email_match_is_case_insensitive(self):
        scott = _conn("Scott@X.com"); _setup(scott)
        _conn("soham@x.com")
        self.assertEqual(find_existing_connection("SQUARE", STORE, "scott@x.COM").pk, scott.pk)

    def test_two_rows_same_email_richest_wins_not_newest(self):
        rich = _conn("scott@x.com"); _setup(rich, 3)
        empty = _conn("scott@x.com", active=True)   # newer, active, no settings
        self.assertEqual(find_existing_connection("SQUARE", STORE, "scott@x.com").pk, rich.pk)

    def test_no_own_row_falls_back_to_richest_for_store(self):
        rich = _conn("a@x.com"); _setup(rich)
        _conn("b@x.com")
        self.assertEqual(find_existing_connection("SQUARE", STORE, "new@x.com").pk, rich.pk)

    def test_unknown_store_returns_none(self):
        self.assertIsNone(find_existing_connection("SQUARE", "NOPE", "scott@x.com"))


class FormattedAddressFallback(TestCase):
    def test_uses_address1_when_no_suburb_state_postcode(self):
        self.assertEqual(build_formatted_address("", "", "", "1 Main St"), "1 Main St")

    def test_prefers_suburb_state_postcode(self):
        self.assertEqual(build_formatted_address("Fitzroy", "VIC", "3065", "1 Main St"), "FITZROY VIC 3065")

    def test_blank_when_nothing(self):
        self.assertEqual(build_formatted_address("", "", "", None), "")


class MoveSalesAndPruneStaleSummaries(TestCase):
    def test_moves_sales_and_removes_summaries_without_sales(self):
        import datetime as dt
        from django.utils import timezone
        from core_api.models import Sale, SalesSummary
        from core_api.management.commands.dedupe_connections import repair_group, prune_orphan_summaries
        from core_api.utils.post_sync import refresh_after_sync

        keeper = _conn("scott@x.com", active=True)
        soham = _conn("soham@x.com")
        when = timezone.make_aware(dt.datetime(2026, 9, 13, 12, 0))
        sale_fields = [f.name for f in Sale._meta.fields]
        kw = dict(connection=soham, sale_date=when)
        if "external_sale_id" in sale_fields: kw["external_sale_id"] = "s1"
        if "sale_channel" in sale_fields: kw["sale_channel"] = "POS"
        Sale.objects.create(**kw)
        # stale summary on keeper for a day that has no sale anywhere
        stale = SalesSummary.objects.create(connection=keeper, summary_date=dt.date(2026, 8, 1),
                                            type="Square-POS-Card", ssid="739853-Square-POS-Card")
        repair_group(keeper, [soham])
        refresh_after_sync(keeper)
        removed = prune_orphan_summaries(keeper)
        self.assertEqual(Sale.objects.filter(connection=soham).count(), 0)
        self.assertEqual(Sale.objects.filter(connection=keeper).count(), 1)
        self.assertFalse(SalesSummary.objects.filter(pk=stale.pk).exists())
        self.assertEqual(removed, 1)
        self.assertEqual(SalesSummary.objects.filter(connection=keeper).count(), 1)