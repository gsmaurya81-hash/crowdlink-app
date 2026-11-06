import json
import io
import logging
from django.core.management import call_command
from django.http import HttpResponse
from django.http import JsonResponse, HttpResponse
from django.views import View
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.core.management import call_command
from django.db import models
from core_api.models import ClientStoreConnection, Product, Variant, CATEGORY_CHOICES, is_category_aria_reportable
from core_api.models import Sale, SaleDetail, SalesSummary, SalesLocation
from core_api.utils.post_sync import build_summary_groups, _client_tz
from core_api.utils.product_sync import manual_sync_status, start_manual_sync
import datetime as _datetime

audit = logging.getLogger("crowdlink.audit")
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
except ImportError:  # pragma: no cover
    _ZoneInfo = None


# ---------------------------------------------------------------------------
# Release-date rules (client requirement) - backend is authoritative
# ---------------------------------------------------------------------------
MSG_RELEASE_DATE_PAST = "can't set a release date in the past"
MSG_RELEASE_DATE_LOCKED = (
    "can't change a release date if it's already passed and we've reported the sales"
)
CLIENT_TIME_ZONE = "Australia/Sydney"


def _client_today():
    from core_api.utils.post_sync import client_today
    return client_today()


def validate_release_date_change(existing, new, today=None):
    """Returns an error message, or None if the change is allowed."""
    today = today or _client_today()
    if existing == new:
        return None
    if existing is not None and existing <= today:
        return MSG_RELEASE_DATE_LOCKED
    if new is not None and new <= today:
        return MSG_RELEASE_DATE_PAST
    return None


@method_decorator(csrf_exempt, name='dispatch')
class ConnectionSettingsView(View):
    def get(self, request):
        email = request.GET.get('email')
        if not email:
            return JsonResponse({"error": "email parameter is required"}, status=400)

        connections = ClientStoreConnection.objects.filter(client_email=email, is_active=True)

        data = []
        for c in connections:
            data.append({
                "platform": c.platform,
                "report_frequency": c.report_frequency,
                "show_price_on_charts": c.show_price_on_charts
            })

        return JsonResponse({"connections": data})

    def patch(self, request):
        email = request.GET.get('email')
        if not email:
            return JsonResponse({"error": "email parameter is required"}, status=400)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        connections = ClientStoreConnection.objects.filter(client_email=email, is_active=True)
        if not connections.exists():
            return JsonResponse({"error": "No active connections found for this email"}, status=404)

        updated = 0
        for c in connections:
            if 'report_frequency' in payload:
                c.report_frequency = payload['report_frequency']
            if 'show_price_on_charts' in payload:
                c.show_price_on_charts = payload['show_price_on_charts']
            c.save()
            updated += 1
        audit.info("CONNECTION_SETTINGS_UPDATE email=%s connections=%s fields=%s", email, updated,
                   sorted(k for k in payload if k in ('report_frequency', 'show_price_on_charts')))

        return JsonResponse({"status": "success", "connections_updated": updated})


