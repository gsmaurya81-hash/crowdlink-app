import os
import requests
import logging
from datetime import timedelta
from django.conf import settings
from django.utils import timezone

# NEW: We must define the logger in this file so the webhook function can use it!
logger = logging.getLogger(__name__)

def get_shopify_auth_url(shop_domain, state_token):
    client_id = getattr(settings, 'SHOPIFY_CLIENT_ID', os.environ.get('SHOPIFY_CLIENT_ID', 'fallback_client_id'))
    redirect_uri = getattr(settings, 'SHOPIFY_REDIRECT_URI', os.environ.get('SHOPIFY_REDIRECT_URI', 'http://localhost:8000/api/shopify/callback/'))

    base_url = f"https://{shop_domain}/admin/oauth/authorize"

    params = {
        "client_id": client_id,
        "scope": "write_products,read_products,read_inventory,read_orders,customer_read_customers,read_locations",
        "redirect_uri": redirect_uri,
        "state": state_token
    }

    req = requests.Request('GET', base_url, params=params).prepare()
    return req.url

def exchange_code_for_token(shop_domain, auth_code):
    if settings.DEBUG and auth_code == "test_mock_code":
        return {
            "access_token": "mock_shp_123"
        }

    client_id = getattr(settings, 'SHOPIFY_CLIENT_ID', os.environ.get('SHOPIFY_CLIENT_ID', ''))
    client_secret = getattr(settings, 'SHOPIFY_CLIENT_SECRET', os.environ.get('SHOPIFY_CLIENT_SECRET', ''))

    if not client_id or not client_secret:
        return {
            "access_token": "mock_shp_123"
        }

    url = f"https://{shop_domain}/admin/oauth/access_token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": auth_code,
        "expiring": 1
    }

    response = requests.post(url, json=data)
    response.raise_for_status()
    return response.json()

def refresh_offline_token(shop_domain, refresh_token):
    client_id = getattr(settings, 'SHOPIFY_CLIENT_ID', os.environ.get('SHOPIFY_CLIENT_ID', ''))
    client_secret = getattr(settings, 'SHOPIFY_CLIENT_SECRET', os.environ.get('SHOPIFY_CLIENT_SECRET', ''))

    url = f"https://{shop_domain}/admin/oauth/access_token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }

    response = requests.post(url, data=data)
    response.raise_for_status()
    return response.json()

def get_valid_access_token(connection):
    """
    Returns a valid access token for this connection, refreshing it
    first if it's expired or missing an expiry (legacy token).
    """
    if not connection.token_expires_at:
        return connection.access_token

    if timezone.now() < connection.token_expires_at - timedelta(minutes=5):
        return connection.access_token

    if not connection.refresh_token:
        return connection.access_token

    result = refresh_offline_token(connection.store_identifier, connection.refresh_token)

    connection.access_token = result.get("access_token", connection.access_token)
    connection.refresh_token = result.get("refresh_token", connection.refresh_token)

    expires_in = result.get("expires_in")
    if expires_in:
        connection.token_expires_at = timezone.now() + timedelta(seconds=expires_in)

    connection.save(update_fields=["access_token", "refresh_token", "token_expires_at"])

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