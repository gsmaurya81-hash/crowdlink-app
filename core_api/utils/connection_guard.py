"""
core_api/utils/connection_guard.py

One store, one CrowdLink account.

A Shopify shop / Square merchant must be connected to exactly ONE CrowdLink
account (client_email). Before this guard the OAuth callbacks matched on
client_email, so a second account connecting the same store silently created a
second ClientStoreConnection. Every nightly sync then ran once per connection
and each run re-stamped the shared Sale rows (Sale.external_order_id is unique
across the whole table) with its own connection, leaving Sale/SaleDetail/
SalesSummary rows pointing at different connections' products and variants.

Use `store_conflict()` in the OAuth callbacks; use the `dedupe_connections`
management command to clean up duplicates that already exist.
"""
from django.db.models import Count
from django.http import HttpResponse

from core_api.models import ClientStoreConnection


def store_conflict(platform, store_identifier, email):
    """Another account's ACTIVE connection to the same store, or None.
    (A disconnected / inactive connection does not block a new owner.)"""
    if not store_identifier:
        return None
    qs = ClientStoreConnection.objects.filter(
        platform=platform, store_identifier=store_identifier, is_active=True)
    if email:
        qs = qs.exclude(client_email__iexact=email)
    return qs.first()


def active_duplicate_stores():
    """[(platform, store_identifier, n_active_connections)] for every store that
    still has more than one ACTIVE connection. Used by the nightly cron to warn."""
    return list(
        ClientStoreConnection.objects.filter(is_active=True)
        .values_list("platform", "store_identifier")
        .annotate(n=Count("id")).filter(n__gt=1)
        .order_by("platform", "store_identifier")
        .values_list("platform", "store_identifier", "n"))

def absorb_previous_owners(connection):
    """Call AFTER the new owner's first sync has finished."""
    from core_api.management.commands.dedupe_connections import repair_group
    from core_api.utils.post_sync import refresh_after_sync

    others = list(ClientStoreConnection.objects
                  .filter(platform=connection.platform,
                          store_identifier=connection.store_identifier)
                  .exclude(pk=connection.pk))
    if not others:
        return None
    stats = repair_group(connection, others)
    refresh_after_sync(connection)
    return stats
    
def conflict_response(platform):
    """Friendly page for the OAuth popup (HTTP 409). Deliberately does NOT say
    which account already owns the store."""
    label = "Shopify" if platform == "SHOPIFY" else "Square"
    html = f"""<!DOCTYPE html>
<html><head><title>Store already connected</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       display:flex; justify-content:center; align-items:center; height:100vh; margin:0; background:#f4f6f8; }}
.card {{ background:#fff; padding:2rem; border-radius:8px; box-shadow:0 4px 6px rgba(0,0,0,.1);
        text-align:center; max-width:420px; }}
h1 {{ color:#b98900; font-size:1.4rem; margin-bottom:1rem; }}
p {{ color:#6d7175; line-height:1.5; }}
</style></head>
<body><div class="card">
<h1>This {label} store is already connected</h1>
<p>It is already linked to another CrowdLink account, and a store can only be
connected to one account. Please contact CrowdLink support if you need it
moved. You can close this tab.</p>
</div></body></html>"""
    return HttpResponse(html, status=409)