@method_decorator(csrf_exempt, name='dispatch')
class ProductReleaseDateView(View):
    """
    CrowdLink product-update endpoint.

    Originally release-date only; extended for Stage 2 to be the single
    endpoint for all three CrowdLink-owned Product fields (category,
    release_date, report_to_aria), per the SOW's preference to extend an
    existing endpoint rather than add several single-purpose ones. Class
    name and URL are unchanged since nothing depends on new naming yet.

    A Product row on the Products tab can represent a group of underlying
    Shopify/Square variant rows (see core_api.utils.product_grouping). To
    keep those variants consistent, an update here fans out to every
    Product row that shares the same (connection, external_product_id) as
    the target row — not just the single row identified by `product_id`.
    """
    CATEGORY_MAX_LEN = 32

    def patch(self, request, product_id):
        email = request.GET.get('email')
        if not email:
            return JsonResponse({"error": "email parameter is required for authorization"}, status=401)

        try:
            product = Product.objects.get(id=product_id, connection__client_email=email)
        except Product.DoesNotExist:
            return JsonResponse({"error": "Product not found or not authorized"}, status=404)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        updates = {}

        # ----- CATEGORY -----
        if 'category' in payload:
            category = payload['category']

            if category is None or category == '':
                updates['category'] = None
            elif isinstance(category, str):
                stripped = category.strip()
                
                if not stripped or set(stripped) == {'-'}:   # "-", "----------" etc. revert to blank
                    updates['category'] = None
                elif len(stripped) > self.CATEGORY_MAX_LEN:
                    return JsonResponse(
                        {"error": f"category too long (max {self.CATEGORY_MAX_LEN} chars)"},
                        status=400,
                    )
                else:
                    updates['category'] = stripped
            else:
                return JsonResponse(
                    {"error": "category must be a string or null"},
                    status=400,
                )

        # ----- JSON BUNDLE -----
        # Stored as [{"product_id": "<uuid>", "bundle_unitprice": 12.5}, ...].
        # "unit_price" is accepted as an alias on input.
        if 'json_bundle' in payload:
            import math, uuid
            bundle_data = payload['json_bundle']
            if bundle_data is None or bundle_data == []:
                bundle_data = None
            else:
                if not isinstance(bundle_data, list):
                    return JsonResponse({"error": "json_bundle must be a list"}, status=400)
                clean, seen = [], set()
                for item in bundle_data:
                    if not isinstance(item, dict):
                        return JsonResponse({"error": "Each bundle item must be an object"}, status=400)
                    raw_price = item.get('bundle_unitprice', item.get('unit_price', 0))
                    try:
                        price_val = float(raw_price)
                        if math.isnan(price_val) or math.isinf(price_val) or price_val < 0:
                            raise ValueError
                    except (TypeError, ValueError):
                        return JsonResponse({"error": "bundle_unitprice must be a non-negative number"}, status=400)
                    sub_prod_id = item.get('product_id')
                    try:
                        sub_uuid = str(uuid.UUID(str(sub_prod_id)))
                    except (TypeError, ValueError):
                        return JsonResponse({"error": "Invalid product_id in bundle"}, status=400)
                    if sub_uuid == str(product.id):
                        return JsonResponse({"error": "A bundle cannot contain itself"}, status=400)
                    if sub_uuid in seen:
                        return JsonResponse({"error": "A product can only be added to a bundle once"}, status=400)
                    seen.add(sub_uuid)
                    sub_product = Product.objects.filter(id=sub_uuid, connection=product.connection).first()
                    if sub_product is None:
                        return JsonResponse({"error": "Bundle product not found for this connection"}, status=400)
                    if sub_product.category == 'Define Bundle':
                        return JsonResponse({"error": "A bundle cannot contain another bundle"}, status=400)
                    if not sub_product.category:
                        return JsonResponse(
                            {"error": f"Set a category on '{sub_product.title}' before adding it to a bundle"},
                            status=400,
                        )
                    clean.append({"product_id": sub_uuid, "bundle_unitprice": round(price_val, 2)})
                bundle_data = clean
            updates['json_bundle'] = bundle_data

        # ----- RELEASE DATE -----
        if 'release_date' in payload:
            raw = payload['release_date']
            if raw is None or raw == '':
                new_date = None
            elif isinstance(raw, str):
                try:
                    new_date = _datetime.datetime.strptime(
                        raw.strip()[:10], "%Y-%m-%d"
                    ).date()
                except ValueError:
                    return JsonResponse(
                        {"error": "release_date must be a valid date (YYYY-MM-DD)"},
                        status=400,
                    )
            else:
                return JsonResponse(
                    {"error": "release_date must be a string or null"},
                    status=400,
                )

            if product.external_product_id:
                _date_siblings = Product.objects.filter(
                    connection=product.connection,
                    external_product_id=product.external_product_id,
                )
            else:
                _date_siblings = Product.objects.filter(pk=product.pk)

            today = _client_today()
            existing_dates = list(_date_siblings.values_list('release_date', flat=True))
            for existing in existing_dates:
                err = validate_release_date_change(existing, new_date, today)
                if err:
                    return JsonResponse({"error": err}, status=400)

            # Only touch the row (and reset the presale flag) if the date
            # actually changes.
            if any(existing != new_date for existing in existing_dates):
                updates['release_date'] = new_date
                # Client Requirement: Reset the presale flag when the date
                # changes so the background sync re-expands its search window.
                updates['all_presales_loaded'] = False

        # ----- REPORT TO ARIA (explicit YES/NO from the dropdown) -----
        if 'report_to_aria' in payload:
            val = payload['report_to_aria']
            if not isinstance(val, bool):
                return JsonResponse(
                    {"error": "report_to_aria must be true or false"},
                    status=400,
                )
            effective_category = (
                updates['category'] if 'category' in updates else product.category
            )
            # YES is only possible for a reportable category.
            updates['report_to_aria'] = val and is_category_aria_reportable(effective_category)

        # ----- REPORT TO ARIA (auto-derived from category) -----
        if 'category' in updates:
            updates['report_to_aria'] = is_category_aria_reportable(updates['category'])

        # CLEANUP: If the product is no longer a bundle, wipe any leftover bundle data
        effective_category = updates.get('category', product.category)
        if effective_category != 'Define Bundle' and product.json_bundle is not None:
            updates['json_bundle'] = None

        if not updates:
            if 'release_date' in payload:
                # Valid no-op (date unchanged): return current values.
                product.refresh_from_db()
                return JsonResponse({
                    "status": "success",
                    "product_id": str(product.id),
                    "updated_product_ids": [str(product.id)],
                    "category": product.category,
                    "release_date": product.release_date.isoformat() if product.release_date else None,
                    "report_to_aria": product.report_to_aria,
                    "json_bundle": product.json_bundle,
                })
            return JsonResponse({"error": "No recognized fields in payload"}, status=400)

        if product.external_product_id:
            sibling_qs = Product.objects.filter(
                connection=product.connection,
                external_product_id=product.external_product_id,
            )
        else:
            sibling_qs = Product.objects.filter(pk=product.pk)

        updated_ids = list(sibling_qs.values_list('id', flat=True))
        sibling_qs.update(**updates)
        product.refresh_from_db()
        audit.info("PRODUCT_UPDATE email=%s product=%s rows=%s changes=%s", email, product.id,
                   len(updated_ids), {k: str(v) for k, v in updates.items()})

        return JsonResponse({
            "status": "success",
            "product_id": str(product.id),
            "updated_product_ids": [str(pid) for pid in updated_ids],
            "category": product.category,
            "release_date": product.release_date.isoformat() if product.release_date else None,
            "report_to_aria": product.report_to_aria,
            "json_bundle": product.json_bundle,
        })


