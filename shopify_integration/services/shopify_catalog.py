"""
shopify_integration/services/shopify_catalog.py

Shopify sync engine implementing the two client rules
(see core_api/utils/sync_common.py for the summary):

  MODE_INITIAL      new connection  -> 30d of sales
                                       + products/locations referenced by them
                                       + products/locations created in last 60d
  MODE_INCREMENTAL  daily auto sync -> everything with updated_at > since

Every network failure RAISES (nothing is swallowed) so the orchestrator does
not advance connection.last_sync_at and the same window is retried next run.
"""
import logging
import time
from datetime import timedelta

import requests
from django.db.models import Min
from django.utils.dateparse import parse_datetime

from core_api.models import Product, Sale, SaleDetail, Variant
from core_api.utils.aria import get_current_aria_week_start
from core_api.utils.product_sync import (
    sync_upsert_product,
    MODE_INITIAL, NEW_CONNECTION_RECORD_DAYS, NEW_CONNECTION_SALES_DAYS,
    apply_reporting_dates, iso_z, presale_backfill_products, presale_backfill_start,
    resolve_actual_location,
    upsert_location,
)
from .shopify_oauth import get_valid_access_token

logger = logging.getLogger("shopify_integration")

API_VERSION = "2026-07"
HTTP_TIMEOUT = 30
MAX_RETRIES = 4
PRODUCTS_PAGE = 10        # 10 products x 50 variants keeps GraphQL cost < 1000
VARIANTS_PAGE = 50

