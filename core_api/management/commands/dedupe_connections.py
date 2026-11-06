"""
python manage.py dedupe_connections                      # REPORT ONLY (default, changes nothing)
python manage.py dedupe_connections --keep-email scott@replicat.com.au --apply

Finds stores (platform + store_identifier) that have more than one
ClientStoreConnection, picks ONE keeper per store and repairs the data.

Keeper: --keep-email wins if that account is in the group; otherwise active
first, then most recently synced, then oldest.

--apply, per store, in one transaction:
  1. every non-keeper connection is set is_active=False (NOT deleted - their
     rows stay in the database, they just stop syncing and stop showing);
  2. every Sale still pointing at a non-keeper is moved to the keeper;
  3. every SaleDetail on those sales that points at a non-keeper's Variant is
     re-pointed to the keeper's Variant with the same SKU/barcode. If the sale
     already has a line for that variant, the stale copy is deleted. Lines with
     no matching keeper variant are left alone and counted in the report;
then the keeper's post-sync refresh (bundles, SSID, SalesSummary, reporting
dates) is re-run and the keeper's summaries that have no sales behind them
(stale leftovers, incl. old SSIDs) are deleted. Reported sales keep their reporting dates (refresh skips them).
"""
import logging
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from core_api.models import (
    ClientStoreConnection, Product, Sale, SaleDetail, SalesSummary, Variant,
)


audit = logging.getLogger("crowdlink.audit")   # -> logs/app.log


def _rank(c):
    # active first, then most recent sync, then oldest connection
    return (not c.is_active, -(c.last_sync_at.timestamp() if c.last_sync_at else 0), c.created_at)


def pick_keeper(conns, keep_emails=(), keep_ids=()):
    for cid in keep_ids:
        match = [c for c in conns if str(c.pk).startswith(str(cid).lower())]
        if match:
            return match[0]
    for email in keep_emails:
        match = [c for c in conns if (c.client_email or "").lower() == email.lower()]
        if match:
            return sorted(match, key=_rank)[0]
    return sorted(conns, key=_rank)[0]


def duplicate_groups(platform=None, store=None):
    qs = ClientStoreConnection.objects.all().order_by("created_at")
    if platform:
        qs = qs.filter(platform=platform)
    if store:
        qs = qs.filter(store_identifier=store)
    groups = defaultdict(list)
    for c in qs:
        groups[(c.platform, c.store_identifier)].append(c)
    return {k: v for k, v in groups.items() if len(v) > 1}