# Stage 2 SOW-supplied default copy for the Products tab's "How does it
# work?" section.
DEFAULT_PRODUCTS_HELP_TEXT = (
    "First select a category. Then select a release date (or leave date "
    "blank if it's already released). Then flag albums / singles for ARIA "
    "reporting as required.\n"
    "Note: CrowdLink assumes you've already submitted an ARIA release "
    "survey for each product you want to report - and included the same "
    "unique SKU or barcode on your webstore."
)


DEFAULT_STORE_CONNECTION_TEXT = (
    "Connect your Square or Shopify account to sync products/sales and set "
    "up ARIA reporting. Note: We never change the data on your actual "
    "store or POS system, ever. This is a read-only app."
)


class StoreConnectionTextView(View):
    def get(self, request):
        email = request.GET.get('email')
        text = None

        if email:
            connection = ClientStoreConnection.objects.filter(
                client_email=email, platform='SHOPIFY', is_active=True
            ).first()
            if connection:
                try:
                    from shopify_integration.services.shopify_catalog import get_shop_metafield
                    text = get_shop_metafield(connection, namespace='custom', key='howdoesitwork_connect')
                except Exception:
                    text = None

        return JsonResponse({"text": text or DEFAULT_STORE_CONNECTION_TEXT})


class ProductsHelpTextView(View):
    def get(self, request):
        email = request.GET.get('email')
        text = None

        if email:
            connection = ClientStoreConnection.objects.filter(
                client_email=email, platform='SHOPIFY', is_active=True
            ).first()
            if connection:
                try:
                    from shopify_integration.services.shopify_catalog import get_shop_metafield
                    text = get_shop_metafield(connection, namespace='crowdlink', key='products_help_text')
                except Exception:
                    text = None

        return JsonResponse({"text": text or DEFAULT_PRODUCTS_HELP_TEXT})


DEFAULT_CATEGORY_OPTIONS = [choice[0] for choice in CATEGORY_CHOICES]


class CategoryOptionsView(View):
    def get(self, request):
        email = request.GET.get('email')
        categories = None

        if email:
            connection = ClientStoreConnection.objects.filter(
                client_email=email, platform='SHOPIFY', is_active=True
            ).first()
            if connection:
                try:
                    from shopify_integration.services.shopify_catalog import get_shop_metafield
                    raw = get_shop_metafield(connection, namespace='custom', key='dropmenujson_category')
                    if raw:
                        parsed = json.loads(raw)
                        if (
                            isinstance(parsed, list)
                            and len(parsed) > 0
                            and all(isinstance(item, str) for item in parsed)
                        ):
                            categories = parsed
                except Exception:
                    categories = None

        return JsonResponse({"categories": categories if categories is not None else DEFAULT_CATEGORY_OPTIONS})

class BundleProductsView(View):
    """Returns all non-bundle products for the connection to populate the 'Define Bundle' UI picker."""
    def get(self, request):
        email = request.GET.get('email')
        product_id = request.GET.get('product_id')
        
        if not email or not product_id:
            return JsonResponse({"error": "email and product_id parameters are required"}, status=400)
            
        product = Product.objects.filter(id=product_id, connection__client_email=email).first()
        if not product:
            return JsonResponse({"products": []})
            
        # Exclude other bundles AND the current product itself from the dropdown options
        products = Product.objects.filter(connection=product.connection).exclude(category='Define Bundle').exclude(id=product.id).order_by('title')
        data = [
            {
                "id": str(p.id), 
                "title": p.title, 
                "category": p.category, 
                "image_url": p.image_url
            } 
            for p in products
        ]
        return JsonResponse({"products": data})


DEFAULT_SALES_HELP_TEXT = (
    "All sales paid by client credit card or POS-tap (Apple Pay etc) through your connected webstore / POS account appear here.\n"
    "Assign sales to the places in your Locations tab and make notes you might find useful later."
)


