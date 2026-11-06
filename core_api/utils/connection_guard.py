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


def find_existing_connection(platform, store_identifier, email=None):
    """Finds any existing connection for this store, active or inactive.
    If an email is provided, it attempts to return that user's past
    connection first so they get their own custom settings back."""
    if not store_identifier:
        return None
    
    base_qs = ClientStoreConnection.objects.filter(platform=platform, store_identifier=store_identifier)

    # 1. This account's own past connection(s) to the store (case-insensitive).
    #    If there are several, take the one holding the most client settings,
    #    NOT simply the newest (a newer empty row must not win over the row
    #    that has the client's categories / release dates / audience / notes).
    if email:
        own = list(base_qs.filter(client_email__iexact=email.strip()))
        if own:
            return pick_richest(own)

    # 2. No row of their own: reuse the richest existing row for the store.
    return pick_richest(list(base_qs))


def settings_score(connection):
    """How much client-entered data hangs off a connection. Used only to choose
    between several rows for the same store."""
    from core_api.models import Product, SalesLocation, SalesSummary
    score = Product.objects.filter(connection=connection).exclude(
        category__isnull=True, release_date__isnull=True).count()
    score += Product.objects.filter(connection=connection, report_to_aria=False).count()
    score += SalesSummary.objects.filter(connection=connection).exclude(
        audience_size__isnull=True, notes__isnull=True, assigned_location__isnull=True).count()
    score += SalesLocation.objects.filter(connection=connection, locid__isnull=False).count()
    return score


def pick_richest(conns):
    """Most settings first, then active, then most recently synced, then oldest."""
    if not conns:
        return None
    def key(c):
        synced = c.last_sync_at.timestamp() if c.last_sync_at else 0
        return (-settings_score(c), not c.is_active, -synced, c.created_at)
    return sorted(conns, key=key)[0]


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
    """Make this ACTIVE connection the only holder of the store's data: move any
    sales still on INACTIVE connections of the same store onto it, carry client-entered
    summary data across, delete every summary left on those inactive connections, and
    delete this connection's summaries that have no sales behind them."""
    if not connection.is_active:
        return None
    from django.db import transaction
    from core_api.models import Sale, SalesSummary
    from core_api.management.commands.dedupe_connections import repair_group, prune_orphan_summaries
    from core_api.utils.post_sync import refresh_after_sync

    others = list(ClientStoreConnection.objects
                  .filter(platform=connection.platform,
                          store_identifier=connection.store_identifier,
                          is_active=False)
                  .exclude(pk=connection.pk))
    if not others:
        return None
    other_ids = [o.pk for o in others]
    stats = {}
    if Sale.objects.filter(connection_id__in=other_ids).exists():
        stats.update(repair_group(connection, others))
        refresh_after_sync(connection)

    with transaction.atomic():
        mine = {(s.summary_date, s.type): s for s in SalesSummary.objects.filter(connection=connection)}
        for old in SalesSummary.objects.filter(connection_id__in=other_ids):
            new = mine.get((old.summary_date, old.type))
            if not new:
                continue
            changed = False
            if old.audience_size and not new.audience_size:
                new.audience_size, changed = old.audience_size, True
            if old.notes and not new.notes:
                new.notes, changed = old.notes, True
            if old.assigned_location_id and not new.assigned_location_id:
                new.assigned_location_id, changed = old.assigned_location_id, True
            if changed:
                new.save(update_fields=["audience_size", "notes", "assigned_location_id"])
        stats["inactive_summaries_deleted"] = SalesSummary.objects.filter(
            connection_id__in=other_ids).delete()[0]
    stats["stale_summaries_removed"] = prune_orphan_summaries(connection)
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