def repair_group(keeper, others):
    """Apply the repair for one store. Returns a stats dict."""
    from core_api.models import SalesLocation
    other_ids = [o.pk for o in others]
    stats = {"deactivated": 0, "sales_moved": 0, "lines_repointed": 0,
             "lines_dupes_deleted": 0, "lines_unmatched": 0, "settings_copied": 0}
    with transaction.atomic():
        stats["deactivated"] = (ClientStoreConnection.objects
                                .filter(pk__in=other_ids, is_active=True).update(is_active=False))
        
        # Copy Product settings
        keeper_products = {p.external_product_id: p for p in Product.objects.filter(connection=keeper)}
        # json_bundle holds Product UUIDs, and those belong to ONE connection. Copying
        # it verbatim left the keeper pointing at the other connection's products
        # ("product not found" in the bundle editor, and every save rejected).
        # Translate each id through external_product_id; drop the ones with no match.
        other_ext_by_pk = {str(p.pk): p.external_product_id
                           for p in Product.objects.filter(connection_id__in=other_ids)}
        keeper_pk_by_ext = {ext: str(p.pk) for ext, p in keeper_products.items()}

        def _remap_bundle(bundle):
            out = []
            for item in bundle or []:
                ext = other_ext_by_pk.get(str(item.get("product_id")))
                new_pk = keeper_pk_by_ext.get(ext)
                if new_pk:
                    out.append({**item, "product_id": new_pk})
            return out
        for other_prod in Product.objects.filter(connection_id__in=other_ids):
            kp = keeper_products.get(other_prod.external_product_id)
            if kp:
                changed = False
                if other_prod.category and not kp.category:
                    kp.category, changed = other_prod.category, True
                # Never copy release_date or turn reporting ON for the keeper:
                # both may be deliberate client choices, not empty defaults.
                # User switched reporting OFF on the old row; keeper still has the
                # untouched default (True, nothing else set) -> carry the OFF over.
                if (not other_prod.report_to_aria and kp.report_to_aria
                      and not kp.category and not kp.release_date):
                    kp.report_to_aria, changed = False, True
                if other_prod.json_bundle and not kp.json_bundle:
                    remapped = _remap_bundle(other_prod.json_bundle)
                    if remapped:
                        kp.json_bundle, changed = remapped, True
                if changed:
                    kp.save(update_fields=['category', 'release_date', 'report_to_aria', 'json_bundle'])
                    stats["settings_copied"] += 1

        # Copy Summary settings
        keeper_summaries = {(s.summary_date, s.type): s for s in SalesSummary.objects.filter(connection=keeper)}
        for other_sum in SalesSummary.objects.filter(connection_id__in=other_ids):
            ks = keeper_summaries.get((other_sum.summary_date, other_sum.type))
            if ks:
                changed = False
                if other_sum.audience_size and not ks.audience_size:
                    ks.audience_size, changed = other_sum.audience_size, True
                if other_sum.notes and not ks.notes:
                    ks.notes, changed = other_sum.notes, True
                if other_sum.assigned_location_id and not ks.assigned_location_id:
                    ks.assigned_location_id, changed = other_sum.assigned_location_id, True
                if changed:
                    ks.save(update_fields=['audience_size', 'notes', 'assigned_location_id'])
                    stats["settings_copied"] += 1

        # Copy Location settings
        keeper_locations = {l.external_location_id: l for l in SalesLocation.objects.filter(connection=keeper)}
        for other_loc in SalesLocation.objects.filter(connection_id__in=other_ids):
            kl = keeper_locations.get(other_loc.external_location_id)
            if kl:
                changed = False
                if other_loc.formatted_address and not kl.formatted_address:
                    kl.formatted_address, changed = other_loc.formatted_address, True
                if other_loc.locid and not kl.locid:
                    kl.locid, changed = other_loc.locid, True
                if other_loc.latitude and not kl.latitude:
                    kl.latitude, kl.longitude, changed = other_loc.latitude, other_loc.longitude, True
                if changed:
                    kl.save(update_fields=['formatted_address', 'locid', 'latitude', 'longitude'])

        stats["sales_moved"] = Sale.objects.filter(connection_id__in=other_ids).update(connection=keeper)

        # Match each stale line to the keeper's variant by platform id first, then SKU, so
        # variants WITHOUT a SKU are matched too (they used to be left behind as "unmatched").
        from core_api.utils.line_cleanup import fix_cross_connection_lines
        fixed = fix_cross_connection_lines(SaleDetail, Variant, connection_id=keeper.pk, include_reported=True)
        stats["lines_repointed"] = fixed["repointed"]
        stats["lines_dupes_deleted"] = fixed["deleted"]
        stats["lines_unmatched"] = fixed["unmatched"]
        SaleDetail.objects.filter(sale__connection=keeper).exclude(connection=keeper).update(connection=keeper)
    return stats


def prune_orphan_summaries(keeper):
    """Delete the keeper's SalesSummary rows that no sale backs any more
    (e.g. leftovers from before the sales were moved onto the keeper).
    Uses the same grouping as the Sales tab, so a summary is kept iff it
    still has at least one sale for its (date, type). Returns the count."""
    from core_api.utils.post_sync import build_summary_groups
    live_ids = {line["summary"].pk for line in build_summary_groups([keeper])}
    orphans = SalesSummary.objects.filter(connection=keeper).exclude(pk__in=live_ids)
    count = orphans.count()
    orphans.delete()
    return count


