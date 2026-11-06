"""
One-off clean-up after an inactive connection synced a store that also has an
ACTIVE connection (see other_connection_owns_sale in core_api/utils/post_sync.py):

  1. Sales still owned by an inactive connection are moved back to the store's
     active connection (repair_group also copies settings and re-points lines).
  2. Summary rows on inactive connections that hold NO client-entered data
     (audience size, notes, assigned location) are deleted. Rows that do hold
     client data are kept and listed.
  3. On each store whose sales were moved, the active connection's summaries that
     have no sales behind them (stale leftovers, old SSIDs) are deleted.

  python manage.py clean_inactive_summaries            # dry run - prints what it would do
  python manage.py clean_inactive_summaries --apply    # does it (take a DB backup first)
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from core_api.models import ClientStoreConnection, Sale, SalesSummary
from core_api.management.commands.dedupe_connections import repair_group, prune_orphan_summaries
from core_api.utils.post_sync import refresh_sales_summaries


class Command(BaseCommand):
    help = "Move sales back to the active connection and delete empty summaries on inactive connections."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Make the changes (default is a dry run)")

    def handle(self, *args, **opts):
        apply_changes = opts["apply"]
        mode = "APPLY" if apply_changes else "DRY RUN"
        touched_active = []

        # 1. stores with exactly one active connection whose inactive siblings still own sales
        for keeper in ClientStoreConnection.objects.filter(is_active=True):
            siblings = ClientStoreConnection.objects.filter(
                platform=keeper.platform, store_identifier=keeper.store_identifier, is_active=False)
            if ClientStoreConnection.objects.filter(
                    platform=keeper.platform, store_identifier=keeper.store_identifier,
                    is_active=True).count() != 1:
                self.stdout.write(self.style.WARNING(
                    "%s: more than one active connection for %s %s - skipped (run dedupe_connections)"
                    % (mode, keeper.platform, keeper.store_identifier)))
                continue
            sibling_ids = list(siblings.values_list("pk", flat=True))
            owned = Sale.objects.filter(connection_id__in=sibling_ids).count()
            if not owned:
                continue
            self.stdout.write("%s: %s sale(s) of %s %s are held by inactive connection(s) %s -> move to %s"
                              % (mode, owned, keeper.platform, keeper.store_identifier,
                                 [str(i)[:8] for i in sibling_ids], str(keeper.pk)[:8]))
            if apply_changes:
                stats = repair_group(keeper, list(siblings))
                self.stdout.write("   moved: %s" % stats)
                touched_active.append(keeper)

        # 2. empty summaries on inactive connections
        empty = SalesSummary.objects.filter(connection__is_active=False).filter(
            Q(audience_size__isnull=True), Q(notes__isnull=True) | Q(notes=""), Q(assigned_location__isnull=True))
        kept = SalesSummary.objects.filter(connection__is_active=False).exclude(pk__in=empty.values_list("pk", flat=True))
        self.stdout.write("%s: %s empty summary row(s) on inactive connections" % (mode, empty.count()))
        if kept.exists():
            self.stdout.write(self.style.WARNING(
                "%s: %s summary row(s) on inactive connections hold client data and are KEPT: %s"
                % (mode, kept.count(), [str(s.pk)[:8] for s in kept[:20]])))
        if apply_changes:
            deleted = empty.delete()[0]
            self.stdout.write(self.style.SUCCESS("deleted %s empty summary row(s)" % deleted))
            for keeper in touched_active:
                refresh_sales_summaries(keeper)
                removed = prune_orphan_summaries(keeper)
                self.stdout.write("   %s: stale summaries (no sales behind them) removed: %s"
                                  % (str(keeper.pk)[:8], removed))
        else:
            self.stdout.write("Dry run only - re-run with --apply to make the changes.")