class SalesHelpTextView(View):
    def get(self, request):
        email = request.GET.get('email')
        text = None

        if email:
            connection = ClientStoreConnection.objects.filter(
                client_email=email, platform='SHOPIFY', is_active=True
            ).first()
            if connection:
                try:
                    from shopify_integration.services.shopify_catalog import get_shop_metafield
                    text = get_shop_metafield(connection, namespace='crowdlink', key='sales_help_text')
                except Exception:
                    text = None

        return JsonResponse({"text": text or DEFAULT_SALES_HELP_TEXT})


def _serialize_transaction(d):
    """One row of the (uneditable) expanded transaction listing under a summary row."""
    s = d.sale
    product = d.variant.product if d.variant else None
    
    saved_cat = d.product_category or ""
    if saved_cat.startswith("Bundle-") or saved_cat == "Define Bundle":
        category = saved_cat                      # strictly keep bundle markers
    else:
        category = (product.category if product and product.category else d.product_category)
        
    title = (product.title if product else d.product_title) or "Unknown"

    payment_str = s.payment_type.replace('_', ' ').title() if s.payment_type else "Unknown"
    if payment_str == "Credit Card":
        payment_str = "Card"

    # Show date + time in the client's timezone (DB stores UTC).
    local_dt = s.sale_date.astimezone(_client_tz()) if s.sale_date else None
    unit_price = float(d.price) if d.price is not None else None
    line_total = round(unit_price * d.sales_units, 2) if unit_price is not None else None

    return {
        "id": str(d.id),
        "sale_date": local_dt.strftime('%d %b %Y') if local_dt else "Unknown",
        "sale_time": local_dt.strftime('%I:%M %p').lstrip('0') if local_dt else "",
        "sale_datetime_iso": local_dt.isoformat() if local_dt else None,
        "system_location": s.system_location or "Unknown",
        "sales_units": d.sales_units,
        "payment_type": payment_str,
        "type": s.type or "",
        "product_title": title,
        "product_category": category,
        "is_album": is_category_aria_reportable(category),
        "price": unit_price,
        "line_total": line_total,
        "external_order_id": s.external_order_id or "",
    }


@method_decorator(csrf_exempt, name='dispatch')
class SalesView(View):
    """
    Returns one row per (connection, calendar date, sale_channel).
    Reads order headers from Sale and line items from SaleDetail -> Variant -> Product.
    """
    def get(self, request):
        email = request.GET.get('email')
        if not email:
            return JsonResponse({"error": "email parameter is required"}, status=400)

        # Filter for active connections only so hidden data doesn't show up in the UI
        connections = ClientStoreConnection.objects.filter(client_email=email, is_active=True)
        if not connections.exists():
            return JsonResponse({"sales": []})

        # Grouping, SSID stamping and the calculated SalesSummary columns are shared
        # with the post-sync refresh (core_api/utils/post_sync.py). User-entered
        # audience_size / notes / assigned_location are never written there.
        data = []
        for line in build_summary_groups(connections):
            summary = line["summary"]
            sale_day = line["sale_day"]
            bucket_details = line["details"]
            is_webstore = line["is_webstore"]
            data.append({
                "summary_id": str(summary.id),
                "ssid": summary.ssid,
                "sale_date": sale_day.strftime('%d %b %Y') if sale_day else "Unknown",
                "sale_date_iso": sale_day.isoformat() if sale_day else None,
                "type": summary.type or line["sale_type"] or "",
                "location": line["location_name"],
                "location_id": str(summary.assigned_location_id) if summary.assigned_location_id else None,
                "is_location_editable": not is_webstore,
                "albums_units": line["albums_units"],
                "merch_units": line["merch_units"],
                "audience_size": summary.audience_size,
                "notes": summary.notes if summary.notes is not None else "",
                "transaction_count": len(bucket_details),
                "transactions": [_serialize_transaction(d) for d in bucket_details],
            })

        return JsonResponse({"sales": data})


