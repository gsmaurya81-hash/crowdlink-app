import threading
import hmac
import hashlib
import base64
import json
import os
import secrets
import logging
import traceback
import requests
from urllib.parse import urlparse
from django.utils.dateparse import parse_datetime
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.db.models import Q
from django.http import JsonResponse, HttpResponseRedirect, HttpResponse
from django.views import View
from django.core.cache import cache
from datetime import timedelta
from django.utils import timezone


class ShopifyExtensionRedirect(HttpResponseRedirect):
    allowed_schemes = ['http', 'https', 'ftp', 'extension']

logger = logging.getLogger(__name__)
audit = logging.getLogger("crowdlink.audit")

def is_valid_return_url(url):
    if not url:
        return False
    try:
        parsed = urlparse(url)
        if parsed.scheme == 'extension':
            return True
        if parsed.scheme == 'https':
            netloc = parsed.netloc.lower()
            if netloc == 'shopify.com' or netloc.endswith('.myshopify.com'):
                return True
    except Exception:
        pass
    return False

from core_api.models import ClientStoreConnection, Sale, Product, SalesLocation
from .services.shopify_oauth import get_shopify_auth_url, exchange_code_for_token
from .services.shopify_catalog import process_orders as _process_shopify_orders
from core_api.utils.aria import get_current_aria_week_start
from core_api.utils.product_sync import sync_upsert_product, run_connection_sync

class ShopifyConnectView(View):
    def get(self, request):
        shop = request.GET.get('shop')
        email = request.GET.get('email')
        return_url = request.GET.get('return_url')

        if not shop or not email:
            return JsonResponse({"error": "shop and email query parameters are required"}, status=400)

        if return_url and not is_valid_return_url(return_url):
            logger.warning(f"Blocked invalid return_url in ShopifyConnectView: {return_url}")
            return JsonResponse({"error": "Invalid return_url format or domain."}, status=400)

        state_token = secrets.token_urlsafe(32)

        cache_data = {"email": email, "return_url": return_url}
        cache.set(f"oauth_state_{state_token}", cache_data, timeout=600)

        client_id = getattr(settings, 'SHOPIFY_CLIENT_ID', os.environ.get('SHOPIFY_CLIENT_ID', ''))
        redirect_uri = getattr(settings, 'SHOPIFY_REDIRECT_URI', os.environ.get('SHOPIFY_REDIRECT_URI', 'http://localhost:8000/api/shopify/callback/'))

        # Match the scopes exactly with your shopify.app.toml
        scopes = "write_products,read_products,read_inventory,read_orders,customer_read_customers"

        auth_url = f"https://{shop}/admin/oauth/authorize?client_id={client_id}&scope={scopes}&redirect_uri={redirect_uri}&state={state_token}"

        return HttpResponseRedirect(auth_url)

