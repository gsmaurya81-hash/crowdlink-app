"""
square_integration/services/square_catalog.py

Square sync engine implementing the two client rules
(see core_api/utils/sync_common.py for the summary).

Square API notes that shape the implementation:
  * orders/search accepts max 10 location_ids per call and is paginated
    (the old code sent all IDs at once and ignored the cursor).
  * an order search can filter on created_at OR updated_at (sort must match).
  * catalog/search has a `begin_time` ("modified after") filter -> used for
    incremental catalog sync. Items carry their variations nested inline.
  * /v2/locations exposes created_at but NO updated_at, so for the daily
    sync locations are re-read (tiny list) and upserted idempotently.

Every network failure RAISES so last_sync_at is not advanced on a bad run.
"""
import logging
import os
import time
from datetime import timedelta

import requests
from django.conf import settings
from django.utils.dateparse import parse_datetime

from .square_oauth import get_valid_access_token

from core_api.models import Product, Sale, SaleDetail, Variant
from core_api.utils.aria import get_current_aria_week_start
from core_api.utils.barcodes import unique_variant_by_sku
from core_api.utils.post_sync import other_connection_owns_sale
from core_api.utils.product_sync import (
    sync_upsert_product,
    MODE_INITIAL, NEW_CONNECTION_RECORD_DAYS, NEW_CONNECTION_SALES_DAYS,
    apply_reporting_dates, iso_z, presale_backfill_products, presale_backfill_start,
    resolve_actual_location,
    upsert_location,
)
from django.db.models import Min

logger = logging.getLogger("square_integration")

HTTP_TIMEOUT = 30
MAX_RETRIES = 4
LOCATIONS_PER_ORDER_SEARCH = 10   # Square hard limit


class SquareAuthError(RuntimeError):
    """Square answered 401/403: the token is rejected. Retrying cannot help."""


def _base_url():
    env = getattr(settings, "SQUARE_ENVIRONMENT", os.environ.get("SQUARE_ENVIRONMENT", "sandbox")).lower()
    return "https://connect.squareup.com" if env == "production" else "https://connect.squareupsandbox.com"


def _request(method, path, token, **kwargs):
    url = f"{_base_url()}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.request(method, url, headers=headers, timeout=HTTP_TIMEOUT, **kwargs)
            if resp.status_code in (401, 403):
                # Fail fast - no point retrying (and sleeping) on a bad token.
                raise SquareAuthError(
                    f"Square rejected the access token ({resp.status_code}) for {path}")
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = requests.HTTPError(f"{resp.status_code} from {path}")
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        except SquareAuthError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Square request failed after {MAX_RETRIES} attempts: {last_exc}")


# --------------------------------------------------------------------------
# Fetchers
# --------------------------------------------------------------------------
def fetch_locations(token):
    return _request("GET", "/v2/locations", token).get("locations", [])


def fetch_orders(token, location_ids, *, since, by):
    """by = 'created_at' (initial) or 'updated_at' (incremental)."""
    sort_field = "CREATED_AT" if by == "created_at" else "UPDATED_AT"
    orders = []
    for i in range(0, len(location_ids), LOCATIONS_PER_ORDER_SEARCH):
        chunk = location_ids[i:i + LOCATIONS_PER_ORDER_SEARCH]
        cursor = None
        while True:
            payload = {
                "location_ids": chunk,
                "limit": 500,
                "query": {
                    "filter": {
                        "state_filter": {"states": ["COMPLETED"]},
                        "date_time_filter": {by: {"start_at": since.isoformat()}},
                    },
                    "sort": {"sort_field": sort_field, "sort_order": "ASC"},
                },
            }
            if cursor:
                payload["cursor"] = cursor
            data = _request("POST", "/v2/orders/search", token, json=payload)
            orders.extend(data.get("orders", []))
            cursor = data.get("cursor")
            if not cursor:
                break
    return orders


def _images(objs):
    """{image id: url} for IMAGE objects, plus {"tax:<id>": (rate %, inclusive?)} for TAX
    objects. The tax entries ride in the same dict so every fetch path that already
    returns `images` also delivers the tax rates needed to turn prices ex-tax."""
    out = {o["id"]: (o.get("image_data") or {}).get("url") for o in objs if o.get("type") == "IMAGE"}
    for o in objs:
        if o.get("type") == "TAX":
            td = o.get("tax_data") or {}
            try:
                rate = float(td.get("percentage"))
            except (TypeError, ValueError):
                continue
            out[f"tax:{o['id']}"] = (rate, td.get("inclusion_type") == "INCLUSIVE")
    return out