@method_decorator(csrf_exempt, name='dispatch')
class SalesSummaryDetailView(View):
    """
    Handles updating the fields the client assigns at the summary-row level:
    - location_id (a SalesLocation id, or null/"unknown" to clear it)
    - audience_size
    - notes
    Summary rows are created on-demand by SalesView.get(), so this only ever
    PATCHes a row that already exists.
    """
    def patch(self, request, summary_id):
        email = request.GET.get('email')
        if not email:
            return JsonResponse({"error": "email parameter is required for authorization"}, status=401)

        try:
            summary = SalesSummary.objects.get(id=summary_id, connection__client_email=email)
        except SalesSummary.DoesNotExist:
            return JsonResponse({"error": "Sales summary not found or not authorized"}, status=404)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        updated_fields = []

        if 'location_id' in payload:
            loc_id = payload['location_id']
            if loc_id in (None, '', 'unknown', 'Unknown'):
                summary.assigned_location = None
                summary.locid = None
                # If clearing the override, recompute location name from
                # the day's sales (auto-detect) or fallback to "Unknown".
                if summary.type and 'Webstore' in summary.type:
                    summary.location = "Webstore Sales"
                else:
                    generic_values = {'N/A', 'Unknown', '', None, 'Shopify POS', 'Square POS', 'Shopify Webstore', 'Square Webstore'}
                    # To match AEST groupings, we filter by the AEST calendar day bounds
                    import datetime
                    from core_api.utils.post_sync import _client_tz
                    from core_api.utils.post_sync import local_dt
                    start_dt = local_dt(summary.summary_date, datetime.time.min)
                    end_dt = local_dt(summary.summary_date, datetime.time.max)
                    day_sales = Sale.objects.filter(
                        connection=summary.connection,
                        sale_date__range=(start_dt, end_dt),
                        type=summary.type,
                    )
                    actual_locations = {
                        s.actual_location for s in day_sales
                        if s.actual_location and s.actual_location not in generic_values
                    }
                    if len(actual_locations) == 1:
                        summary.location = actual_locations.pop()
                    else:
                        summary.location = "Unknown"
                updated_fields.extend(['location', 'locid'])
            else:
                try:
                    location = SalesLocation.objects.get(
                        models.Q(connection__client_email=email) | models.Q(client_email=email),
                        id=loc_id,
                    )
                except SalesLocation.DoesNotExist:
                    return JsonResponse({"error": "Location not found or not authorized"}, status=404)
                summary.assigned_location = location
                summary.locid = location.locid
                summary.location = location.name
                updated_fields.extend(['location', 'locid'])
            updated_fields.append('assigned_location')

        if 'audience_size' in payload:
            aud = payload['audience_size']
            if aud in (None, ''):
                summary.audience_size = None
            else:
                try:
                    summary.audience_size = int(aud)
                except (ValueError, TypeError):
                    return JsonResponse({"error": "audience_size must be an integer or null"}, status=400)
            updated_fields.append('audience_size')

        if 'notes' in payload:
            notes_val = payload['notes']
            summary.notes = notes_val if notes_val is not None else ""
            updated_fields.append('notes')

        if updated_fields:
            # Dedupe in case 'location' added twice
            updated_fields = list(dict.fromkeys(updated_fields))
            updated_fields.append('updated_at')
            summary.save(update_fields=updated_fields)
            audit.info("SUMMARY_UPDATE email=%s summary=%s date=%s fields=%s", email, summary.id,
                       summary.summary_date, [f for f in updated_fields if f != 'updated_at'])

        return JsonResponse({
            "status": "success",
            "id": str(summary.id),
            "location_id": str(summary.assigned_location_id) if summary.assigned_location_id else None,
            "location": summary.location if summary.location else "Unknown",
            "audience_size": summary.audience_size,
            "notes": summary.notes,
            "albums_units": summary.albums_units,
            "merch_units": summary.merch_units,
        })


from decimal import Decimal
from core_api.models import SalesLocation


DEFAULT_LOCATIONS_HELP_TEXT = (
    "If you use Locations in your Shopify or Square store then CrowdLink will capture that data. "
    "If not, you can create an event location here then use it in the Sales section.\n"
    "Note: Spending a little time to set up locations and assign them to sales opens up "
    "geo-location Insights - so it's well worth your time."
)


# ---------------------------------------------------------------------------
# Sync tab: [Run Manual Sync]
#   POST /api/account/sync/manual/?email=...  -> starts the sync (202) and returns
#                                                 immediately; it can take minutes
#   GET  /api/account/sync/manual/?email=...  -> {"state": idle|running|done|error}
# The extension polls the GET until state leaves "running".
# ---------------------------------------------------------------------------
@method_decorator(csrf_exempt, name='dispatch')
class ManualSyncView(View):
    @staticmethod
    def _email(request):
        email = request.GET.get('email')
        if not email and request.body:
            try:
                email = (json.loads(request.body) or {}).get('email')
            except (json.JSONDecodeError, AttributeError):
                email = None
        return (email or '').strip()

    def get(self, request):
        email = self._email(request)
        if not email:
            return JsonResponse({"error": "email parameter is required"}, status=400)
        payload = dict(manual_sync_status(email))
        last = (
            ClientStoreConnection.objects.filter(client_email=email, is_active=True)
            .aggregate(last=models.Max('last_sync_at'))['last']
        )
        payload["last_sync_at"] = last.isoformat() if last else None
        return JsonResponse(payload)

    def post(self, request):
        email = self._email(request)
        if not email:
            return JsonResponse({"error": "email parameter is required"}, status=400)
        result = start_manual_sync(email)
        audit.info("MANUAL_SYNC_REQUEST email=%s state=%s", email, result.get("state"))
        if result.get("state") == "no_connection":
            return JsonResponse({"error": "No connected store to sync"}, status=404)
        return JsonResponse(result, status=202)


class LocationsHelpTextView(View):
    def get(self, request):
        email = request.GET.get('email')
        text = None

        if email:
            connection = ClientStoreConnection.objects.filter(
                client_email=email, platform='SHOPIFY', is_active=True
            ).first()
            if connection:
                try:
                    from shopify_integration.services.shopify_catalog import get_shop_metafield
                    text = get_shop_metafield(connection, namespace='crowdlink', key='locations_help_text')
                except Exception:
                    text = None

        return JsonResponse({"text": text or DEFAULT_LOCATIONS_HELP_TEXT})


