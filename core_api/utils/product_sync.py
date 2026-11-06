"""
core_api/utils/product_sync.py

Shared helper for all Shopify and Square sync paths.

SOW Stage 2 constraint: the three CrowdLink-owned Product fields
  - category
  - release_date
  - report_to_aria
must NEVER be overwritten by a platform sync, even if a caller accidentally
includes them in the `defaults` dict. This function is the single call site
for all product upserts; it strips those keys before touching the database,
making the protection structural rather than relying on every sync author
remembering to omit them.
"""

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Min

from core_api.models import Product, Variant

# Fields that belong to CrowdLink and must survive every sync unchanged.
_CROWDLINK_OWNED_FIELDS = frozenset({"category", "release_date", "report_to_aria", "json_bundle"})


def to_price(value):
    """Platform price -> Decimal(2dp), or None when missing / not a number."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None


def sync_upsert_product(connection, sku_or_barcode, defaults):
    """
    Create or update a parent Product row and its child Variant row from platform sync data.
    Ensures 1 row per parent product in Product, and 1 row per platform variant id in Variant
    (several variants/products may share a SKU/barcode - see core_api/utils/barcodes.py).
    """
    safe_defaults = {k: v for k, v in defaults.items() if k not in _CROWDLINK_OWNED_FIELDS}

    external_product_id = safe_defaults.get("external_product_id") or sku_or_barcode
    external_catalog_id = safe_defaults.get("external_catalog_id") or None   # never ''
    has_missing_identifier = safe_defaults.get("has_missing_identifier", False)
    title = safe_defaults.get("title", "Unknown Product")
    image_url = safe_defaults.get("image_url")
    channel = safe_defaults.get("channel", "POS")
    webstore_creation_date = safe_defaults.get("webstore_creation_date")
    variant_title = safe_defaults.get("variant_title") or title
    # Ex-tax price. None = the platform gave no usable price: keep what we already hold.
    price = to_price(safe_defaults.get("price"))

    # Placeholder identifiers (MISSING-...) exist only as the Variant unique key.
    # They must never be shown on / stored in Product.sku_or_barcode (null instead).
    product_defaults = {
        "title": title,
        "image_url": image_url,
        "channel": channel,
    }
    if webstore_creation_date is not None:
        product_defaults["webstore_creation_date"] = webstore_creation_date

    # 1. Upsert Parent Product (1 row per external_product_id)
    product, created = Product.objects.update_or_create(
        connection=connection,
        external_product_id=external_product_id,
        defaults=product_defaults,
    )

    # 2. Upsert Child Variant (1 row per platform variant id, linked to parent Product).
    # Variants with no real identifier store sku_or_barcode = NULL and are matched
    # by their platform id (external_catalog_id) instead of a MISSING-* placeholder.
    variant_fields = {
        "product": product,
        "external_catalog_id": external_catalog_id,
        "external_product_id": external_product_id,
        "has_missing_identifier": has_missing_identifier,
        "title": variant_title,
        "image_url": image_url,
    }
    if price is not None:
        variant_fields["price"] = price
    # The look-up-then-create below is NOT atomic. Shopify fires products/create
    # and products/update back to back, and the daily / manual sync can overlap
    # them, so two callers used to both find "no variant" and both insert one
    # (a NULL sku does not trip the (connection, sku) unique key). Locking the
    # parent Product row serialises every writer of that product's variants.
    with transaction.atomic():
        Product.objects.select_for_update().get(pk=product.pk)

        # A variant is identified by its PLATFORM id only. Never by SKU/barcode: two
        # products may share a barcode and each must keep its own variant and sales
        # (matching on the barcode used to hand one product's variant to the other).
        # One narrow exception: an old row that never got a platform id is adopted when
        # it sits under this same parent product and carries this SKU.
        variant = None
        if external_catalog_id:
            variant = Variant.objects.filter(
                connection=connection, external_catalog_id=external_catalog_id).first()
            if variant is None and not has_missing_identifier:
                legacy = list(Variant.objects.filter(
                    connection=connection, product=product, external_catalog_id__isnull=True,
                    sku_or_barcode=sku_or_barcode)[:2])
                variant = legacy[0] if len(legacy) == 1 else None
        elif not has_missing_identifier:
            # No platform id at all (not something Shopify/Square send): SKU, if unambiguous.
            from core_api.utils.barcodes import unique_variant_by_sku
            variant = unique_variant_by_sku(connection, sku_or_barcode)
        # Variants with no real identifier store sku_or_barcode = NULL (matched by
        # platform id above); everything else keeps its SKU/barcode.
        variant_fields["sku_or_barcode"] = None if has_missing_identifier else sku_or_barcode

        if variant is None:
            Variant.objects.create(connection=connection, **variant_fields)
        else:
            for k, v in variant_fields.items():
                setattr(variant, k, v)
            variant.save()

        # Product price = lowest variant price (the same value for one-variant products)
        low = product.variants.aggregate(low=Min("price"))["low"]
        if low is not None and product.price != low:
            product.price = low
            product.save(update_fields=["price"])

        # 3. Product-level identifier = ALL real variant SKUs combined (placeholders
        # excluded). Recomputed here so the webhook path (which does not run the
        # post-sync refresh) stays correct; refresh_product_identifiers() applies
        # the same rule after every full sync.
        combined = combine_variant_skus(product.variants.all())
        missing = combined is None
        if product.combined_sku_bar != combined or product.has_missing_identifier != missing:
            product.combined_sku_bar, product.has_missing_identifier = combined, missing
            product.save(update_fields=["combined_sku_bar", "has_missing_identifier"])

    return product, created


import logging
import threading
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from core_api.utils.post_sync import (  # noqa: F401  (apply_reporting_dates re-exported)
    apply_reporting_dates, client_today, combine_variant_skus, refresh_after_sync,
)

# ==========================================================================
#  SYNC RULES (client spec)
#    1. NEW connection (connection.last_sync_at is NULL):
#         - sales created in the last NEW_CONNECTION_SALES_DAYS (30) days
#         - every product + location REFERENCED by those sales (any age)
#         - every product + location CREATED on the platform in the last
#           NEW_CONNECTION_RECORD_DAYS (60) days
#    2. EXISTING connection (daily auto sync):
#         - sales / products / locations whose platform updated_at is later
#           than connection.last_sync_at
#    3. MANUAL sync (Sync tab): same as 2 but updated_at > connection.created_at
#    4. HISTORICAL PRESALE: while product.release_date >= today - 7 days and
#       product.all_presales_loaded is false, ALL sales for it are loaded back
#       to product.webstore_creation_date (see presale_backfill_products)
#    5. Every sync type ends with core_api/utils/post_sync.refresh_after_sync()
# ==========================================================================

NEW_CONNECTION_SALES_DAYS = 30
NEW_CONNECTION_RECORD_DAYS = 60

# Safety margin when reading the watermark: we ask the platform for
# "updated after (last_sync_at - 5 min)". Upserts are idempotent, so
# re-reading a few minutes of data is harmless, whereas clock skew between
# our server and Shopify/Square could otherwise silently lose changes.
WATERMARK_OVERLAP = timedelta(minutes=5)

MODE_INITIAL = "initial"
MODE_INCREMENTAL = "incremental"
# Manual "refresh" resync: re-pulls everything the platform has touched since
# the connection was first created. Same fetch path as MODE_INCREMENTAL (the
# engines only special-case MODE_INITIAL) - only `since` differs. Exists so a
# client whose daily sync silently failed for a while can be made whole again
# with one click, without re-running the heavier 30d/60d "new connection" rule.
MODE_REFRESH = "refresh"


# A release date up to 6 days old still has to report on day 7, so its
# presales must still be pulled: release_date >= sync date - 7.
PRESALE_LOOKBACK_DAYS = 7

# Product has no webstore_creation_date (legacy / POS-created product): pull
# its historical presales back this many days instead (client answer: 90).
PRESALE_DEFAULT_LOOKBACK_DAYS = 90


def presale_backfill_products(connection, today=None):
    """Products whose historical presales still need loading:
         release_date >= sync date - 7  AND  all_presales_loaded = false
    Old release dates (clients back-dating older products) fall outside the
    window on purpose - those sales are not wanted. How far back the sales are
    pulled: see presale_backfill_start()."""
    from core_api.models import Product

    today = today or client_today()
    return Product.objects.filter(
        connection=connection,
        all_presales_loaded=False,
        release_date__isnull=False,
        release_date__gte=today - timedelta(days=PRESALE_LOOKBACK_DAYS),
    )


def presale_backfill_start(presale_qs, started_at):
    """Oldest date to pull sales from for the given presale products: each
    product's webstore_creation_date, or started_at - 90 days when it has none.
    Returns None when there is nothing to backfill."""
    dates = list(presale_qs.values_list("webstore_creation_date", flat=True))
    if not dates:
        return None
    fallback = started_at - timedelta(days=PRESALE_DEFAULT_LOOKBACK_DAYS)
    return min(d or fallback for d in dates)


def iso_z(dt):
    """UTC 'YYYY-MM-DDTHH:MM:SSZ' - the format both APIs accept in filters."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_actual_location(connection, external_location_id, channel_is_pos):
    """Same rule the old sync used: 'City STATE' if known, else location name."""
    from core_api.models import SalesLocation

    if not external_location_id or not channel_is_pos:
        return None
    loc = SalesLocation.objects.filter(
        connection=connection, external_location_id=str(external_location_id)
    ).first()
    if not loc:
        return None
    combined = f"{loc.city or ''} {loc.province or ''}".strip()
    return combined if combined else loc.name


