import os
import requests
import logging
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.utils import timezone

# NEW: We must define the logger in this file so the webhook function can use it!
logger = logging.getLogger(__name__)

def get_shopify_auth_url(shop_domain, state_token):
    client_id = getattr(settings, 'SHOPIFY_CLIENT_ID', os.environ.get('SHOPIFY_CLIENT_ID', 'fallback_client_id'))
    redirect_uri = getattr(settings, 'SHOPIFY_REDIRECT_URI', os.environ.get('SHOPIFY_REDIRECT_URI', 'http://localhost:8000/api/shopify/callback/'))

    base_url = f"https://{shop_domain}/admin/oauth/authorize"

    params = {
        "client_id": client_id,
        "scope": "read_products,read_inventory,read_orders,customer_read_customers,read_locations"
                 + (",read_all_orders" if os.environ.get("SHOPIFY_REQUEST_READ_ALL_ORDERS") == "1" else ""),
        "redirect_uri": redirect_uri,
        "state": state_token
    }

    req = requests.Request('GET', base_url, params=params).prepare()
    return req.url

def exchange_code_for_token(shop_domain, auth_code):
    client_id = getattr(settings, 'SHOPIFY_CLIENT_ID', os.environ.get('SHOPIFY_CLIENT_ID', ''))
    client_secret = getattr(settings, 'SHOPIFY_CLIENT_SECRET', os.environ.get('SHOPIFY_CLIENT_SECRET', ''))

    if not client_id or not client_secret:
        raise RuntimeError("SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET are not configured")

    url = f"https://{shop_domain}/admin/oauth/access_token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": auth_code,
        "expiring": 1
    }

    response = requests.post(url, json=data, timeout=15)
    response.raise_for_status()
    return response.json()

class ShopifyTokenRefreshError(Exception):
    """Refresh of an expiring offline token was rejected by Shopify."""


def refresh_offline_token(shop_domain, refresh_token):
    client_id = getattr(settings, 'SHOPIFY_CLIENT_ID', os.environ.get('SHOPIFY_CLIENT_ID', ''))
    client_secret = getattr(settings, 'SHOPIFY_CLIENT_SECRET', os.environ.get('SHOPIFY_CLIENT_SECRET', ''))

    if not client_id or not client_secret:
        raise ShopifyTokenRefreshError(
            "SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET are not visible to this process "
            "(check .env is readable by cron)")

    url = f"https://{shop_domain}/admin/oauth/access_token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }

    response = requests.post(url, data=data, timeout=15)
    if response.status_code >= 400:
        # Log Shopify's own reason (never the tokens/secret) so a 401 is diagnosable.
        body = (response.text or "")[:300]
        logger.error("[TOKEN] refresh rejected for %s: HTTP %s client_id=%s... body=%s",
                     shop_domain, response.status_code, client_id[:6], body)
        raise ShopifyTokenRefreshError(
            "Shopify rejected the token refresh (HTTP %s) for %s - the store must reconnect "
            "the app (reinstall / re-authorise). Shopify said: %s"
            % (response.status_code, shop_domain, body))
    return response.json()

def _apply_token_result(connection, result):
    connection.access_token = result.get("access_token", connection.access_token)
    connection.refresh_token = result.get("refresh_token", connection.refresh_token)
    expires_in = result.get("expires_in")
    if expires_in:
        connection.token_expires_at = timezone.now() + timedelta(seconds=expires_in)


def get_valid_access_token(connection):
    """
    Returns a valid access token for this connection, refreshing it
    first if it's expired or missing an expiry (legacy token).

    Shopify refresh tokens are single-use: every refresh returns a NEW pair and kills
    the old one. Webhooks, the nightly cron and manual syncs can all get here at the
    same moment, so the refresh runs under a row lock and re-reads the row first. The
    second caller then reuses the tokens the first one just saved instead of spending
    an already-used refresh token (which Shopify rejects with 401 invalid_request).
    """
    if not connection.token_expires_at:
        return connection.access_token

    if timezone.now() < connection.token_expires_at - timedelta(minutes=5):
        return connection.access_token

    if not connection.refresh_token:
        return connection.access_token

    with transaction.atomic():
        locked = type(connection).objects.select_for_update().get(pk=connection.pk)

        # Someone else refreshed while we waited for the lock: use their tokens.
        if locked.token_expires_at and timezone.now() < locked.token_expires_at - timedelta(minutes=5):
            connection.access_token = locked.access_token
            connection.refresh_token = locked.refresh_token
            connection.token_expires_at = locked.token_expires_at
            return connection.access_token

        result = refresh_offline_token(locked.store_identifier, locked.refresh_token)
        _apply_token_result(locked, result)
        locked.save(update_fields=["access_token", "refresh_token", "token_expires_at"])

    logger.info("[TOKEN] refreshed %s: access_expires_in=%s refresh_expires_in=%s",
                locked.store_identifier, result.get("expires_in"),
                result.get("refresh_token_expires_in"))
    connection.access_token = locked.access_token
    connection.refresh_token = locked.refresh_token
    connection.token_expires_at = locked.token_expires_at
    return connection.access_token

def register_order_webhook(shop, access_token):
    """Registers a real-time orders/create webhook subscription with Shopify
    so new orders are pushed to us immediately, instead of only being
    captured by the one-time post-connect backfill."""
    redirect_base = getattr(settings, 'SHOPIFY_REDIRECT_URI', os.environ.get('SHOPIFY_REDIRECT_URI', ''))
    webhook_callback_url = redirect_base.replace('/callback/', '/webhooks/orders/')

    url = f"https://{shop}/admin/api/2026-07/webhooks.json"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json"
    }
    payload = {
        "webhook": {
            "topic": "orders/create",
            "address": webhook_callback_url,
            "format": "json"
        }
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        logger.info(f"Registered orders/create webhook for {shop}: {response.json()}")
    except Exception as e:
        logger.error(f"Failed to register order webhook for {shop}: {e}")