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
class _SydneyTZ(datetime.tzinfo):
    """Built-in Australia/Sydney rules (AEST +10 / AEDT +11; DST from the first
    Sunday in October 02:00 to the first Sunday in April 03:00, in force since
    2008). Used ONLY when the server has no timezone database, so the app does
    not silently fall back to UTC."""
    _STD = datetime.timedelta(hours=10)
    _DST = datetime.timedelta(hours=11)

    @staticmethod
    def _first_sunday(year, month):
        d = datetime.date(year, month, 1)
        return d + datetime.timedelta(days=(6 - d.weekday()) % 7)

    def _is_dst_local(self, dt):
        y = dt.year
        start = datetime.datetime.combine(self._first_sunday(y, 10), datetime.time(2, 0))
        end = datetime.datetime.combine(self._first_sunday(y, 4), datetime.time(3, 0))
        naive = dt.replace(tzinfo=None)
        if naive >= start:
            return True
        if naive < end:
            # 02:00-03:00 on the April changeover day happens twice; the
            # second pass (fold=1) is already standard time.
            return not (dt.fold and naive >= end - datetime.timedelta(hours=1))
        return False

    def utcoffset(self, dt):
        if dt is None:
            return None
        return self._DST if self._is_dst_local(dt) else self._STD

    def dst(self, dt):
        if dt is None:
            return None
        return datetime.timedelta(hours=1) if self._is_dst_local(dt) else datetime.timedelta(0)

    def tzname(self, dt):
        if dt is None:
            return None
        return "AEDT" if self._is_dst_local(dt) else "AEST"

    def fromutc(self, dt):
        y = dt.year
        start_utc = datetime.datetime.combine(
            self._first_sunday(y, 10), datetime.time(2, 0)) - self._STD
        end_utc = datetime.datetime.combine(
            self._first_sunday(y, 4), datetime.time(3, 0)) - self._DST
        naive = dt.replace(tzinfo=None)
        off = self._DST if (naive >= start_utc or naive < end_utc) else self._STD
        local = naive + off
        fold = 0
        if off == self._STD:
            end_local = datetime.datetime.combine(self._first_sunday(y, 4), datetime.time(3, 0))
            if end_local - datetime.timedelta(hours=1) <= local < end_local:
                fold = 1
        return local.replace(tzinfo=self, fold=fold)


_TZ_WARNED = False