# apply_reporting_dates() now lives in core_api/utils/post_sync.py (re-exported
# here so the engines and webhooks keep importing it from this module).


def upsert_location(connection, *, platform, external_id, name, address1, city,
                    province, postal_code, country, lat, lng, is_active):
    """Create a SalesLocation, or for an existing one ONLY refresh is_active
    (client requirement: never overwrite user-edited location data)."""
    from decimal import Decimal
    from core_api.models import SalesLocation, StandardLocality

    locid_match = std_lat = std_lng = None
    fallback_formatted_address = ""
    
    if city and postal_code:
        match = StandardLocality.objects.filter(
            suburb__iexact=city.strip(), postcode=postal_code.strip()
        ).first()
        if match:
            locid_match, std_lat, std_lng = match.locid, match.latitude, match.longitude
            fallback_formatted_address = match.locality or ""

    # No locality match: build "Suburb, STATE Postcode" from the platform fields
    if not fallback_formatted_address:
        from core_api.utils.post_sync import build_formatted_address
        fallback_formatted_address = build_formatted_address(city, province, postal_code, address1)

    final_lat = Decimal(str(lat)) if lat is not None else std_lat
    final_lng = Decimal(str(lng)) if lng is not None else std_lng

    existing = SalesLocation.objects.filter(
        connection=connection, external_location_id=str(external_id)
    ).first()
    if existing is None:
        SalesLocation.objects.create(
            connection=connection, external_location_id=str(external_id),
            platform=platform, name=name or "Unknown Location",
            formatted_address=fallback_formatted_address,
            address1=address1 or "", city=city or "", province=province or "",
            postal_code=postal_code or "", country=country or "",
            latitude=final_lat, longitude=final_lng, locid=locid_match,
            is_active=is_active,
        )
        return "created"
    # Only refresh is_active, plus formatted_address if it was never populated
    update_fields = []
    if existing.is_active != is_active:
        existing.is_active = is_active
        update_fields.append("is_active")
    if not existing.formatted_address and fallback_formatted_address:
        existing.formatted_address = fallback_formatted_address
        update_fields.append("formatted_address")
    if update_fields:
        existing.save(update_fields=update_fields)
        return "updated"
    return "unchanged"