@method_decorator(csrf_exempt, name='dispatch')
class LocationsListView(View):
    """Handles GET (list with filters) and POST (create custom CrowdLink location)."""
    def get(self, request):
        email = request.GET.get('email')
        if not email:
            return JsonResponse({"error": "email parameter is required"}, status=400)

        connections = ClientStoreConnection.objects.filter(client_email=email, is_active=True)

        qs = SalesLocation.objects.filter(
            models.Q(connection__in=connections) | models.Q(client_email=email),
            is_active=True
        ).order_by('platform', '-created_at')

        search = request.GET.get('search', '').strip().lower()
        location_status = request.GET.get('location_status', 'any').lower()
        platform = request.GET.get('platform', 'any').upper()

        if search:
            qs = qs.filter(
                models.Q(name__icontains=search) |
                models.Q(formatted_address__icontains=search) |
                models.Q(address1__icontains=search)
            )

        if location_status == 'found':
            qs = qs.filter(latitude__isnull=False, longitude__isnull=False)
        elif location_status == 'not_yet_found':
            qs = qs.filter(models.Q(latitude__isnull=True) | models.Q(longitude__isnull=True))

        if platform != 'ANY':
            qs = qs.filter(platform=platform)

        data = []
        for loc in qs:
            has_geo = loc.latitude is not None and loc.longitude is not None
            geo_text = f"Found it! {abs(loc.latitude):.3f}°{'S' if loc.latitude < 0 else 'N'}, {abs(loc.longitude):.3f}°{'E' if loc.longitude >= 0 else 'W'}" if has_geo else None

            data.append({
                "id": str(loc.id),
                "name": loc.name,
                "platform": loc.platform,
                "is_editable_name": loc.platform == 'CROWDLINK',
                "is_editable_address": loc.platform in ['SHOPIFY', 'SQUARE', 'CROWDLINK'],
                "formatted_address": loc.formatted_address or "",
                "address1": loc.address1 or "",
                "city": loc.city or "",
                "province": loc.province or "",
                "postal_code": loc.postal_code or "",
                "latitude": float(loc.latitude) if loc.latitude else None,
                "longitude": float(loc.longitude) if loc.longitude else None,
                "has_geo": has_geo,
                "geo_label": geo_text or "Geo-location not found. Please use drop menu to select address from list.",
            })

        return JsonResponse({"locations": data})

    def post(self, request):
        """Creates a new CrowdLink custom location."""
        email = request.GET.get('email')
        if not email:
            return JsonResponse({"error": "email parameter is required"}, status=400)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        name = payload.get('name', '').strip()
        if not name:
            return JsonResponse({"error": "Location name is required"}, status=400)

        lat = payload.get('latitude')
        lng = payload.get('longitude')

        formatted_address = payload.get('formatted_address', '')
        city = payload.get('city', '')
        province = payload.get('province', '')
        postal_code = payload.get('postal_code', '')
        locid = None

        if formatted_address:
            from core_api.models import StandardLocality
            match = StandardLocality.objects.filter(locality=formatted_address).first()
            if match:
                city = match.suburb or city
                province = match.state or province
                postal_code = match.postcode or postal_code
                locid = match.locid

        # Grab the active connection to fulfill the client requirement
        connection = ClientStoreConnection.objects.filter(client_email=email, is_active=True).first()

        location = SalesLocation.objects.create(
            client_email=email,
            connection=connection,
            platform='CROWDLINK',
            name=name,
            formatted_address=formatted_address,
            address1=payload.get('address1', ''),
            city=city,
            province=province,
            postal_code=postal_code,
            locid=locid,
            country=payload.get('country', 'AU'),
            latitude=Decimal(str(lat)) if lat is not None else None,
            longitude=Decimal(str(lng)) if lng is not None else None,
            is_active=True
        )
        audit.info("LOCATION_CREATE email=%s location=%s name=%s", email, location.id, location.name)

        return JsonResponse({
            "status": "success",
            "id": str(location.id),
            "name": location.name,
            "formatted_address": location.formatted_address
        }, status=201)


