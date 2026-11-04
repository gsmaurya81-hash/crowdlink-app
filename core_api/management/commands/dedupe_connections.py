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
dates) is re-run. Reported sales keep their reporting dates (refresh skips them).
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


def pick_keeper(conns, keep_emails=()):
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
    other_ids = [o.pk for o in others]
    stats = {"deactivated": 0, "sales_moved": 0, "lines_repointed": 0,
             "lines_dupes_deleted": 0, "lines_unmatched": 0}
    with transaction.atomic():
        stats["deactivated"] = (ClientStoreConnection.objects
                                .filter(pk__in=other_ids, is_active=True).update(is_active=False))
        stats["sales_moved"] = Sale.objects.filter(connection_id__in=other_ids).update(connection=keeper)

        stale = (SaleDetail.objects.filter(sale__connection=keeper)
                 .exclude(variant__connection=keeper).select_related("variant"))
        for d in list(stale):
            sku = d.variant.sku_or_barcode
            target = (Variant.objects.filter(connection=keeper, sku_or_barcode=sku).first()
                      if sku else None)
            if target is None:
                stats["lines_unmatched"] += 1
                continue
            if SaleDetail.objects.filter(sale_id=d.sale_id, variant=target).exclude(pk=d.pk).exists():
                d.delete()
                stats["lines_dupes_deleted"] += 1
            else:
                d.variant, d.connection = target, keeper
                d.save(update_fields=["variant", "connection"])
                stats["lines_repointed"] += 1
        SaleDetail.objects.filter(sale__connection=keeper).exclude(connection=keeper).update(connection=keeper)
    return stats


class Command(BaseCommand):
    help = "Report (default) or fix stores connected more than once. Use --apply to fix."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Make the changes (default: report only)")
        parser.add_argument("--keep-email", action="append", default=[],
                            help="Account to keep when it is in a duplicate group (repeatable)")
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
            keeper = pick_keeper(conns, opts["keep_email"])
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
                    audit.info("DEDUPE_REFRESH_OK keeper=%s", keeper.pk)
                except Exception as exc:
                    self.stdout.write(f"  keeper post-sync refresh FAILED: {exc} (re-run a manual sync)")
                    audit.error("DEDUPE_REFRESH_FAILED keeper=%s error=%s", keeper.pk, exc, exc_info=True)
            self.stdout.write("")