def _client_tz():
    global _TZ_WARNED
    if ZoneInfo is not None:
        try:
            return ZoneInfo(CLIENT_TIME_ZONE)
        except Exception:
            pass
    if not _TZ_WARNED:
        _TZ_WARNED = True
        logger.warning(
            "Timezone database not found on this server - using the built-in "
            "%s rules (AEST/AEDT). Installing tzdata is optional.",
            CLIENT_TIME_ZONE)
    return _SydneyTZ()


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

    ARIA additionally needs product.report_to_aria = true.
    CLAU dates have no such conditions.
    """
    # Bundles themselves never get reported. Only their unpacked items do.
    if product.category == "Define Bundle":
        return None, None

    today = today or client_today()
    release = product.release_date

    local_sale_dt = sale_date.astimezone(_client_tz()) if sale_date else None
    local_sale_d = local_sale_dt.date() if local_sale_dt else None

    if release is None:
        aria_base = local_sale_d
        clau = local_sale_d
    elif release <= today:
        aria_base = release
        clau = release
    else:
        return None, None

    aria = None
    if is_card_sale and product.report_to_aria:
        aria = aria_base
    return aria, clau


def _later(new, current):
    """True if `new` should replace `current` as a header date (latest wins)."""
    return current is None or new > current


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
        # The line takes its own date; the sale header only ever moves FORWARD
        # to the latest line date (refresh_reporting_dates re-derives it fully
        # after every sync).
        is_header = obj is sale
        if aria is not None and obj.aria_reporting_date != aria:
            if not is_header or _later(aria, obj.aria_reporting_date):
                obj.aria_reporting_date = aria
                fields.append("aria_reporting_date")
        if clau is not None and obj.clau_reporting_date != clau:
            if not is_header or _later(clau, obj.clau_reporting_date):
                obj.clau_reporting_date = clau
                fields.append("clau_reporting_date")
        if fields:
            obj.save(update_fields=fields)


def is_release_held(product, today=None):
    """True while the product's release date is still in the future: its
    unreported lines must carry NO reporting dates (presale hold)."""
    today = today or client_today()
    return product.release_date is not None and product.release_date > today


def refresh_reporting_dates(connection, today=None):
    """Re-derive reporting dates (and sale.is_presale) for EVERY unreported
    line of the connection, so a release date set / changed after the sync
    treats old and new lines the same:
      - release date in the future -> aria + clau dates are CLEARED (hold)
      - otherwise                  -> dates set per reporting_dates_for()
    """
    today = today or client_today()
    qs = (
        SaleDetail.objects.filter(connection=connection, sale__reported_flag=False)
        .select_related("sale", "variant__product")
        .order_by("sale_id", "created_at")
    )
    sales = {}          # share ONE Sale object per sale across its lines
    presale_sales = set()  # sales with a line sold before its product's release date
    touched_sales, touched_details = set(), []
    held_lines = 0

    # Pass 1: Process lines and determine if the whole sale is held
    sale_dates = {} # sale_id -> {'aria': None, 'clau': None, 'is_held': False}
    for d in qs.iterator(chunk_size=500):
        product = d.variant.product if d.variant_id else None
        if product is None:
            continue
        sale = sales.setdefault(d.sale_id, d.sale)
        if d.sale_id not in sale_dates:
            sale_dates[d.sale_id] = {'aria': None, 'clau': None, 'is_held': False}

        # A Define Bundle line is never reported (only its unpacked items are),
        # so it must carry NO dates, whatever report_to_aria / release_date say.
        # Clear any stamped earlier, e.g. while the line still pointed at a
        # non-bundle product of a previous connection.
        if product.category == "Define Bundle":
            if d.aria_reporting_date is not None or d.clau_reporting_date is not None:
                d.aria_reporting_date = None
                d.clau_reporting_date = None
                touched_details.append(d)
            continue

        local_sale_d = (sale.sale_date.astimezone(_client_tz()).date()
                        if sale.sale_date else None)
        if product.release_date and local_sale_d and product.release_date > local_sale_d:
            presale_sales.add(d.sale_id)
        # Fix: Restore support for MANUAL_CARD_ENTRY and CARD_ON_FILE
        is_card = sale.payment_type in ('CREDIT_CARD', 'MANUAL_CARD_ENTRY', 'CARD_ON_FILE')
        held = is_release_held(product, today)
        
        if held:
            aria, clau = None, None
            held_lines += 1
            sale_dates[d.sale_id]['is_held'] = True
        else:
            aria, clau = reporting_dates_for(product, sale.sale_date, is_card, today)

        changed = False
        if held:
            # presale hold: wipe whatever an earlier sync stamped
            if d.aria_reporting_date is not None:
                d.aria_reporting_date, changed = None, True
            if d.clau_reporting_date is not None:
                d.clau_reporting_date, changed = None, True
        else:
            if aria is not None and d.aria_reporting_date != aria:
                d.aria_reporting_date, changed = aria, True
            if clau is not None and d.clau_reporting_date != clau:
                d.clau_reporting_date, changed = clau, True
        if changed:
            touched_details.append(d)

        # Header date = the LATEST line date (ARIA and CLAU each on their own),
        # so a sale reports once its last item is released. The old "first
        # line wins" rule gave inconsistent headers (and could mix an early
        # CLAU date with a later ARIA date taken from a different line).
        # Bundle lines were skipped above, so only real items count here.
        if aria and (not sale_dates[d.sale_id]['aria'] or aria > sale_dates[d.sale_id]['aria']):
            sale_dates[d.sale_id]['aria'] = aria
        if clau and (not sale_dates[d.sale_id]['clau'] or clau > sale_dates[d.sale_id]['clau']):
            sale_dates[d.sale_id]['clau'] = clau

    # Pass 2: Apply dates to the Sale headers
    for sale_id, dates in sale_dates.items():
        sale = sales[sale_id]
        
        if dates['is_held']:
            if sale.aria_reporting_date is not None:
                sale.aria_reporting_date = None
                touched_sales.add(sale.pk)
            if sale.clau_reporting_date is not None:
                sale.clau_reporting_date = None
                touched_sales.add(sale.pk)
        else:
            if dates['aria'] is not None and sale.aria_reporting_date != dates['aria']:
                sale.aria_reporting_date = dates['aria']
                touched_sales.add(sale.pk)
            if dates['clau'] is not None and sale.clau_reporting_date != dates['clau']:
                sale.clau_reporting_date = dates['clau']
                touched_sales.add(sale.pk)

    # is_presale: sold before a release date (or a webstore order, as the
    # platform sync already flags). Recomputed for every unreported sale seen.
    presale_changed = []
    for pk, sale in sales.items():
        want = (pk in presale_sales) or sale.sale_channel == "WEBSTORE"
        if sale.is_presale != want:
            sale.is_presale = want
            presale_changed.append(sale)

    SaleDetail.objects.bulk_update(
        touched_details, ["aria_reporting_date", "clau_reporting_date"], batch_size=500)
    Sale.objects.bulk_update(
        [sales[pk] for pk in touched_sales],
        ["aria_reporting_date", "clau_reporting_date"], batch_size=500)
    if presale_changed:
        Sale.objects.bulk_update(presale_changed, ["is_presale"], batch_size=500)
    return {"sale_lines_dated": len(touched_details), "sales_dated": len(touched_sales),
            "lines_held": held_lines, "sales_flagged_presale": len(presale_changed)}


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


def build_formatted_address(city, province, postal_code, address1=None):
    """Display address from the platform's suburb / state / postcode fields in
    the locality format used everywhere else, e.g. 'FITZROY VIC 3065'.
    Prefers the StandardLocality text (same wording the predictive menu gives)
    when suburb + postcode match one."""
    match = find_locality(city, province, postal_code)
    if match and match.locality:
        return match.locality
    parts = [(city or "").strip().upper(), _state_code(province) or "",
             (postal_code or "").strip()]
    built = " ".join(p for p in parts if p)
    # Platform gave no suburb/state/postcode (common for Square locations):
    # fall back to the street line so the field is not left blank.
    return built or (address1 or "").strip()


def refresh_location_addresses(connection):
    """Fill formatted_address ONLY where it has never been populated. A value
    already there (typed by the client or picked from the predictive menu) is
    never overwritten."""
    filled = 0
    qs = SalesLocation.objects.filter(connection=connection).filter(
        Q(formatted_address__isnull=True) | Q(formatted_address=""))
    for loc in qs:
        addr = build_formatted_address(loc.city, loc.province, loc.postal_code, loc.address1)
        if not addr:
            continue
        loc.formatted_address = addr
        loc.save(update_fields=["formatted_address", "updated_at"])
        filled += 1
    return {"locations_address_filled": filled}


# --------------------------------------------------------------------------
# Sale ownership (Sale.external_order_id is unique across ALL connections)
# --------------------------------------------------------------------------
def other_connection_owns_sale(connection, order_id):
    """True when this order already belongs to ANOTHER connection that `connection`
    must not take over - so the sync skips it instead of re-stamping the sale:
      * an INACTIVE connection never takes a sale from another connection
      * an ACTIVE connection may take a sale from an inactive one (a re-connect)
      * two ACTIVE connections: the current owner keeps it (no flip-flopping)
    A sale nobody owns yet is created normally."""
    owner = (
        Sale.objects.filter(external_order_id=order_id)
        .exclude(connection_id=connection.pk)
        .select_related("connection").first()
    )
    if owner is None:
        return False
    if not connection.is_active:
        return True
    return owner.connection.is_active


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
    # Structural rule: Never let live product edits override historical bundle markers
    saved_cat = detail.product_category or ""
    if saved_cat.startswith("Bundle-") or saved_cat == "Define Bundle":
        return saved_cat
    
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
        Sale.objects.filter(connection__in=connections, connection__is_active=True)
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
        local_dt = s.sale_date.astimezone(_client_tz()) if s.sale_date else None
        day = local_dt.date() if local_dt else None
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
    # An inactive (disconnected / replaced) connection keeps syncing, but it must
    # never create summary rows - the sales belong to the client's active connection.
    if not connection.is_active:
        return {"summary_lines": 0, "summaries_skipped_inactive": 1}
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

def refresh_line_categories(connection):
    """Post-sync step: bring the category/title snapshot on EVERY unreported
    sale line back in line with its product. Platform sync only re-saves
    orders inside the current ARIA period, so category changes made on the
    Products tab never reached older unreported lines.
    Bundle- lines are rebuilt by refresh_bundles; Define Bundle parents are
    fixed by unpack_bundles_for_sale, so both are left alone here."""
    qs = (
        SaleDetail.objects
        .filter(connection=connection, sale__reported_flag=False)
        .exclude(product_category__startswith="Bundle-")
        .select_related("variant__product")
    )
    changed = []
    for d in qs.iterator(chunk_size=500):
        product = d.variant.product if d.variant_id else None
        if product is None:
            continue
        dirty = False
        if d.product_category != product.category:
            d.product_category, dirty = product.category, True
        if product.title and d.product_title != product.title:
            d.product_title, dirty = product.title, True
        if dirty:
            changed.append(d)
    SaleDetail.objects.bulk_update(
        changed, ["product_category", "product_title"], batch_size=500)
    return {"line_categories_refreshed": len(changed)}


def refresh_bundles(connection):
    """Post-sync step: Rebuilds bundle unpacked rows for unreported sales so edits apply retroactively."""
    from django.db.models import Q
    
    # Client rule: Clear json_bundle during sync when the category isn't Define Bundle
    Product.objects.filter(connection=connection).exclude(category='Define Bundle').exclude(json_bundle__isnull=True).update(json_bundle=None)

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
        ("location_addresses", lambda: refresh_location_addresses(connection)),
        ("line_categories", lambda: refresh_line_categories(connection)),
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