class ShopifyCallbackView(View):
    def get(self, request):
        shop = request.GET.get('shop')
        code = request.GET.get('code')
        state_token = request.GET.get('state')

        if not shop or not code or not state_token:
            return JsonResponse({"error": "shop, code, and state are required"}, status=400)

        state_data = cache.get(f"oauth_state_{state_token}")

        if not state_data or not isinstance(state_data, dict):
            return JsonResponse({"error": "Invalid, expired, or missing state token (CSRF attempt)"}, status=403)

        email = state_data.get("email")
        return_url = state_data.get("return_url")

        if return_url and not is_valid_return_url(return_url):
            logger.warning(f"Blocked invalid cached return_url in ShopifyCallbackView: {return_url}")
            return JsonResponse({"error": "Invalid return_url format or domain."}, status=400)

        cache.delete(f"oauth_state_{state_token}")

        # One store = one CrowdLink account (see core_api/utils/connection_guard.py)
        from core_api.utils.connection_guard import store_conflict, conflict_response
        existing = store_conflict('SHOPIFY', shop, email)
        if existing:
            audit.warning("STORE_CONNECT_BLOCKED platform=SHOPIFY email=%s store=%s reason=already_connected "
                          "owner_email=%s owner_connection=%s", email, shop, existing.client_email, existing.pk)
            return conflict_response('SHOPIFY')

        try:
            token_data = exchange_code_for_token(shop, code)
        except Exception as e:
            return JsonResponse({"error": f"Failed to exchange token: {str(e)}"}, status=400)

        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in")
        token_expires_at = timezone.now() + timedelta(seconds=expires_in) if expires_in else None

        connection, created = ClientStoreConnection.objects.update_or_create(
            client_email=email,
            platform='SHOPIFY',
            store_identifier=shop,
            defaults={
                'access_token': access_token,
                'refresh_token': refresh_token,
                'token_expires_at': token_expires_at,
                'is_active': True
            }
        )
        audit.info("STORE_CONNECT platform=%s email=%s store=%s new_connection=%s",
                   'SHOPIFY', email, connection.store_identifier, created)

        def background_sync():
            try:
                result = run_connection_sync(connection)
                if result.get('status') == 'error':
                    raise RuntimeError(result.get('error'))
                from core_api.utils.connection_guard import absorb_previous_owners
                absorb_previous_owners(connection)
            except Exception:
                error_detail = traceback.format_exc()
                logger.error("[SYNC] first sync after connect FAILED for %s - %s:\n%s",
                             email, 'SHOPIFY', error_detail)
                log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'sync_error.log')
                try:
                    with open(log_path, 'a') as f:
                        f.write(f"\n--- Shopify Sync Error ---\n{error_detail}")
                except Exception:
                    pass

        # Run sync in the background to prevent 30-second timeout on 1000+ products
        sync_thread = threading.Thread(target=background_sync)
        sync_thread.start()

        html_response = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Connection Successful</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background-color: #f4f6f8; }}
                .card {{ background: white; padding: 2rem; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); text-align: center; max-width: 400px; }}
                h1 {{ color: #008060; font-size: 1.5rem; margin-bottom: 1rem; }}
                p {{ color: #6d7175; line-height: 1.5; }}
            </style>
        </head>
        <body>
            <div class="card">
                <h1>Shopify POS Connected Successfully!</h1>
                <p>Your catalog is syncing securely in the background. You can now close this tab and return to your dashboard.</p>
            </div>
            <script>
                setTimeout(() => {{
                    window.close();
                }}, 2000);
            </script>
        </body>
        </html>
        """
        return HttpResponse(html_response)


def _log_compliance_webhook_error(topic):
    """Writes the current exception's traceback to a dedicated log file
    so a bug in one compliance webhook's business logic never surfaces
    as a 500 to Shopify (which just triggers endless retries) but is
    still fully visible to us for debugging."""
    error_detail = traceback.format_exc()
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'compliance_webhook_error.log')
    try:
        with open(log_path, 'a') as f:
            f.write(f"\n--- {timezone.now().isoformat()} [{topic}] ---\n")
            f.write(error_detail)
    except Exception:
        pass
    logger.exception(f"Compliance webhook ({topic}) business logic failed")


def verify_shopify_webhook_hmac(request):
    """Returns True if the request's HMAC header is valid (or DEBUG mock mode).
    Fails closed (returns False) on any unexpected error, so callers always
    get a clean 401 instead of a 500."""
    try:
        mock_mode = settings.DEBUG and request.GET.get('mock', '').lower() == 'true'
        if mock_mode:
            return True

        hmac_header = request.headers.get('X-Shopify-Hmac-Sha256')
        if not hmac_header:
            return False

        secret = getattr(settings, 'SHOPIFY_CLIENT_SECRET', None) or os.environ.get('SHOPIFY_CLIENT_SECRET', '')
        if not secret:
            logger.error("SHOPIFY_CLIENT_SECRET is not set — cannot verify webhook HMAC")
            return False

        hash_str = hmac.new(
            secret.encode('utf-8'),
            request.body,
            hashlib.sha256
        ).digest()
        calculated_hmac = base64.b64encode(hash_str).decode('utf-8')

        return hmac.compare_digest(calculated_hmac, hmac_header)
    except Exception:
        logger.exception("verify_shopify_webhook_hmac failed unexpectedly")
        return False


@method_decorator(csrf_exempt, name='dispatch')
class ShopifyOrderWebhookView(View):
    """
    Real-time order webhook. Reuses shopify_catalog.process_orders() — the
    SAME engine the daily/manual sync uses to match line items to Variants,
    upsert Sale/SaleDetail, and apply reporting dates (is_card_sale rule) —
    so a webhook-delivered order and a backfilled order are always handled
    identically. The payload Shopify POSTs here is the same Order resource
    shape process_orders() already expects from orders.json.
    """
    def post(self, request):
        mock_mode = settings.DEBUG and request.GET.get('mock', '').lower() == 'true'
        if not verify_shopify_webhook_hmac(request):
            return JsonResponse({"error": "Missing or invalid HMAC signature"}, status=401)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        shop_domain = request.headers.get('X-Shopify-Shop-Domain', '')
        connection = ClientStoreConnection.objects.filter(store_identifier=shop_domain, platform='SHOPIFY').first()

        if mock_mode and not connection:
            connection = ClientStoreConnection.objects.filter(platform='SHOPIFY').first()

        if not connection:
            return JsonResponse({"error": "Store connection not found"}, status=404)

        try:
            saved, skipped = _process_shopify_orders(connection, [payload], timezone.now())
        except Exception:
            _log_compliance_webhook_error("orders webhook")
            # Acknowledge anyway — Shopify retries a non-2xx indefinitely, and
            # we've already logged the failure above for follow-up.
            return JsonResponse({"status": "received", "error": "processing failed, logged"}, status=200)

        return JsonResponse({"status": "success", "synced_transactions": saved, "skipped": skipped})


@method_decorator(csrf_exempt, name='dispatch')
class ShopifyProductWebhookView(View):
    def post(self, request):
        if not verify_shopify_webhook_hmac(request):
            return JsonResponse({"error": "Missing or invalid HMAC signature"}, status=401)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"status": "ignored", "reason": "invalid JSON"}, status=200)

        try:
            topic = request.headers.get('X-Shopify-Topic', '')
            shop_domain = request.headers.get('X-Shopify-Shop-Domain', '')

            connection = ClientStoreConnection.objects.filter(
                store_identifier=shop_domain, platform='SHOPIFY'
            ).first()

            if not connection:
                return JsonResponse({"status": "ignored", "reason": "no matching connection"}, status=200)

            if topic == 'products/delete':
                return JsonResponse({"status": "received"}, status=200)

            product_title = payload.get('title', 'Unknown Product')
            product_id_raw = payload.get('id', '')
            product_gid = payload.get('admin_graphql_api_id') or (f"gid://shopify/Product/{product_id_raw}" if product_id_raw else None)
            variants = payload.get('variants', []) or []

            synced_count = 0
            for variant in variants:
                variant_id = variant.get('id', '')
                sku = variant.get('sku') or ''
                barcode = variant.get('barcode') or ''

                sku_or_barcode = barcode if barcode else sku
                has_missing_identifier = False
                if not sku_or_barcode:
                    sku_or_barcode = f"MISSING-SHOPIFY-gid://shopify/ProductVariant/{variant_id}"
                    has_missing_identifier = True

                sync_upsert_product(
                    connection=connection,
                    sku_or_barcode=sku_or_barcode,
                    defaults={
                        "title": product_title,
                        "variant_title": variant.get('title') or product_title,
                        "has_missing_identifier": has_missing_identifier,
                        "external_catalog_id": f"gid://shopify/ProductVariant/{variant_id}",
                        "external_product_id": product_gid or sku_or_barcode,
                    }
                )
                synced_count += 1

            logger.info(f"[Webhook] {topic} for {shop_domain}: {synced_count} variant(s) synced")

        except Exception:
            _log_compliance_webhook_error("products webhook")

        return JsonResponse({"status": "received"}, status=200)


@method_decorator(csrf_exempt, name='dispatch')
class ShopifyCustomerDataRequestView(View):
    """Mandatory GDPR webhook: customers/data_request."""
    def post(self, request):
        if not verify_shopify_webhook_hmac(request):
            return JsonResponse({"error": "Invalid HMAC signature"}, status=401)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            payload = {}

        try:
            logger.info(f"[GDPR] customers/data_request received: shop={payload.get('shop_domain')} customer={payload.get('customer', {}).get('id')}")
        except Exception:
            _log_compliance_webhook_error("customers/data_request")

        return JsonResponse({"status": "received"}, status=200)


@method_decorator(csrf_exempt, name='dispatch')
class ShopifyCustomerRedactView(View):
    """Mandatory GDPR webhook: customers/redact."""
    def post(self, request):
        if not verify_shopify_webhook_hmac(request):
            return JsonResponse({"error": "Invalid HMAC signature"}, status=401)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            payload = {}

        try:
            logger.info(f"[GDPR] customers/redact received: shop={payload.get('shop_domain')} customer={payload.get('customer', {}).get('id')}")
        except Exception:
            _log_compliance_webhook_error("customers/redact")

        return JsonResponse({"status": "received"}, status=200)


@method_decorator(csrf_exempt, name='dispatch')
class ShopifyShopRedactView(View):
    """Mandatory GDPR webhook: shop/redact. Unlike the in-app "Disconnect"
    button (which only flips is_active=False so the merchant can reconnect),
    this webhook means the store has uninstalled the app and is legally
    requesting erasure, so it actually deletes the connection's data."""
    def post(self, request):
        if not verify_shopify_webhook_hmac(request):
            return JsonResponse({"error": "Invalid HMAC signature"}, status=401)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            payload = {}

        try:
            shop_domain = payload.get('shop_domain', '') or ''
            logger.info(f"[GDPR] shop/redact received: shop={shop_domain}")

            connections = ClientStoreConnection.objects.filter(store_identifier=shop_domain, platform='SHOPIFY')
            for connection in connections:
                Sale.objects.filter(connection=connection).delete()
                Product.objects.filter(connection=connection).delete()
                SalesLocation.objects.filter(connection=connection).delete()
                connection.delete()
        except Exception:
            _log_compliance_webhook_error("shop/redact")

        # Always acknowledge receipt with 200 — Shopify retries on non-2xx,
        # and we've already logged anything unexpected above for our own follow-up.
        return JsonResponse({"status": "received"}, status=200)


class ProductsView(View):
    def get(self, request):
        shop = request.GET.get('shop')
        email = request.GET.get('email')

        if not shop and not email:
            return JsonResponse({"error": "shop or email parameter is required"}, status=400)

        if email:
            connection = ClientStoreConnection.objects.filter(client_email=email, platform='SHOPIFY', is_active=True).first()
        else:
            connection = ClientStoreConnection.objects.filter(store_identifier=shop, platform='SHOPIFY', is_active=True).first()

        if not connection:
            return JsonResponse({"products": []})

        products = Product.objects.filter(connection=connection).order_by('-id')

        data = []
        for p in products:
            # Safely fetch the channel from the DB.
            # If the database doesn't have a channel column yet, it won't crash.
            product_channel = getattr(p, 'channel', None) or 'Online'

            data.append({
                "id": str(p.id),
                "title": p.title,
                "combined_sku_bar": p.combined_sku_bar,
                "has_missing_identifier": p.has_missing_identifier,
                "release_date": p.release_date.isoformat() if p.release_date else None,
                "report_to_aria": p.report_to_aria,
                "image_url": p.image_url,
                "status": "Synced",
                "channel": product_channel,
                "category": p.category,
                "json_bundle": p.json_bundle,
                "external_product_id": p.external_product_id,
            })

        return JsonResponse({"products": data})


@csrf_exempt
def manual_resync_shopify(request):
    email = request.GET.get('email')
    if not email:
        return JsonResponse({"error": "Pass ?email=<account email> in the URL"}, status=400)

    try:
        connection = ClientStoreConnection.objects.get(
            client_email=email, platform='SHOPIFY', is_active=True
        )
    except ClientStoreConnection.DoesNotExist:
        return JsonResponse({"error": f"No active Shopify connection for {email}"}, status=404)

    last_sync_before = connection.last_sync_at
    sales_before = Sale.objects.filter(connection=connection).count()

    result = run_connection_sync(
        connection,
        force_initial=request.GET.get('force_initial') == '1',
        # Manual refresh: re-syncs everything updated since this connection was
        # first created - a simple way to fix a connection whose daily sync has
        # been failing. See core_api/utils/product_sync.py MODE_REFRESH.
        refresh=request.GET.get('refresh') == '1',
    )

    connection.refresh_from_db()
    sales_after = Sale.objects.filter(connection=connection).count()

    return JsonResponse({
        "result": result,
        "store": connection.store_identifier,
        "last_sync_at_before": last_sync_before.isoformat() if last_sync_before else None,
        "last_sync_at_after": connection.last_sync_at.isoformat() if connection.last_sync_at else None,
        "sale_rows_before": sales_before,
        "sale_rows_after": sales_after,
        "note": "Check logs/sync.log for the 'since <timestamp>' line from this run.",
    })


@csrf_exempt
def disconnect_shopify(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            email = data.get('email')

            if not email:
                return JsonResponse({"error": "Email is required"}, status=400)

            connections = list(
                ClientStoreConnection.objects.filter(client_email=email, platform='SHOPIFY')
            )

            if not connections:
                return JsonResponse({"error": "No connection found"}, status=404)

            for connection in connections:
                rest_url = f"https://{connection.store_identifier}/admin/api/2026-07/api_permissions/current.json"
                headers = {
                    "X-Shopify-Access-Token": connection.access_token,
                    "Content-Type": "application/json",
                    "Accept": "application/json"
                }
                try:
                    requests.delete(rest_url, headers=headers, timeout=10)
                except Exception as e:
                    logger.warning(f"Failed to reach Shopify for app uninstall ({connection.store_identifier}): {str(e)}")

                # Deactivate only — keep the connection row and its synced
                # history so the merchant can reconnect later without losing
                # past sales/products. Actual data erasure only happens via
                # the shop/redact GDPR webhook above.
                connection.is_active = False
                connection.save(update_fields=['is_active'])
                audit.info("STORE_DISCONNECT platform=%s email=%s store=%s", 'SHOPIFY', email,
                           connection.store_identifier)

            return JsonResponse({
                "status": "success",
                "message": "App uninstalled and disconnected.",
                "stores_disconnected": [c.store_identifier for c in connections],
            })

        except Exception as e:
            audit.exception("STORE_DISCONNECT_FAILED platform=%s", 'SHOPIFY')
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid method"}, status=405)