# ==========================================================================
#  SYNC RUNNER  (single entry point: OAuth callbacks, cron command, resync endpoints)
#
#  Watermark contract: last_sync_at NULL -> MODE_INITIAL; otherwise MODE_INCREMENTAL
#  (updated_at > last_sync_at). last_sync_at is set to the run's START time and
#  ONLY when every step succeeded, so a failed run retries the same window.
# ==========================================================================
logger = logging.getLogger("shopify_integration")  # writes to logs/sync.log

LOCK_SECONDS = 60 * 60


def _engine_for(connection):
    if connection.platform == "SHOPIFY":
        from shopify_integration.services import shopify_catalog as engine
    elif connection.platform == "SQUARE":
        from square_integration.services import square_catalog as engine
    else:
        raise ValueError(f"Unsupported platform {connection.platform}")
    return engine


def run_connection_sync(connection, force_initial=False, refresh=False):
    """
    force_initial=True : re-run the "new connection" 30d-sales / 60d-records
                          rule from scratch, as if this were day one.
    refresh=True        : manual "refresh" resync (existing connection only).
                          Re-fetches sales/products/locations whose updated_at
                          is greater than connection.created_at - i.e. every
                          change the platform has ever recorded for this
                          connection - so a connection whose daily sync has
                          been failing quietly can be brought back in sync
                          with a single click. force_initial takes priority
                          if both are passed.
    Neither flag        : normal daily incremental sync from last_sync_at.
    """
    lock_key = f"sync_lock_{connection.pk}"
    # cache.add is atomic: prevents cron + OAuth callback + manual resync
    # from running the same connection twice at once.
    if not cache.add(lock_key, "1", LOCK_SECONDS):
        logger.warning(f"[SYNC] {connection} already syncing - skipped")
        return {"status": "skipped", "reason": "sync already running"}

    started_at = timezone.now()
    try:
        is_new = force_initial or connection.last_sync_at is None
        if is_new:
            mode, since = MODE_INITIAL, None
        elif refresh:
            mode, since = MODE_REFRESH, connection.created_at
        else:
            mode, since = MODE_INCREMENTAL, connection.last_sync_at - WATERMARK_OVERLAP
        # Manual refresh only: if any variant still has no price, re-read the
        # whole catalogue (not just items changed since created_at) so prices
        # backfill. Orders keep their normal window. Variable-price items stay
        # NULL, which only costs a full catalogue read on a manual sync.
        connection._catalog_since = None
        # Same for a product with no variant row at all: before variants were keyed by
        # platform id, a product sharing a barcode lost its variant to the other product.
        # The full read recreates it (and is a no-op for healthy catalogues).
        if mode == MODE_REFRESH and (
                Variant.objects.filter(connection=connection, price__isnull=True).exists()
                or Product.objects.filter(connection=connection, variants__isnull=True).exists()):
            from datetime import datetime, timezone as _tz
            connection._catalog_since = datetime(2000, 1, 1, tzinfo=_tz.utc)
        logger.info(f"[SYNC] START {connection} mode={mode} since={since}")

        stats = _engine_for(connection).run_sync(connection, mode, since, started_at)
        # Item 5 - runs for EVERY sync type; a failure here leaves last_sync_at
        # untouched so the same window is retried.
        stats["post_sync"] = refresh_after_sync(connection)

        if connection.is_active:
            try:
                from core_api.utils.connection_guard import absorb_previous_owners
                absorbed = absorb_previous_owners(connection)
                if absorbed:
                    stats["absorbed_previous_owners"] = absorbed
            except Exception:
                logger.exception(f"[SYNC] absorb_previous_owners failed for {connection}")

        connection.last_sync_at = started_at
        connection.save(update_fields=["last_sync_at"])
        logger.info(f"[SYNC] DONE {connection} {stats}")
        return {"status": "success", **stats}
    except Exception as exc:
        logger.exception(f"[SYNC] FAILED {connection}: {exc}")
        return {"status": "error", "error": str(exc)}
    finally:
        cache.delete(lock_key)


