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
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.db.models import Q
from django.http import JsonResponse, HttpResponseRedirect, HttpResponse
from django.views import View
from django.core.cache import cache
from core_api.models import ClientStoreConnection, Sale, SaleDetail, Product, Variant, SalesLocation
from .services.square_oauth import get_square_auth_url, exchange_code_for_token, get_valid_access_token
from core_api.utils.aria import get_current_aria_week_start
def is_valid_return_url(url):
    if not url:
        return False
    if url.startswith("extension:") or url.startswith("extension%3A") or url.startswith("extension%3a"):
        return True
    allowed_prefixes = (
        "https://api.app.crowdmerch.com.au",
        "https://app.crowdmerch.com.au",
        "https://crowdmerch.com.au",
        "https://shopify.com",
        "https://admin.shopify.com",
        "https://myshopify.com",
    )
    for prefix in allowed_prefixes:
        if url.startswith(prefix):
            return True
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme == "extension":
            return True
        if parsed.scheme == "https" and parsed.netloc.lower().endswith(".myshopify.com"):
            return True
    except Exception:
        pass
    return False

logger = logging.getLogger(__name__)
audit = logging.getLogger("crowdlink.audit")


class SquareConnectView(View):
    def get(self, request):
        email = request.GET.get('email')
        return_url = request.GET.get('return_url')

        if not email:
            return JsonResponse({"error": "email query parameter is required"}, status=400)

        if return_url and not is_valid_return_url(return_url):
            logger.warning(f"Blocked invalid return_url in SquareConnectView: {return_url}")
            return JsonResponse({"error": "Invalid return_url format or domain."}, status=400)

        # Generate secure state token
        state_token = secrets.token_urlsafe(32)

        # Store securely mapped to the email and return_url (expires in 10 minutes)
        cache_data = {"email": email, "return_url": return_url}
        cache.set(f"oauth_state_{state_token}", cache_data, timeout=600)

        auth_url = get_square_auth_url(state_token)
        return HttpResponseRedirect(auth_url)