PRODUCT_FIELDS = """
  id title publishedAt createdAt updatedAt
  featuredImage { url }
  variants(first: %d) {
    pageInfo { hasNextPage endCursor }
    edges { node { id sku barcode title image { url } } }
  }
""" % VARIANTS_PAGE


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def _request(method, url, headers, **kwargs):
    """requests wrapper with bounded retry on 429 / 5xx / network errors."""
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.request(method, url, headers=headers, timeout=HTTP_TIMEOUT, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                logger.warning(f"[SYNC] Shopify {resp.status_code} on {url}; retry in {wait}s")
                time.sleep(wait)
                last_exc = requests.HTTPError(f"{resp.status_code} from {url}")
                continue
            if resp.status_code in (401, 403):
                # Token revoked / app uninstalled / missing scope: retrying
                # cannot help. Fail fast with a clear, actionable message.
                raise RuntimeError(
                    f"Shopify rejected the access token ({resp.status_code}) for {url} - "
                    "the store must reconnect the app (reinstall / re-authorise).")
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Shopify request failed after {MAX_RETRIES} attempts: {last_exc}")


def _gql(connection, token, query, variables=None):
    url = f"https://{connection.store_identifier}/admin/api/{API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    for attempt in range(MAX_RETRIES):
        resp = _request("POST", url, headers, json={"query": query, "variables": variables or {}})
        body = resp.json()
        errors = body.get("errors") or []
        if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
            time.sleep(2 ** attempt)
            continue
        if errors and not body.get("data"):
            raise RuntimeError(f"Shopify GraphQL error: {errors}")
        return body["data"]
    raise RuntimeError("Shopify GraphQL kept returning THROTTLED")


# --------------------------------------------------------------------------
# Fetchers
# --------------------------------------------------------------------------
def fetch_orders(connection, token, *, created_at_min=None, updated_at_min=None):
    url = f"https://{connection.store_identifier}/admin/api/{API_VERSION}/orders.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    params = {"status": "any", "limit": 250}
    if created_at_min:
        params["created_at_min"] = created_at_min.isoformat()
    if updated_at_min:
        params["updated_at_min"] = updated_at_min.isoformat()

    orders, next_url, next_params = [], url, params
    while next_url:
        resp = _request("GET", next_url, headers, params=next_params)
        orders.extend(resp.json().get("orders", []))
        next_url, next_params = None, None
        for part in resp.headers.get("Link", "").split(","):
            if 'rel="next"' in part:
                next_url = part[part.find("<") + 1: part.find(">")]
                break
    return orders


def fetch_locations(connection, token):
    url = f"https://{connection.store_identifier}/admin/api/{API_VERSION}/locations.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    return _request("GET", url, headers).json().get("locations", [])


def _extra_variants(connection, token, product_id, cursor):
    """Products with more than VARIANTS_PAGE variants: page through the rest."""
    q = """query($id: ID!, $cursor: String) { product(id: $id) {
             variants(first: %d, after: $cursor) {
               pageInfo { hasNextPage endCursor }
               edges { node { id sku barcode title image { url } } } } } }""" % VARIANTS_PAGE
    while cursor:
        data = _gql(connection, token, q, {"id": product_id, "cursor": cursor})
        conn = ((data.get("product") or {}).get("variants")) or {}
        for e in conn.get("edges", []):
            yield e["node"]
        info = conn.get("pageInfo") or {}
        cursor = info.get("endCursor") if info.get("hasNextPage") else None


def fetch_products(connection, token, search_query):
    """Yield (product_node, variant_node) for products matching a Shopify
    search query such as  created_at:>='...'  or  updated_at:>'...'."""
    q = """query($cursor: String, $q: String) {
      products(first: %d, after: $cursor, query: $q) {
        pageInfo { hasNextPage endCursor }
        edges { node { %s } } } }""" % (PRODUCTS_PAGE, PRODUCT_FIELDS)
    cursor = None
    while True:
        data = _gql(connection, token, q, {"cursor": cursor, "q": search_query})
        conn = data.get("products") or {}
        for edge in conn.get("edges", []):
            prod = edge["node"]
            vconn = prod.get("variants") or {}
            for ve in vconn.get("edges", []):
                yield prod, ve["node"]
            vinfo = vconn.get("pageInfo") or {}
            if vinfo.get("hasNextPage"):
                for vnode in _extra_variants(connection, token, prod["id"], vinfo.get("endCursor")):
                    yield prod, vnode
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return
        cursor = info.get("endCursor")


def fetch_variants_by_gid(connection, token, gids):
    """Yield (product_node, variant_node) for specific variant GIDs."""
    q = """query($ids: [ID!]!) { nodes(ids: $ids) { ... on ProductVariant {
             id sku barcode title image { url }
             product { id title publishedAt createdAt updatedAt featuredImage { url } } } } }"""
    gids = list(gids)
    for i in range(0, len(gids), 100):
        data = _gql(connection, token, q, {"ids": gids[i:i + 100]})
        for node in data.get("nodes") or []:
            if node and node.get("product"):
                yield node["product"], node


# --------------------------------------------------------------------------
# Upserts
# --------------------------------------------------------------------------
def _upsert_variant(connection, product_node, var_node):
    var_gid = var_node.get("id", "")
    sku = var_node.get("sku") or ""
    barcode = var_node.get("barcode") or ""
    sku_or_barcode = barcode or sku
    missing = False
    if not sku_or_barcode:
        sku_or_barcode, missing = f"MISSING-SHOPIFY-{var_gid}", True

    created_str = product_node.get("createdAt")
    image = (var_node.get("image") or {}).get("url") or (product_node.get("featuredImage") or {}).get("url")
    sync_upsert_product(
        connection=connection,
        sku_or_barcode=sku_or_barcode,
        defaults={
            "title": product_node.get("title", "Unknown Product"),
            "variant_title": var_node.get("title") or product_node.get("title"),
            "has_missing_identifier": missing,
            "external_catalog_id": var_gid,
            "external_product_id": product_node.get("id"),
            "image_url": image,
            "channel": "Online" if product_node.get("publishedAt") else "POS",
            "webstore_creation_date": parse_datetime(created_str) if created_str else None,
        },
    )


def _channel_and_payment(order):
    channel = "POS" if order.get("source_name", "web") == "pos" else "ONLINE"
    gateways = [g.lower() for g in order.get("payment_gateway_names", [])]
    if any("cash" in g for g in gateways):
        payment = "CASH"
    elif any("gift_card" in g for g in gateways):
        payment = "GIFT_CARD_REDEMPTION"
    elif any("manual" in g for g in gateways):
        payment = "MANUAL_CARD_ENTRY"
    elif any(g in ["shopify_payments", "stripe", "paypal", "bogus"] for g in gateways) or not gateways:
        payment = "CREDIT_CARD"
    else:
        payment = "OTHER_USER_DEFINED"
    return channel, payment


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
        channel, payment_type = _channel_and_payment(order)
        sale_channel = "POS" if channel == "POS" else "WEBSTORE"
        system_loc = "Shopify Webstore" if channel == "ONLINE" else "Shopify POS"
        actual_loc = resolve_actual_location(connection, order.get("location_id"), channel == "POS")
        
        # Client Rule: Group any form of card payment as 'Card'
        is_card = payment_type in ('CREDIT_CARD', 'MANUAL_CARD_ENTRY', 'CARD_ON_FILE')
        sale_type = f"{system_loc.replace(' ', '-')}-{'Card' if is_card else 'Other'}"

        for item in order.get("line_items", []):
            sku = item.get("sku") or ""
            variant_id = str(item.get("variant_id") or "")
            if not sku and not variant_id:
                skipped += 1
                continue
            variant = None
            if variant_id:
                variant = Variant.objects.filter(
                    connection=connection,
                    external_catalog_id=f"gid://shopify/ProductVariant/{variant_id}",
                ).select_related("product").first()
            if not variant and sku:
                variant = Variant.objects.filter(connection=connection, sku_or_barcode=sku).select_related("product").first()
            if not variant:
                logger.warning(f"[SYNC] order {order_id}: no variant for sku='{sku}' variant='{variant_id}'")
                skipped += 1
                continue

            product = variant.product
            sale, _ = Sale.objects.update_or_create(
                external_order_id=order_id,
                defaults={
                    "connection": connection, "sale_date": created_dt,
                    "system_location": system_loc,
                    "actual_location": actual_loc or system_loc,
                    "sale_channel": sale_channel, "is_presale": sale_channel == "WEBSTORE",
                    "payment_type": payment_type, "type": sale_type,
                    "source_platform": "Shopify",
                    "is_within_current_aria_period": not (created_dt and created_dt < aria_start),
                },
            )
            processed_sales.add(sale)
                
            detail, _ = SaleDetail.objects.update_or_create(
                sale=sale, variant=variant,
                defaults={
                    "connection": connection, "external_order_id": order_id,
                    "sku_or_barcode": variant.sku_or_barcode,
                    "sales_units": int(item.get("quantity", 1)),
                    "price": item.get("price", 0.0),
                    "product_title": product.title, "product_category": product.category,
                },
            )
            
            # Client Requirement: Only Card sales are reported to ARIA.
            # Other sales get CLAU reporting dates only.
            apply_reporting_dates(
                sale=sale, 
                detail=detail, 
                product=product, 
                sync_started_at=sync_started_at, 
                is_card_sale=is_card
            )
            saved += 1

        # Client Rule: Hand off to the shared bundle helper once per order
        from core_api.utils.post_sync import unpack_bundles_for_sale
        for sale in processed_sales:
            unpack_bundles_for_sale(sale, connection, sync_started_at)

    return saved, skipped


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def run_sync(connection, mode, since, started_at):
    """
    mode   : MODE_INITIAL | MODE_INCREMENTAL
    since  : datetime watermark (only used for incremental)
    """
    token = get_valid_access_token(connection)
    stats = {"mode": mode}

    # ---- 1. Sales ---------------------------------------------------------
    if mode == MODE_INITIAL:
        sales_from = started_at - timedelta(days=NEW_CONNECTION_SALES_DAYS)
        orders = fetch_orders(connection, token, created_at_min=sales_from)
    else:
        sales_from = since
        orders = fetch_orders(connection, token, updated_at_min=since)

        # Sync 4 (client spec): release_date >= sync date - 7 AND
    # all_presales_loaded = false -> load ALL sales back to webstore_creation_date.
    presale_qs = presale_backfill_products(connection)
    oldest_presale = presale_backfill_start(presale_qs, started_at)
    if oldest_presale and oldest_presale < sales_from:
        seen = {str(o.get("id")) for o in orders}
        for o in fetch_orders(connection, token, created_at_min=oldest_presale):
            if str(o.get("id")) not in seen:
                orders.append(o)
    stats["orders_fetched"] = len(orders)

    ref_variant_gids, ref_location_ids = set(), set()
    for o in orders:
        if o.get("location_id"):
            ref_location_ids.add(str(o["location_id"]))
        for li in o.get("line_items", []):
            if li.get("variant_id"):
                ref_variant_gids.add(f"gid://shopify/ProductVariant/{li['variant_id']}")

    # ---- 2. Locations -----------------------------------------------------
    # Client Requirement: Always sync all active Shopify locations regardless of age
    # or whether they've had recent sales, because physical locations rarely change
    # their created_at/updated_at timestamps.
    loc_count = 0
    for loc in fetch_locations(connection, token):
        lid = str(loc.get("id"))
        upsert_location(
            connection, platform="SHOPIFY", external_id=lid, name=loc.get("name"),
            address1=loc.get("address1"), city=loc.get("city"), province=loc.get("province"),
            postal_code=loc.get("zip"), country=loc.get("country"),
            lat=loc.get("latitude"), lng=loc.get("longitude"),
            is_active=loc.get("active", True),
        )
        loc_count += 1
    stats["locations_synced"] = loc_count

    # ---- 3. Products ------------------------------------------------------
    if mode == MODE_INITIAL:
        record_cutoff = started_at - timedelta(days=NEW_CONNECTION_RECORD_DAYS)
        search = f"created_at:>='{iso_z(record_cutoff)}'"
    else:
        search = f"updated_at:>'{iso_z(since)}'"
    prod_count = 0
    for pnode, vnode in fetch_products(connection, token, search):
        _upsert_variant(connection, pnode, vnode)
        prod_count += 1

    # Any variant a sale points to must exist locally, whatever its age.
    have = set(Variant.objects.filter(connection=connection, external_catalog_id__in=ref_variant_gids)
               .values_list("external_catalog_id", flat=True))
    missing = ref_variant_gids - have
    for pnode, vnode in fetch_variants_by_gid(connection, token, missing):
        _upsert_variant(connection, pnode, vnode)
        prod_count += 1
    stats["variants_synced"] = prod_count

    # ---- 4. Save sales (after products so every line item can be matched) -
    saved, skipped = process_orders(connection, orders, started_at)
    stats.update(sale_lines_saved=saved, sale_lines_skipped=skipped)

    if presale_qs.exists():
        presale_qs.update(all_presales_loaded=True)
    return stats


# --------------------------------------------------------------------------
# Shop metafield reader (used by core_api/views.py for help texts)
# --------------------------------------------------------------------------
def get_shop_metafield(connection, namespace, key):
    """Reads a single Shop-level metafield via the Admin GraphQL API."""
    access_token = get_valid_access_token(connection)
    if not access_token or access_token.startswith("mock_"):
        return None

    shop_domain = connection.store_identifier
    url = f"https://{shop_domain}/admin/api/2026-07/graphql.json"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }
    query = """
    query getShopMetafield($namespace: String!, $key: String!) {
      shop {
        metafield(namespace: $namespace, key: $key) {
          value
        }
      }
    }
    """

    try:
        response = requests.post(
            url,
            headers=headers,
            json={"query": query, "variables": {"namespace": namespace, "key": key}},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        metafield = ((data.get("data") or {}).get("shop") or {}).get("metafield")
        return metafield.get("value") if metafield else None
    except Exception:
        logger.warning(f"Failed to read shop metafield {namespace}.{key} for {shop_domain}")
        return None