class Command(BaseCommand):
    help = "Report (default) or fix stores connected more than once. Use --apply to fix."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Make the changes (default: report only)")
        parser.add_argument("--keep-email", action="append", default=[],
                            help="Account to keep when it is in a duplicate group (repeatable)")
        parser.add_argument("--keep-id", action="append", default=[],
                            help="Connection id (or its first 8 chars) to keep, e.g. 903f0670 (repeatable)")
        parser.add_argument("--platform", choices=["SHOPIFY", "SQUARE"])
        parser.add_argument("--store", help="Only this store_identifier")

    def handle(self, *args, **opts):
        groups = duplicate_groups(opts["platform"], opts["store"])
        if not groups:
            self.stdout.write("No store has more than one connection.")
            return
        mode = "apply" if opts["apply"] else "report"
        self.stdout.write(f"{len(groups)} store(s) with more than one connection "
                          f"({'APPLYING' if opts['apply'] else 'REPORT ONLY - nothing changed'})\n")
        audit.info("DEDUPE_START mode=%s stores=%s keep_email=%s platform=%s store=%s",
                   mode, len(groups), opts["keep_email"] or None, opts["platform"], opts["store"])

        for (platform, store), conns in sorted(groups.items()):
            keeper = pick_keeper(conns, opts["keep_email"], opts["keep_id"])
            others = [c for c in conns if c.pk != keeper.pk]
            self.stdout.write(f"=== {platform}  {store}  ({len(conns)} connections)")
            for c in conns:
                mark = "KEEP  " if c.pk == keeper.pk else "retire"
                synced = f"{c.last_sync_at:%Y-%m-%d %H:%M}" if c.last_sync_at else "never"
                self.stdout.write(f"  [{mark}] {c.client_email:<38} active={int(c.is_active)} last_sync={synced}")
                self.stdout.write(
                    f"           products={Product.objects.filter(connection=c).count()} "
                    f"sales={Sale.objects.filter(connection=c).count()} "
                    f"summaries={SalesSummary.objects.filter(connection=c).count()}")
            mismatched = sum(
                1 for d in SaleDetail.objects.filter(sale__connection__in=[c.pk for c in conns])
                .select_related("sale", "variant")
                if d.variant.connection_id != d.sale.connection_id)
            user_data = SalesSummary.objects.filter(connection__in=[o.pk for o in others]).exclude(
                audience_size__isnull=True, notes__isnull=True, assigned_location__isnull=True).count()
            self.stdout.write(f"  sale lines whose variant belongs to another connection: {mismatched}")
            if user_data:
                self.stdout.write(f"  NOTE: {user_data} summary row(s) on retired connections hold audience/notes/"
                                  f"location entries (kept in the DB, not copied to the keeper).")

            audit.info("DEDUPE_GROUP mode=%s platform=%s store=%s keeper=%s(%s) retire=%s mismatched_lines=%s "
                       "retired_summaries_with_user_data=%s",
                       mode, platform, store, keeper.client_email, keeper.pk,
                       [f"{o.client_email}({o.pk})" for o in others], mismatched, user_data)

            if opts["apply"]:
                try:
                    stats = repair_group(keeper, others)
                except Exception as exc:
                    audit.error("DEDUPE_FAILED platform=%s store=%s keeper=%s error=%s (group rolled back)",
                                platform, store, keeper.pk, exc, exc_info=True)
                    self.stdout.write(f"  repair FAILED and was rolled back: {exc}")
                    self.stdout.write("")
                    continue
                self.stdout.write(f"  repaired: {stats}")
                audit.info("DEDUPE_REPAIRED platform=%s store=%s keeper=%s stats=%s", platform, store, keeper.pk, stats)
                if stats["lines_unmatched"]:
                    audit.warning("DEDUPE_UNMATCHED_LINES platform=%s store=%s keeper=%s count=%s "
                                  "(sale lines left on a retired connection's variant - no SKU match)",
                                  platform, store, keeper.pk, stats["lines_unmatched"])
                try:
                    from core_api.utils.post_sync import refresh_after_sync
                    refresh_after_sync(keeper)
                    self.stdout.write("  keeper post-sync refresh: done")
                    removed = prune_orphan_summaries(keeper)
                    self.stdout.write(f"  stale summaries removed (no sales behind them): {removed}")
                    audit.info("DEDUPE_ORPHAN_SUMMARIES_REMOVED keeper=%s count=%s", keeper.pk, removed)
                    audit.info("DEDUPE_REFRESH_OK keeper=%s", keeper.pk)
                except Exception as exc:
                    self.stdout.write(f"  keeper post-sync refresh FAILED: {exc} (re-run a manual sync)")
                    audit.error("DEDUPE_REFRESH_FAILED keeper=%s error=%s", keeper.pk, exc, exc_info=True)
            self.stdout.write("")