class SquareCallbackView(View):
    def get(self, request):
        code = request.GET.get('code')
        state_token = request.GET.get('state')

        if not code or not state_token:
            return JsonResponse({"error": "code and state are required"}, status=400)

        state_data = cache.get(f"oauth_state_{state_token}")

        if not state_data or not isinstance(state_data, dict):
            return JsonResponse({"error": "Invalid, expired, or missing state token (CSRF attempt)"}, status=403)

        email = state_data.get("email")
        return_url = state_data.get("return_url")

        if return_url and not is_valid_return_url(return_url):
            logger.warning(f"Blocked invalid cached return_url in SquareCallbackView: {return_url}")
            return JsonResponse({"error": "Invalid return_url format or domain."}, status=400)

        cache.delete(f"oauth_state_{state_token}")

        try:
            token_data = exchange_code_for_token(code)
        except Exception as e:
            return JsonResponse({"error": f"Failed to exchange token: {str(e)}"}, status=400)

        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token", "")
        merchant_id = token_data.get("merchant_id", "unknown_merchant")
        expires_at_str = token_data.get("expires_at")
        expires_at = parse_datetime(expires_at_str) if expires_at_str else None

        # One store = one CrowdLink account (see core_api/utils/connection_guard.py)
        from core_api.utils.connection_guard import store_conflict, conflict_response
        existing = store_conflict('SQUARE', merchant_id, email)
        if existing:
            audit.warning("STORE_CONNECT_BLOCKED platform=SQUARE email=%s store=%s reason=already_connected "
                          "owner_email=%s owner_connection=%s", email, merchant_id, existing.client_email, existing.pk)
            return conflict_response('SQUARE')

        connection, created = ClientStoreConnection.objects.update_or_create(
            client_email=email,
            platform='SQUARE',
            defaults={
                'store_identifier': merchant_id,
                'access_token': access_token,
                'refresh_token': refresh_token,
                'token_expires_at': expires_at,
                'is_active': True
            }
        )
        audit.info("STORE_CONNECT platform=%s email=%s store=%s new_connection=%s",
                   'SQUARE', email, connection.store_identifier, created)

        def background_sync():
            try:
                from core_api.utils.product_sync import run_connection_sync
                result = run_connection_sync(connection)
                if result.get('status') == 'error':
                    raise RuntimeError(result.get('error'))
                from core_api.utils.connection_guard import absorb_previous_owners
                absorb_previous_owners(connection)
            except Exception:
                error_detail = traceback.format_exc()
                logger.error("[SYNC] first sync after connect FAILED for %s - %s:\n%s",
                             email, 'SQUARE', error_detail)
                log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'sync_error.log')
                try:
                    with open(log_path, 'a') as f:
                        f.write(f"\n--- Square Sync Error ---\n{error_detail}")
                except Exception:
                    pass

        # Run sync in the background so the OAuth window doesn't timeout!
        sync_thread = threading.Thread(target=background_sync)
        sync_thread.start()

        html_response = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Connection Successful</title>
            <style>
                body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background-color: #f4f6f8; }
                .card { background: white; padding: 2rem; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); text-align: center; max-width: 400px; }
                h1 { color: #008060; font-size: 1.5rem; margin-bottom: 1rem; }
                p { color: #6d7175; line-height: 1.5; }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>Square POS Connected Successfully!</h1>
                <p>Your catalog is syncing securely in the background. You can now close this tab and return to your dashboard.</p>
            </div>
            <script>
                setTimeout(() => {
                    window.close();
                }, 2000);
            </script>
        </body>
        </html>
        """
        return HttpResponse(html_response)


@method_decorator(csrf_exempt, name='dispatch')
class SquareOrderWebhookView(View):
    def post(self, request):
        mock_mode = settings.DEBUG and request.GET.get('mock', '').lower() == 'true'

        if not mock_mode:
            signature_header = request.headers.get('x-square-hmacsha256-signature')
            if not signature_header:
                return JsonResponse({"error": "Missing Square signature header"}, status=401)

            signature_key = getattr(settings, 'SQUARE_WEBHOOK_SIGNATURE_KEY', os.environ.get('SQUARE_WEBHOOK_SIGNATURE_KEY', ''))

            # FORCE HTTPS: Fixes the cPanel HTTP proxy bug for signature verification
            domain = request.get_host()
            notification_url = f"https://{domain}{request.path}"

            string_to_sign = notification_url + request.body.decode('utf-8')
            hash_str = hmac.new(
                signature_key.encode('utf-8'),
                string_to_sign.encode('utf-8'),
                hashlib.sha256
            ).digest()
            calculated_signature = base64.b64encode(hash_str).decode('utf-8')

            if not hmac.compare_digest(calculated_signature, signature_header):
                return JsonResponse({"error": "Invalid Square signature"}, status=401)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        data_object = payload.get('data', {}).get('object', {}).get('order_created', {})
        merchant_id = payload.get('merchant_id', '')

        # Grab the ID from the metadata payload
        order_id = data_object.get('order_id', '')
        if not order_id:
            return JsonResponse({"error": "Missing order_id in webhook payload"}, status=400)

        connection = ClientStoreConnection.objects.filter(store_identifier=merchant_id, platform='SQUARE').first()

        if mock_mode and not connection:
            connection = ClientStoreConnection.objects.filter(platform='SQUARE').first()

        if not connection:
            return JsonResponse({"error": "Store connection not found"}, status=404)

        # Fetch full order details via API (because Webhook only sends metadata)
        environment = getattr(settings, "SQUARE_ENVIRONMENT", os.environ.get("SQUARE_ENVIRONMENT", "sandbox")).lower()
        base_url = "https://connect.squareup.com" if environment == "production" else "https://connect.squareupsandbox.com"

        url = f"{base_url}/v2/orders/{order_id}"
        try:
            headers = {
                "Authorization": f"Bearer {get_valid_access_token(connection)}",
                "Content-Type": "application/json"
            }
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            order_data = response.json().get('order', {})
        except Exception as e:
            logger.error(f"Square webhook failed to fetch full order {order_id}: {e}")
            return JsonResponse({"error": "Failed to fetch full order from Square"}, status=500)

        created_at_str = order_data.get('created_at')
        if not created_at_str:
            return JsonResponse({"error": "Missing created_at"}, status=400)

        created_at_dt = parse_datetime(created_at_str)

        is_within_aria = True
        if created_at_dt and created_at_dt < get_current_aria_week_start():
            is_within_aria = False

        source_name = order_data.get('source', {}).get('name', '')
        channel = 'WEBSTORE' if 'square online' in source_name.lower() else 'POS'
        location_id = str(order_data.get('location_id', ''))

        # Map actual_location dynamically
        system_loc_str = 'Square Webstore' if channel == 'WEBSTORE' else 'Square POS'
        actual_loc_str = None

        if location_id and channel == 'POS':
            loc_obj = SalesLocation.objects.filter(connection=connection, external_location_id=location_id).first()
            if loc_obj:
                city_str = loc_obj.city or ""
                prov_str = loc_obj.province or ""
                combined = f"{city_str} {prov_str}".strip()
                actual_loc_str = combined if combined else loc_obj.name

        tenders = order_data.get('tenders', [])

        payment_type = 'OTHER_USER_DEFINED'
        if tenders:
            tender_type = tenders[0].get('type', '')
            if tender_type == 'CASH':
                payment_type = 'CASH'
            elif tender_type == 'CARD':
                payment_type = 'CREDIT_CARD'
            elif tender_type == 'SQUARE_GIFT_CARD':
                payment_type = 'GIFT_CARD_REDEMPTION'

        sale_channel = 'POS' if channel == 'POS' else 'WEBSTORE'
        is_presale = (sale_channel == 'WEBSTORE')
        
        # Client Rule: Group any form of card payment as 'Card'
        is_card = payment_type in ('CREDIT_CARD', 'MANUAL_CARD_ENTRY', 'CARD_ON_FILE')

        # Client Rule: Delete old unpacked bundle items before regenerating them (Order-level cleanup)
        existing_sale = Sale.objects.filter(connection=connection, external_order_id=order_id).first()
        if existing_sale and not existing_sale.reported_flag:
            SaleDetail.objects.filter(sale=existing_sale, product_category__startswith="Bundle-").delete()

        line_items = order_data.get('line_items', [])
        synced_count = 0

        for item in line_items:
            sku = item.get('note', '')
            catalog_object_id = item.get('catalog_object_id', '')
            quantity = int(item.get('quantity', 1))

            amount = item.get('base_price_money', {}).get('amount', 0)
            price = float(amount) / 100.0

            if not sku and not catalog_object_id:
                continue

            variant = None
            if catalog_object_id:
                variant = Variant.objects.filter(connection=connection, external_catalog_id=catalog_object_id).select_related('product').first()

            if not variant and sku:
                logger.warning(f"Square webhook: Falling back to SKU matching for '{sku}'")
                variant = Variant.objects.filter(connection=connection, sku_or_barcode=sku).select_related('product').first()

            if variant:
                product = variant.product
                sale_type = f"{system_loc_str.replace(' ', '-')}-{'Card' if is_card else 'Other'}"

                # 1. Upsert Sale Header (1 unique row per external_order_id)
                sale, _ = Sale.objects.update_or_create(
                    external_order_id=order_id,
                    defaults={
                        'connection': connection,
                        'sale_date': created_at_dt,
                        'system_location': system_loc_str,
                        'actual_location': actual_loc_str if actual_loc_str else system_loc_str,
                        'sale_channel': sale_channel,
                        'is_presale': is_presale,
                        'payment_type': payment_type,
                        'type': sale_type,
                        'source_platform': 'Square',
                        'is_within_current_aria_period': is_within_aria,
                    }
                )

                # 2. Upsert SaleDetail Line Item (linked to Sale header and Variant)
                sale_detail, _ = SaleDetail.objects.update_or_create(
                    sale=sale,
                    variant=variant,
                    defaults={
                        'connection': connection,
                        'external_order_id': order_id,
                        'sku_or_barcode': variant.sku_or_barcode,
                        'sales_units': quantity,
                        'price': price,
                        'product_title': product.title,
                        'product_category': product.category,
                    }
                )

                # --- REPORTING DATE LOGIC ---
                from core_api.utils.product_sync import apply_reporting_dates
                apply_reporting_dates(
                    sale=sale, 
                    detail=sale_detail, 
                    product=product, 
                    sync_started_at=timezone.now(), 
                    is_card_sale=is_card
                )

                synced_count += 1

        # Client Rule: Hand off to the shared bundle helper once per order
        if synced_count > 0:
            from core_api.utils.post_sync import unpack_bundles_for_sale
            unpack_bundles_for_sale(sale, connection, timezone.now())

        return JsonResponse({"status": "success", "synced_transactions": synced_count})


class SquareProductsView(View):
    def get(self, request):
        email = request.GET.get('email')

        if not email:
            return JsonResponse({"error": "email parameter is required"}, status=400)

        # Filter for active connections only to hide disconnected data
        connection = ClientStoreConnection.objects.filter(client_email=email, platform='SQUARE', is_active=True).first()

        if not connection:
            return JsonResponse({"products": []})

        products = Product.objects.filter(connection=connection).order_by('-id')

        data = []
        for p in products:
            data.append({
                "id": str(p.id),
                "title": p.title,
                "combined_sku_bar": p.combined_sku_bar,
                "has_missing_identifier": p.has_missing_identifier,
                "release_date": p.release_date.isoformat() if p.release_date else None,
                "report_to_aria": p.report_to_aria,
                "image_url": p.image_url,
                "status": "Synced",
                "channel": getattr(p, 'channel', None) or 'POS',
                "category": p.category,
                "json_bundle": p.json_bundle,
                "external_product_id": p.external_product_id,
            })

        return JsonResponse({"products": data})


@csrf_exempt
def manual_resync_square(request):
    from core_api.utils.product_sync import run_connection_sync

    email = request.GET.get('email')
    if not email:
        return JsonResponse({"error": "Pass ?email=<account email> in the URL"}, status=400)

    try:
        connection = ClientStoreConnection.objects.get(
            client_email=email, platform='SQUARE', is_active=True
        )
    except ClientStoreConnection.DoesNotExist:
        return JsonResponse({"error": f"No active Square connection for {email}"}, status=404)

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
def disconnect_square(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            email = data.get('email')

            if not email:
                return JsonResponse({"error": "Email is required"}, status=400)

            connections = list(
                ClientStoreConnection.objects.filter(client_email=email, platform='SQUARE')
            )

            if not connections:
                return JsonResponse({"error": "No connection found"}, status=404)

            client_id = getattr(settings, 'SQUARE_APPLICATION_ID', os.environ.get('SQUARE_APPLICATION_ID', ''))
            environment = getattr(settings, 'SQUARE_ENVIRONMENT', os.environ.get('SQUARE_ENVIRONMENT', 'sandbox')).lower()
            revoke_url = "https://connect.squareup.com/oauth2/revoke" if environment == 'production' else "https://connect.squareupsandbox.com/oauth2/revoke"

            for connection in connections:
                try:
                    requests.post(
                        revoke_url,
                        headers={"Authorization": f"Client {client_id}"},
                        json={"client_id": client_id, "access_token": connection.access_token},
                        timeout=10
                    )
                except Exception as e:
                    logger.warning(f"Failed to reach Square for token revocation ({connection.store_identifier}): {str(e)}")

                connection.is_active = False
                connection.save(update_fields=['is_active'])
                audit.info("STORE_DISCONNECT platform=%s email=%s store=%s", 'SQUARE', email,
                           connection.store_identifier)

            return JsonResponse({
                "status": "success",
                "message": "Square uninstalled and disconnected.",
                "stores_disconnected": [c.store_identifier for c in connections],
            })

        except Exception as e:
            audit.exception("STORE_DISCONNECT_FAILED platform=%s", 'SQUARE')
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid method"}, status=405)