# ==========================================================================
#  MANUAL SYNC (Sync tab -> [Run Manual Sync])
#
#  A sync can take a minute or more - longer than a proxy/Passenger request
#  timeout - so the POST only STARTS it in a background thread (same pattern
#  as the OAuth callback) and the extension polls the status. Status lives in
#  the shared file cache, so any worker process can answer the poll.
# ==========================================================================
MANUAL_SYNC_STATUS_SECONDS = 60 * 30


def _manual_key(email):
    import hashlib
    return "manual_sync_status_" + hashlib.sha1(email.strip().lower().encode()).hexdigest()


def manual_sync_status(email):
    return cache.get(_manual_key(email)) or {"state": "idle"}


def start_manual_sync(email):
    """Returns {"state": "running" | "no_connection", ...}. Idempotent while a
    manual sync for this email is already running."""
    from core_api.models import ClientStoreConnection

    key = _manual_key(email)
    current = cache.get(key)
    if current and current.get("state") == "running":
        return current

    conns = list(ClientStoreConnection.objects.filter(client_email=email, is_active=True))
    if not conns:
        return {"state": "no_connection"}

    running = {"state": "running", "started_at": timezone.now().isoformat()}
    cache.set(key, running, MANUAL_SYNC_STATUS_SECONDS)

    def worker():
        from django.db import connections as db_connections
        results, state = [], "done"
        try:
            for conn in conns:
                # refresh=True -> updated_at > connection.created_at (sync 3)
                res = run_connection_sync(conn, refresh=True)
                results.append({"platform": conn.platform, **res})
                if res.get("status") == "error":
                    state = "error"
        except Exception as exc:  # never leave the status stuck on "running"
            logger.exception(f"[SYNC] manual sync crashed for {email}: {exc}")
            state = "error"
            results.append({"status": "error", "error": str(exc)})
        finally:
            cache.set(key, {
                "state": state, "started_at": running["started_at"],
                "finished_at": timezone.now().isoformat(), "results": results,
            }, MANUAL_SYNC_STATUS_SECONDS)
            db_connections.close_all()

    threading.Thread(target=worker, daemon=True).start()
    return running