def variation_price_ex_tax(item_data, vd, images):
    """Ex-tax price (dollars) of a Square variation, or None when it cannot be known.
    Square catalog prices exclude tax unless the item's tax is INCLUSIVE, in which case
    the rate is divided out. Variable-price variations and items whose tax object we
    could not load give None (the stored price is left alone)."""
    money = vd.get("price_money") or {}
    if vd.get("pricing_type") == "VARIABLE_PRICING" or money.get("amount") is None:
        return None
    price = float(money["amount"]) / 100.0
    for tax_id in item_data.get("tax_ids") or []:
        tax = images.get(f"tax:{tax_id}")
        if tax is None:
            return None
        rate, inclusive = tax
        if inclusive:
            price = price / (1.0 + rate / 100.0)
    return round(price, 2)


def fetch_all_items(token):
    """Full catalog (ITEM + IMAGE). Returns (items, images)."""
    objs, cursor = [], None
    while True:
        params = {"types": "ITEM,IMAGE,TAX"}
        if cursor:
            params["cursor"] = cursor
        data = _request("GET", "/v2/catalog/list", token, params=params)
        objs.extend(data.get("objects", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return [o for o in objs if o["type"] == "ITEM"], _images(objs)


def fetch_items_changed_since(token, begin_time):
    items, images, cursor = [], {}, None
    while True:
        body = {"object_types": ["ITEM"], "begin_time": iso_z(begin_time),
                "include_related_objects": True, "limit": 1000}
        if cursor:
            body["cursor"] = cursor
        data = _request("POST", "/v2/catalog/search", token, json=body)
        items.extend(data.get("objects", []))
        images.update(_images(data.get("related_objects", [])))
        cursor = data.get("cursor")
        if not cursor:
            return items, images


def _batch_retrieve(token, ids):
    objs, related = [], []
    ids = list(ids)
    for i in range(0, len(ids), 100):
        data = _request("POST", "/v2/catalog/batch-retrieve", token,
                        json={"object_ids": ids[i:i + 100], "include_related_objects": True})
        objs.extend(data.get("objects", []))
        related.extend(data.get("related_objects", []))
    return objs, related


def fetch_items_for_variations(token, variation_ids):
    """Resolve variation IDs (from order line items) to full parent ITEMs."""
    if not variation_ids:
        return [], {}
    objs, _ = _batch_retrieve(token, variation_ids)
    item_ids = {o["item_variation_data"]["item_id"] for o in objs
                if o.get("type") == "ITEM_VARIATION" and o.get("item_variation_data", {}).get("item_id")}
    if not item_ids:
        return [], {}
    items, related = _batch_retrieve(token, item_ids)
    return [o for o in items if o["type"] == "ITEM"], _images(related)


# --------------------------------------------------------------------------
# Upserts
# --------------------------------------------------------------------------
def _upsert_item(connection, item, images):
    item_id = item["id"]
    item_data = item.get("item_data") or {}
    title = item_data.get("name", "Unknown Item")
    created = item.get("created_at")
    created_dt = parse_datetime(created) if created else None
    channel = "Webstore" if item_data.get("available_online") is True else "POS"
    count = 0
    for var in item_data.get("variations", []):
        vd = var.get("item_variation_data") or {}
        sku_or_barcode = vd.get("upc") or vd.get("sku") or ""
        missing = False
        if not sku_or_barcode:
            sku_or_barcode, missing = f"MISSING-SQUARE-{item_id}", True

        image_ids = vd.get("image_ids") or item_data.get("image_ids") or []
        image_url = images.get(image_ids[0]) if image_ids else None
        if not image_url and item_data.get("ecom_image_uris"):
            image_url = item_data["ecom_image_uris"][0]

        sync_upsert_product(
            connection=connection, sku_or_barcode=sku_or_barcode,
            defaults={
                "title": title, "variant_title": vd.get("name") or title,
                "has_missing_identifier": missing,
                "external_catalog_id": var.get("id"), "external_product_id": item_id,
                "image_url": image_url, "channel": channel,
                "webstore_creation_date": created_dt,
                "price": variation_price_ex_tax(item_data, vd, images),
            },
        )
        count += 1
    return count


def process_orders(connection, orders, sync_started_at):
    aria_start = get_current_aria_week_start()
    saved = skipped = 0
    processed_sales = set()
    for order in orders:
        created_str = order.get("created_at")
        if not created_str:
            continue
        created_dt = parse_datetime(created_str)
        order_id = str(order.get("id", ""))
        # An order that already belongs to ANOTHER connection is not ours to take over:
        # skip it (its owner's own sync keeps it current).
        if other_connection_owns_sale(connection, order_id):
            skipped += len(order.get("line_items", []))
            continue
        source_name = (order.get("source") or {}).get("name", "")
        is_web = "square online" in source_name.lower()
        sale_channel = "WEBSTORE" if is_web else "POS"
        system_loc = "Square Webstore" if is_web else "Square POS"
        actual_loc = resolve_actual_location(connection, order.get("location_id"), not is_web)

        tenders = order.get("tenders") or []
        payment_type = "OTHER_USER_DEFINED"
        if tenders:
            t = tenders[0].get("type", "")
            payment_type = {"CASH": "CASH", "CARD": "CREDIT_CARD",
                            "SQUARE_GIFT_CARD": "GIFT_CARD_REDEMPTION"}.get(t, "OTHER_USER_DEFINED")
                            
        # Client Rule: Group any form of card payment as 'Card'
        is_card = payment_type in ('CREDIT_CARD', 'MANUAL_CARD_ENTRY', 'CARD_ON_FILE')
        sale_type = f"{system_loc.replace(' ', '-')}-{'Card' if is_card else 'Other'}"

        order_sale = None
        order_variant_ids = []
        for item in order.get("line_items", []):
            cat_id = item.get("catalog_object_id", "")
            sku = item.get("note", "")
            if not sku and not cat_id:
                skipped += 1
                continue
            variant = None
            if cat_id:
                variant = Variant.objects.filter(connection=connection, external_catalog_id=cat_id).select_related("product").first()
            if not variant and sku:
                # Only when the SKU is unambiguous: several products may share it, and the
                # line must never be guessed onto one of them.
                variant = unique_variant_by_sku(connection, sku)
            if not variant:
                logger.warning(f"[SYNC] Square order {order_id}: no variant for '{cat_id}' / '{sku}'")
                skipped += 1
                continue
            order_variant_ids.append(variant.pk)

            product = variant.product
            price = float((item.get("base_price_money") or {}).get("amount", 0)) / 100.0
            sale, _ = Sale.objects.update_or_create(
                external_order_id=order_id,
                defaults={
                    "connection": connection, "sale_date": created_dt,
                    "system_location": system_loc, "actual_location": actual_loc or system_loc,
                    "sale_channel": sale_channel, "is_presale": is_web,
                    "payment_type": payment_type, "type": sale_type,
                    "source_platform": "Square",
                    "is_within_current_aria_period": bool(created_dt and created_dt >= aria_start),
                },
            )
            processed_sales.add(sale)
            order_sale = sale

            detail, _ = SaleDetail.objects.update_or_create(
                sale=sale, variant=variant,
                defaults={
                    "connection": connection, "external_order_id": order_id,
                    "sales_units": int(item.get("quantity", 1)), "price": price,
                    "product_title": product.title, "product_category": product.category,
                },
            )
            
            # Client Requirement: Only Card sales are reported to ARIA.
            apply_reporting_dates(
                sale=sale, 
                detail=detail, 
                product=product, 
                sync_started_at=sync_started_at, 
                is_card_sale=is_card
            )
            saved += 1

        # A re-connected store: this sale may still carry lines on the OLD connection's
        # variants. Now that this order's own lines are saved, fold those away so
        # each item appears once (see core_api/utils/line_cleanup.py).
        if order_sale is not None:
            from core_api.utils.line_cleanup import cleanup_sale_lines, drop_misattributed_lines
            cleanup_sale_lines(order_sale)
            drop_misattributed_lines(order_sale, order_variant_ids)

        # Client Rule: Hand off to the shared bundle helper once per order
        from core_api.utils.post_sync import unpack_bundles_for_sale
        for sale in processed_sales:
            unpack_bundles_for_sale(sale, connection, sync_started_at)

    return saved, skipped


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def run_sync(connection, mode, since, started_at):
    # Renews the token first when it is close to expiry (Square tokens last 30 days).
    token = get_valid_access_token(connection)
    stats = {"mode": mode}
    record_cutoff = started_at - timedelta(days=NEW_CONNECTION_RECORD_DAYS)

    # Location IDs are needed just to *query* orders (Square requires them),
    # so read the list once in memory; only wanted ones are persisted below.
    try:
        api_locations = fetch_locations(token)
    except SquareAuthError:
        # Token rejected despite looking valid: renew once and retry. If that
        # fails too, the error says the store must reconnect.
        token = get_valid_access_token(connection, force=True)
        api_locations = fetch_locations(token)
    all_location_ids = [l["id"] for l in api_locations]
    if not all_location_ids:
        raise RuntimeError(f"No Square locations returned for {connection.store_identifier}")

    # ---- 1. Sales ---------------------------------------------------------
    if mode == MODE_INITIAL:
        sales_from = started_at - timedelta(days=NEW_CONNECTION_SALES_DAYS)
        orders = fetch_orders(token, all_location_ids, since=sales_from, by="created_at")
    else:
        sales_from = since
        orders = fetch_orders(token, all_location_ids, since=since, by="updated_at")

    # Sync 4 (client spec): release_date >= sync date - 7 AND
    # all_presales_loaded = false -> load ALL sales back to webstore_creation_date.
    presale_qs = presale_backfill_products(connection)
    oldest_presale = presale_backfill_start(presale_qs, started_at)
    if oldest_presale and oldest_presale < sales_from:
        seen = {o.get("id") for o in orders}
        for o in fetch_orders(token, all_location_ids, since=oldest_presale, by="created_at"):
            if o.get("id") not in seen:
                orders.append(o)
    stats["orders_fetched"] = len(orders)

    ref_locations = {o["location_id"] for o in orders if o.get("location_id")}
    ref_variations = {li["catalog_object_id"] for o in orders
                      for li in o.get("line_items", []) if li.get("catalog_object_id")}

    # ---- 2. Locations -----------------------------------------------------
    loc_count = 0
    for loc in api_locations:
        created = parse_datetime(loc["created_at"]) if loc.get("created_at") else None
        if mode == MODE_INITIAL:
            wanted = loc["id"] in ref_locations or bool(created and created >= record_cutoff)
        else:
            wanted = True   # Square gives no updated_at for locations (see module doc)
        if not wanted:
            continue
        addr, coords = loc.get("address") or {}, loc.get("coordinates") or {}
        upsert_location(
            connection, platform="SQUARE", external_id=loc["id"], name=loc.get("name"),
            address1=addr.get("address_line_1"), city=addr.get("locality"),
            province=addr.get("administrative_district_level_1"),
            postal_code=addr.get("postal_code"), country=addr.get("country"),
            lat=coords.get("latitude"), lng=coords.get("longitude"),
            is_active=loc.get("status") == "ACTIVE",
        )
        loc_count += 1
    stats["locations_synced"] = loc_count

    # ---- 3. Products ------------------------------------------------------
    var_count = 0
    if mode == MODE_INITIAL:
        items, images = fetch_all_items(token)
        for item in items:
            nested_ids = {v["id"] for v in (item.get("item_data") or {}).get("variations", [])}
            created = parse_datetime(item["created_at"]) if item.get("created_at") else None
            is_recent = bool(created and created >= record_cutoff)
            is_referenced = bool(nested_ids & ref_variations)
            if is_recent or is_referenced:
                var_count += _upsert_item(connection, item, images)
    else:
        items, images = fetch_items_changed_since(
            token, getattr(connection, "_catalog_since", None) or since)
        for item in items:
            var_count += _upsert_item(connection, item, images)

    # Any variation a sale points to must exist locally, whatever its age.
    have = set(Variant.objects.filter(connection=connection, external_catalog_id__in=ref_variations)
               .values_list("external_catalog_id", flat=True))
    items, images = fetch_items_for_variations(token, ref_variations - have)
    for item in items:
        var_count += _upsert_item(connection, item, images)
    stats["variants_synced"] = var_count

    # ---- 4. Save sales ----------------------------------------------------
    saved, skipped = process_orders(connection, orders, started_at)
    stats.update(sale_lines_saved=saved, sale_lines_skipped=skipped)

    if presale_qs.exists():
        presale_qs.update(all_presales_loaded=True)
    return stats