@method_decorator(csrf_exempt, name='dispatch')
class LocationDetailView(View):
    """Handles updating address and coordinates, and renaming (CrowdLink only)."""
    def patch(self, request, location_id):
        email = request.GET.get('email')
        if not email:
            return JsonResponse({"error": "email parameter is required"}, status=400)

        try:
            location = SalesLocation.objects.get(
                models.Q(connection__client_email=email) | models.Q(client_email=email),
                id=location_id,
                is_active=True
            )
        except SalesLocation.DoesNotExist:
            return JsonResponse({"error": "Location not found"}, status=404)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        if 'name' in payload and location.platform == 'CROWDLINK':
            location.name = payload['name'].strip() or location.name

        if 'formatted_address' in payload:
            new_address = payload.get('formatted_address')
            location.formatted_address = new_address
            location.address1 = payload.get('address1', location.address1)
            location.city = payload.get('city', location.city)
            location.province = payload.get('province', location.province)
            location.postal_code = payload.get('postal_code', location.postal_code)

            if new_address:
                from core_api.models import StandardLocality
                match = StandardLocality.objects.filter(locality=new_address).first()
                if match:
                    location.city = match.suburb or location.city
                    location.province = match.state or location.province
                    location.postal_code = match.postcode or location.postal_code
                    location.locid = match.locid

            import os
            import urllib.parse
            from django.conf import settings

            google_api_key = getattr(settings, 'GOOGLE_MAPS_API_KEY', os.environ.get('GOOGLE_MAPS_API_KEY', ''))

            if new_address and google_api_key:
                import requests
                try:
                    encoded_address = urllib.parse.quote(new_address)
                    geo_url = f"https://maps.googleapis.com/maps/api/geocode/json?address={encoded_address}&key={google_api_key}"

                    geo_res = requests.get(geo_url, timeout=5)
                    if geo_res.ok:
                        geo_data = geo_res.json()
                        if geo_data.get('status') == 'OK' and geo_data.get('results'):
                            coords = geo_data['results'][0]['geometry']['location']
                            location.latitude = Decimal(str(coords['lat']))
                            location.longitude = Decimal(str(coords['lng']))
                except Exception as e:
                    audit.warning("GEOCODE_FAILED email=%s location=%s address=%r error=%s",
                                  email, location.id, new_address, e)

        if 'latitude' in payload and payload['latitude'] is not None:
            location.latitude = Decimal(str(payload['latitude']))

        if 'longitude' in payload and payload['longitude'] is not None:
            location.longitude = Decimal(str(payload['longitude']))

        location.save()
        audit.info("LOCATION_UPDATE email=%s location=%s name=%s fields=%s", email, location.id,
                   location.name, sorted(payload.keys()))

        has_geo = location.latitude is not None and location.longitude is not None
        geo_text = f"Found it! {abs(location.latitude):.3f}°{'S' if location.latitude < 0 else 'N'}, {abs(location.longitude):.3f}°{'E' if location.longitude >= 0 else 'W'}" if has_geo else None

        return JsonResponse({
            "status": "success",
            "id": str(location.id),
            "name": location.name,
            "formatted_address": location.formatted_address,
            "has_geo": has_geo,
            "geo_label": geo_text or "Geo-location not found. Please use drop menu to select address from list."
        })

    def delete(self, request, location_id):
        """Soft deletes custom CrowdLink locations."""
        email = request.GET.get('email')
        try:
            location = SalesLocation.objects.get(
                client_email=email,
                id=location_id,
                platform='CROWDLINK'
            )
            location.is_active = False
            location.save()
            audit.info("LOCATION_DELETE email=%s location=%s name=%s", email, location.id, location.name)
            return JsonResponse({"status": "success"})
        except SalesLocation.DoesNotExist:
            return JsonResponse({"error": "Location not found or cannot be deleted"}, status=403)


from core_api.models import StandardLocality

@method_decorator(csrf_exempt, name='dispatch')
class LocationAutocompleteView(View):
    """Native predictive text search querying our own standardized data."""
    def get(self, request):
        try:
            query = request.GET.get('query', '').strip()
            if not query or len(query) < 2:
                return JsonResponse({"predictions": []})

            qs = StandardLocality.objects.filter(locid__isnull=False)
            
            for term in query.split():
                qs = qs.filter(
                    models.Q(suburb__icontains=term) | models.Q(postcode__startswith=term)
                )
                
            qs = qs.order_by('suburb')[:10]

            predictions = []
            for loc in qs:
                predictions.append({
                    "id": str(loc.locid) if loc.locid else "",
                    "description": loc.locality or "",
                    "latitude": float(loc.latitude) if loc.latitude is not None else None,
                    "longitude": float(loc.longitude) if loc.longitude is not None else None,
                })

            return JsonResponse({"predictions": predictions})
        except Exception:
            return JsonResponse({"predictions": []})


