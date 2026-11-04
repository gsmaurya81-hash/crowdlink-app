"""
core_api/utils/post_sync.py

"All sync types - refresh fields after sync" (client spec, item 5).

Runs after EVERY successful platform sync (new connection, daily, manual) via
run_connection_sync(), in this order:

  5.1 / 5.2  product.combined_sku_bar + product.has_missing_identifier   <- variants
  5.3        SalesLocation.locid / formatted_address / lat / long     <- StandardLocality
  5.4        Sale.ssid, then SalesSummary rows (user-entered audience size,
             notes and assigned location are NEVER touched)
  5.5-5.8    Sale / SaleDetail aria_reporting_date + clau_reporting_date

Every step is idempotent, so re-running it is always safe.

`reporting_dates_for()` and `build_summary_groups()` are the single source of
truth for their rules; webhooks, the platform engines and SalesView all call
them so a rule can never drift between code paths.
"""
import datetime
import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core_api.models import (
    Product, Sale, SaleDetail, SalesLocation, SalesSummary, StandardLocality,
    is_category_aria_reportable,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

logger = logging.getLogger("shopify_integration")  # -> logs/sync.log

CLIENT_TIME_ZONE = "Australia/Sydney"


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------
def _client_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(CLIENT_TIME_ZONE)
        except Exception:
            pass
    return datetime.timezone.utc


def client_today():
    """'Sync date (today)' in the client's timezone (same clock the
    release-date validation uses)."""
    return datetime.datetime.now(_client_tz()).date()


def _midnight(d):
    """A date as an aware datetime at 00:00 client time."""
    return datetime.datetime.combine(d, datetime.time.min, tzinfo=_client_tz())


# --------------------------------------------------------------------------
# 5.5 - 5.8  reporting dates
# --------------------------------------------------------------------------
def reporting_dates_for(product, sale_date, is_card_sale, today=None):
    """
    Returns (aria_reporting_date, clau_reporting_date) - the value each field
    should take IF it is currently null. Either can be None (= leave null).

      release_date NULL        -> reported on the sale date
      release_date <= today    -> reported on the release date (presale sales
                                  are reported once the release date arrives)
      release_date in future   -> not reportable yet (both None)

    ARIA additionally needs product.report_to_aria = true. Two guards kept
    from earlier client rules: only CARD sales go to ARIA, and only ARIA-
    reportable categories (a never-categorised product defaults to
    report_to_aria=true in the DB, but the UI shows it as "not reportable").
    CLAU dates have no such conditions.
    """
    today = today or client_today()
    release = product.release_date

    if release is None:
        aria_base = sale_date
        clau = sale_date.date() if sale_date else None
    elif release <= today:
        aria_base = _midnight(release)
        clau = release
    else:
        return None, None

    aria = None
    if (
        is_card_sale
        and product.report_to_aria
        and is_category_aria_reportable(product.category)
    ):
        aria = aria_base
    return aria, clau


def apply_reporting_dates(sale, detail, product, sync_started_at=None, is_card_sale=True):
    """Stamp the reporting dates on one sale line. Dates are (re)calculated
    while sale.reported_flag is False - the client sets release_date /
    report_to_aria after the sync - and are frozen once the flag is True.
    `sync_started_at` is accepted for backwards compatibility and unused."""
    if sale.reported_flag:
        return
    aria, clau = reporting_dates_for(product, sale.sale_date, is_card_sale)

    for obj in (sale, detail):
        fields = []
        if aria is not None and obj.aria_reporting_date != aria:
            obj.aria_reporting_date = aria
            fields.append("aria_reporting_date")
        if clau is not None and obj.clau_reporting_date != clau:
            obj.clau_reporting_date = clau
            fields.append("clau_reporting_date")
        if fields:
            obj.save(update_fields=fields)


def refresh_reporting_dates(connection, today=None):
    today = today or client_today()
    qs = (
        SaleDetail.objects.filter(connection=connection, sale__reported_flag=False)
        .select_related("sale", "variant__product")
        .order_by("sale_id", "created_at")
    )
    sales = {}          # share ONE Sale object per sale across its lines
    sale_done = set()   # sale header takes its dates from its FIRST line only
    touched_sales, touched_details = set(), []
    for d in qs.iterator(chunk_size=500):
        product = d.variant.product if d.variant_id else None
        if product is None:
            continue
        sale = sales.setdefault(d.sale_id, d.sale)
        # Fix: Restore support for MANUAL_CARD_ENTRY and CARD_ON_FILE
        is_card = sale.payment_type in ('CREDIT_CARD', 'MANUAL_CARD_ENTRY', 'CARD_ON_FILE')
        aria, clau = reporting_dates_for(
            product, sale.sale_date, is_card, today)
        changed = False
        if aria is not None and d.aria_reporting_date != aria:
            d.aria_reporting_date, changed = aria, True
        if clau is not None and d.clau_reporting_date != clau:
            d.clau_reporting_date, changed = clau, True
        if changed:
            touched_details.append(d)
        if d.sale_id in sale_done:
            continue
        sale_done.add(d.sale_id)
        if aria is not None and sale.aria_reporting_date != aria:
            sale.aria_reporting_date = aria
            touched_sales.add(sale.pk)
        if clau is not None and sale.clau_reporting_date != clau:
            sale.clau_reporting_date = clau
            touched_sales.add(sale.pk)

    SaleDetail.objects.bulk_update(
        touched_details, ["aria_reporting_date", "clau_reporting_date"], batch_size=500)
    Sale.objects.bulk_update(
        [sales[pk] for pk in touched_sales],
        ["aria_reporting_date", "clau_reporting_date"], batch_size=500)
    return {"sale_lines_dated": len(touched_details), "sales_dated": len(touched_sales)}


# --------------------------------------------------------------------------
# 5.1 / 5.2  product identifiers
# --------------------------------------------------------------------------
def combine_variant_skus(variants):
    """Every REAL variant SKU/barcode, de-duplicated, sorted, space-separated
    (e.g. "REPTEE0004 REPTEE0005"); None if there is no real identifier.
    Placeholders (MISSING-...) are excluded. Single source of truth: used by
    refresh_product_identifiers() AND sync_upsert_product()."""
    real = sorted({
        v.sku_or_barcode.strip()
        for v in variants
        if v.sku_or_barcode
        and not v.has_missing_identifier
        and not v.sku_or_barcode.upper().startswith("MISSING")
    })
    return " ".join(real) if real else None


def refresh_product_identifiers(connection):
    """product.combined_sku_bar <- every REAL variant SKU/barcode of the product,
    de-duplicated, sorted and space-separated (e.g. "REPTEE0004 REPTEE0005").
    Placeholders (MISSING-...) are excluded. has_missing_identifier = (no real
    identifier at all). Products with no variant rows are left alone - there
    is nothing to derive from."""
    changed = []
    products = Product.objects.filter(connection=connection).prefetch_related("variants")
    for p in products:
        variants = list(p.variants.all())
        if not variants:
            continue

        combined = combine_variant_skus(variants)
        missing = combined is None

        if p.combined_sku_bar != combined or p.has_missing_identifier != missing:
            p.combined_sku_bar = combined
            p.has_missing_identifier = missing
            changed.append(p)

    Product.objects.bulk_update(changed, ["combined_sku_bar", "has_missing_identifier"], batch_size=500)
    return {"products_identifier_refreshed": len(changed)}


# --------------------------------------------------------------------------
# 5.3  location locid
# --------------------------------------------------------------------------
_STATE_CODES = {
    "new south wales": "NSW", "victoria": "VIC", "queensland": "QLD",
    "south australia": "SA", "western australia": "WA", "tasmania": "TAS",
    "northern territory": "NT", "australian capital territory": "ACT",
}


def _state_code(province):
    p = (province or "").strip()
    if not p:
        return None
    return _STATE_CODES.get(p.lower(), p.upper())


def find_locality(city, province, postal_code):
    """StandardLocality for city (suburb) + postcode, preferring the same
    state. Both city and postcode are required to avoid false matches."""
    city, postal_code = (city or "").strip(), (postal_code or "").strip()
    if not city or not postal_code:
        return None
    qs = StandardLocality.objects.filter(
        locid__isnull=False, suburb__iexact=city, postcode=postal_code)
    state = _state_code(province)
    if state:
        match = qs.filter(state__iexact=state).first()
        if match:
            return match
    return qs.first()


def refresh_location_locids(connection):
    """Only rows with locid NULL are touched. locid + formatted_address are
    filled from the locality table; coordinates only if the platform did not
    already give us some (a store's own coordinates are more precise than a
    suburb centroid)."""
    updated = 0
    for loc in SalesLocation.objects.filter(connection=connection, locid__isnull=True):
        match = find_locality(loc.city, loc.province, loc.postal_code)
        if not match:
            continue
        loc.locid = match.locid
        loc.formatted_address = match.locality or loc.formatted_address
        fields = ["locid", "formatted_address"]
        if loc.latitude is None and loc.longitude is None:
            loc.latitude, loc.longitude = match.latitude, match.longitude
            fields += ["latitude", "longitude"]
        loc.save(update_fields=fields + ["updated_at"])
        updated += 1
    return {"locations_locid_filled": updated}


# --------------------------------------------------------------------------
# 5.4  SSID + SalesSummary
# --------------------------------------------------------------------------
_GENERIC_LOCATIONS = {
    "N/A", "Unknown", "", None,
    "Shopify POS", "Square POS", "Shopify Webstore", "Square Webstore",
}


def _expected_type(sale):
    loc_part = (sale.system_location or "Unknown").replace(" ", "-")
    pay_part = "Card" if sale.payment_type in ('CREDIT_CARD', 'MANUAL_CARD_ENTRY', 'CARD_ON_FILE') else "Other"
    return f"{loc_part}-{pay_part}"


def _product_category(detail):
    if detail.variant and detail.variant.product:
        return detail.variant.product.category
    return detail.product_category


def build_summary_groups(connections):
    """
    Group sales into (connection, calendar date, type) lines, make sure each
    has a SalesSummary row, and refresh ONLY the calculated columns on it
    (ssid, albums_units, merch_units, location). audience_size, notes and
    assigned_location are user-entered and are never written here.

    Also normalises Sale.type and stamps Sale.ssid. Returns one dict per
    line (newest first) - SalesView serialises these directly.
    """
    sales = list(
        Sale.objects.filter(connection__in=connections)
        .select_related("connection")
        .prefetch_related("details__variant__product")
        .order_by("-sale_date")
    )

    groups, order, sales_changed = {}, [], []
    for s in sales:
        changed = []
        expected = _expected_type(s)
        if s.type != expected:
            s.type = expected
            changed.append("type")
        day = s.sale_date.date() if s.sale_date else None
        ssid = f"{day.toordinal() if day else 0}-{s.type}"
        if s.ssid != ssid:
            s.ssid = ssid
            changed.append("ssid")
        if changed:
            sales_changed.append(s)
        key = (s.connection_id, day, s.type)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(s)
    Sale.objects.bulk_update(sales_changed, ["type", "ssid"], batch_size=500)

    lines = []
    for key in order:
        connection_id, day, sale_type = key
        bucket = groups[key]
        channel = bucket[0].sale_channel if bucket else "POS"
        is_webstore = channel == "WEBSTORE"

        summary, _ = SalesSummary.objects.get_or_create(
            connection_id=connection_id, summary_date=day, type=sale_type)

        details = [d for s in bucket for d in s.details.all()]
        
        # Fix: Exclude parent 'Define Bundle' items so they aren't double-counted alongside their unpacked children
        albums = sum(d.sales_units for d in details if is_category_aria_reportable(_product_category(d)) and _product_category(d) != 'Define Bundle')
        merch = sum(d.sales_units for d in details if not is_category_aria_reportable(_product_category(d)) and _product_category(d) != 'Define Bundle')

        if is_webstore:
            location_name = "Webstore Sales"
            locid = None
        elif summary.assigned_location_id:
            location_name = summary.assigned_location.name
            locid = summary.assigned_location.locid
        else:
            locid = None
            actual = {s.actual_location for s in bucket
                      if s.actual_location and s.actual_location not in _GENERIC_LOCATIONS}
            if len(actual) == 1:
                location_name = next(iter(actual))
            elif len(actual) > 1:
                location_name = "Unknown"
            else:
                system = {s.system_location for s in bucket
                          if s.system_location and s.system_location not in _GENERIC_LOCATIONS}
                location_name = next(iter(system)) if len(system) == 1 else "Unknown"

        ssid = f"{day.toordinal() if day else 0}-{sale_type}"
        if (summary.ssid, summary.albums_units, summary.merch_units, summary.location, summary.locid) != (
                ssid, albums, merch, location_name, locid):
            summary.ssid, summary.albums_units = ssid, albums
            summary.merch_units, summary.location = merch, location_name
            summary.locid = locid
            summary.save(update_fields=["ssid", "albums_units", "merch_units", "location", "locid", "updated_at"])

        lines.append({
            "summary": summary, "bucket": bucket, "details": details,
            "sale_day": day, "sale_type": sale_type, "is_webstore": is_webstore,
            "albums_units": albums, "merch_units": merch, "location_name": location_name,
        })
    return lines


def refresh_sales_summaries(connection):
    lines = build_summary_groups([connection])
    return {"summary_lines": len(lines)}


def unpack_bundles_for_sale(sale, connection, sync_started_at):
    """Shared helper to dynamically unpack 'Define Bundle' items into individual reporting lines."""
    if sale.reported_flag:
        return

    # 1. Clear any existing unpacked bundle rows for this sale
    SaleDetail.objects.filter(sale=sale, product_category__startswith="Bundle-").delete()

    # 2. Find parent bundle lines by the LIVE product category (the snapshot on
    #    the detail row is stale if the client defined the bundle after the sync)
    parent_details = SaleDetail.objects.filter(
        sale=sale, variant__product__category="Define Bundle"
    ).select_related("variant__product")
    parent_details.exclude(product_category="Define Bundle").update(product_category="Define Bundle")
    
    if not parent_details.exists():
        return

    is_card = sale.payment_type in ('CREDIT_CARD', 'MANUAL_CARD_ENTRY', 'CARD_ON_FILE')

    for parent_detail in parent_details:
        product = parent_detail.variant.product if parent_detail.variant else None
        if not product or not product.json_bundle:
            continue

        parent_qty = parent_detail.sales_units

        for bundle_item in product.json_bundle:
            sub_prod_id = bundle_item.get('product_id')
            unit_price = bundle_item.get('bundle_unitprice', bundle_item.get('unit_price', 0.0))
            sub_product = Product.objects.filter(connection=connection, id=sub_prod_id).first()
            if sub_product and sub_product.category != 'Define Bundle':
                sub_variant = sub_product.variants.first()
                if sub_variant:
                    sub_category = f"Bundle-{sub_product.category}" if sub_product.category else "Bundle-Unknown"
                    
                    # Safely sum quantities if the same item is bundled multiple times
                    sub_detail = SaleDetail.objects.filter(sale=sale, variant=sub_variant).first()
                    
                    if sub_detail and not (sub_detail.product_category or "").startswith("Bundle-"):
                        # Same variant also sold as a normal line: (sale, variant) is unique,
                        # so never touch the real line. Skip and log.
                        logger.warning("[BUNDLE] sale %s: variant %s also sold standalone, bundle row skipped",
                                       sale.external_order_id, sub_variant.sku_or_barcode)
                        continue

                    if sub_detail:
                        sub_detail.sales_units += parent_qty
                        sub_detail.save(update_fields=['sales_units'])
                    else:
                        sub_detail = SaleDetail.objects.create(
                            connection=connection,
                            sale=sale,
                            variant=sub_variant,
                            external_order_id=sale.external_order_id,
                            sales_units=parent_qty,
                            price=unit_price,
                            product_title=sub_product.title,
                            product_category=sub_category,
                        )
                    
                    apply_reporting_dates(
                        sale=sale, 
                        detail=sub_detail, 
                        product=sub_product, 
                        sync_started_at=sync_started_at, 
                        is_card_sale=is_card
                    )

def refresh_bundles(connection):
    """Post-sync step: Rebuilds bundle unpacked rows for unreported sales so edits apply retroactively."""
    from django.db.models import Q
    unreported_sales = Sale.objects.filter(
        Q(details__variant__product__category='Define Bundle') |
        Q(details__product_category__startswith='Bundle-'),  # clears orphans if a bundle is un-defined
        connection=connection,
        reported_flag=False,
    ).distinct()
    
    count = 0
    now = timezone.now()
    for sale in unreported_sales:
        unpack_bundles_for_sale(sale, connection, now)
        count += 1
    return {"bundles_unpacked": count}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def refresh_after_sync(connection, today=None):
    """Run every refresh step. A failing step does not stop the others (they
    are independent); the failures are re-raised together at the end so the
    caller does not advance last_sync_at and the whole run is retried."""
    steps = (
        ("identifiers", lambda: refresh_product_identifiers(connection)),
        ("location_locids", lambda: refresh_location_locids(connection)),
        ("bundles", lambda: refresh_bundles(connection)),
        ("sales_summaries", lambda: refresh_sales_summaries(connection)),
        ("reporting_dates", lambda: refresh_reporting_dates(connection, today)),
    )
    stats, errors = {}, []
    for name, fn in steps:
        try:
            with transaction.atomic():
                stats.update(fn())
        except Exception as exc:
            logger.exception(f"[SYNC] post-sync step '{name}' failed for {connection}: {exc}")
            errors.append(f"{name}: {exc}")
    if errors:
        raise RuntimeError("post-sync refresh failed - " + "; ".join(errors))
    return stats