def run_migrations(request):
    """View to run database migrations and print the actual terminal output"""
    out = io.StringIO()
    audit.warning("MIGRATE_DB_CALLED ip=%s ua=%r", request.META.get("REMOTE_ADDR"),
                  request.META.get("HTTP_USER_AGENT", "")[:120])
    try:
        # Pre-clean legacy duplicate rows so the new unique constraints on
        # Product(connection, external_product_id) and Sale(external_order_id) apply cleanly.
        from django.db import connection as db_conn
        with db_conn.cursor() as cursor:
            try:
                cursor.execute("SELECT id, external_order_id FROM core_api_sale")
                seen_orders = set()
                for row_id, ext_id in cursor.fetchall():
                    if ext_id in seen_orders:
                        cursor.execute("DELETE FROM core_api_sale WHERE id = %s", [row_id])
                    else:
                        seen_orders.add(ext_id)
            except Exception:
                pass

            try:
                cursor.execute("DELETE FROM core_api_standardlocality WHERE locid IS NULL")
            except Exception:
                pass

            # MySQL/InnoDB won't let Django drop 'unique_sales_summary_per_day_channel'
            # because it's currently the only index covering the connection_id FK.
            # Give that FK its own plain index first so the drop can proceed.
            try:
                cursor.execute(
                    "CREATE INDEX core_api_salessummary_connection_id_idx "
                    "ON core_api_salessummary (connection_id)"
                )
            except Exception:
                pass  # already exists

            try:
                cursor.execute("SELECT id, connection_id, external_product_id FROM core_api_product")
                seen_prods = set()
                for row_id, conn_id, ext_pid in cursor.fetchall():
                    key = (conn_id, ext_pid)
                    if ext_pid and key in seen_prods:
                        try:
                            cursor.execute("DELETE FROM core_api_sale WHERE product_id = %s", [row_id])
                        except Exception:
                            pass
                        cursor.execute("DELETE FROM core_api_product WHERE id = %s", [row_id])
                    else:
                        seen_prods.add(key)
            except Exception:
                pass

        call_command('makemigrations', 'core_api', interactive=False, stdout=out)
        call_command('migrate', interactive=False, stdout=out)
        audit.warning("MIGRATE_DB_DONE")
        return HttpResponse(f"<pre>MIGRATION OUTPUT:\n{out.getvalue()}</pre>")
    except Exception as e:
        audit.exception("MIGRATE_DB_FAILED")
        return HttpResponse(f"<pre>ERROR:\n{e}\n\nOUTPUT SO FAR:\n{out.getvalue()}</pre>")
    except SystemExit as e:
        return HttpResponse(f"<pre>CRITICAL MIGRATION HALT:\nDjango attempted to ask a Yes/No question in the terminal (likely about a default value), but couldn't because it's running in the browser. \n\nOUTPUT SO FAR:\n{out.getvalue()}</pre>")


import csv
import os
from decimal import Decimal, InvalidOperation
from core_api.models import StandardLocality
from django.contrib.admin.views.decorators import staff_member_required


@staff_member_required
def import_localities_csv(request):
    """
    1-Click Importer: Reads 'clau_localities.csv' from the project root
    and saves all 11 columns exactly into the StandardLocality table using the built-in CSV reader.
    """
    from django.conf import settings

    file_path = os.path.join(settings.BASE_DIR, 'clau_localities.csv')

    if not os.path.exists(file_path):
        return HttpResponse(
            f"<h3>Error: File Not Found</h3>"
            f"<p>Could not find <b>clau_localities.csv</b>.</p>"
            f"<p>Please save your Excel file as a CSV, and upload it via cPanel File Manager to: <br/><code>{settings.BASE_DIR}</code></p>"
        )

    imported_count = 0
    try:
        with open(file_path, mode='r', encoding='utf-8-sig') as file:
            reader = csv.DictReader(file)

            for row in reader:
                locid_str = row.get('LOCID', '').strip()
                locality = row.get('LOCALITY DROP MENU', row.get('LOCALITY', '')).strip()
                suburb = row.get('Suburb', '').strip()
                postcode = row.get('Postcode', '').strip()
                state = row.get('State', '').strip()
                lat_str = row.get('Latitude', '').strip()
                lng_str = row.get('Longitude', '').strip()
                
                location_class = row.get('Location Class', '').strip()
                region = row.get('Region', '').strip()
                lga = row.get('LGA', '').strip()
                federal_electorate = row.get('Federal Electorate', '').strip()

                if not locid_str or not suburb or not postcode:
                    continue

                try:
                    locid = int(locid_str)
                except ValueError:
                    continue

                try:
                    lat = Decimal(lat_str) if lat_str else None
                    lng = Decimal(lng_str) if lng_str else None
                except InvalidOperation:
                    lat = None
                    lng = None

                StandardLocality.objects.update_or_create(
                    locid=locid,
                    defaults={
                        'locality': locality,
                        'suburb': suburb,
                        'postcode': postcode,
                        'state': state,
                        'latitude': lat,
                        'longitude': lng,
                        'location_class': location_class,
                        'region': region,
                        'lga': lga,
                        'federal_electorate': federal_electorate
                    }
                )
                imported_count += 1

        audit.warning("IMPORT_CSV_DONE ip=%s rows=%s", request.META.get("REMOTE_ADDR"), imported_count)
        return HttpResponse(f"<h3>SUCCESS!</h3><p>Successfully imported <b>{imported_count}</b> official CLAU localities into the database.</p>")

    except Exception as e:
        audit.exception("IMPORT_CSV_FAILED")
        return HttpResponse(f"<h3>ERROR</h3><p>{str(e)}</p>")