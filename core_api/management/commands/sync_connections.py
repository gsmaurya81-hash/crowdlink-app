"""
Daily auto-sync for every ACTIVE store connection.

  python manage.py sync_connections                 # all active connections
  python manage.py sync_connections --email a@b.com # one client
  python manage.py sync_connections --platform SQUARE
  python manage.py sync_connections --force-initial # re-run the 30d/60d rule

cPanel cron (03:00 UTC daily) - use the app's virtualenv python:
  0 3 * * * cd /home/<user>/api.app.crowdmerch.com.au/crowdlink_app && \
      <venv>/bin/python manage.py sync_connections >> logs/cron_stdout.log 2>&1

The timestamped record of each run is written by this command itself to
logs/sync_cron.log (and logs/sync.log). cron_stdout.log only catches crashes that
happen before logging starts.
"""
import json
import logging
import os
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection as db_connection

from core_api.models import ClientStoreConnection
from core_api.utils.product_sync import run_connection_sync

log = logging.getLogger("crowdlink.cron")

# A store that fails this many nights in a row is flagged loudly in sync_cron.log.
REPEATED_FAILURE_THRESHOLD = 3
# Files written by shell redirects / crash handlers cannot rotate themselves.
UNROTATED_FILES = ("cron_stdout.log", "cron_sync.log")
MAX_UNROTATED_BYTES = 5 * 1024 * 1024


def _rotate_stray_logs(log_dir):
    """Keep the shell-redirect logs from growing forever (current -> .1, one backup)."""
    for name in UNROTATED_FILES:
        path = os.path.join(str(log_dir), name)
        try:
            if os.path.exists(path) and os.path.getsize(path) > MAX_UNROTATED_BYTES:
                os.replace(path, path + ".1")
                log.info("[CRON] rotated %s", name)
        except OSError as exc:
            log.warning("[CRON] could not rotate %s: %s", name, exc)


def _load_fail_counts(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_fail_counts(path, counts):
    try:
        with open(path, "w") as fh:
            json.dump(counts, fh, indent=1, sort_keys=True)
    except OSError as exc:
        log.warning("[CRON] could not save failure counters: %s", exc)


class Command(BaseCommand):
    help = "Sync sales, products and locations for all active connections."

    def add_arguments(self, parser):
        parser.add_argument("--email")
        parser.add_argument("--platform", choices=["SHOPIFY", "SQUARE"])
        parser.add_argument("--force-initial", action="store_true")

    def handle(self, *args, **opts):
        qs = ClientStoreConnection.objects.filter(is_active=True)
        if opts["email"]:
            qs = qs.filter(client_email=opts["email"])
        if opts["platform"]:
            qs = qs.filter(platform=opts["platform"])

        started = time.time()
        db = db_connection.settings_dict
        log.info("[CRON] START sync_connections db=%s name=%s pid=%s filters=%s",
                 db.get("ENGINE", "?").rsplit(".", 1)[-1], db.get("NAME"), os.getpid(),
                 {k: v for k, v in opts.items() if k in ("email", "platform", "force_initial") and v})
        # The web app uses MySQL when DJANGO_DB_HOST is set (from .env). If cron cannot
        # see it, Django silently falls back to SQLite and syncs the wrong database.
        if "sqlite" in db.get("ENGINE", "") and not os.environ.get("DJANGO_DB_HOST"):
            log.error("[CRON] WARNING running on SQLite (%s) because DJANGO_DB_HOST is not set - "
                      "check that .env is readable by cron and the right venv/cwd is used",
                      db.get("NAME"))

        _rotate_stray_logs(settings.LOG_DIR)
        # A store connected under more than one account makes every sync re-stamp the
        # same Sale rows. Block is in the OAuth callbacks; this flags any that remain.
        try:
            from core_api.utils.connection_guard import active_duplicate_stores
            for plat, store, n in active_duplicate_stores():
                log.warning("[CRON] DUPLICATE_STORE %s %s has %s active connections - "
                            "run: manage.py dedupe_connections", plat, store, n)
        except Exception:
            log.exception("[CRON] duplicate-store check failed (sync continues)")
        counts_path = os.path.join(str(settings.LOG_DIR), "fail_counts.json")
        fail_counts = _load_fail_counts(counts_path)
        repeated = []

        failures, failed_list, ok = 0, [], 0
        for conn in qs:
            try:
                result = run_connection_sync(conn, force_initial=opts["force_initial"])
            except Exception:
                # run_connection_sync reports errors in its result, but never let one
                # connection's crash stop the rest of the night's sync.
                log.exception("[CRON] CRASH %s - %s", conn.client_email, conn.platform)
                result = {"status": "error", "error": "unexpected exception (see traceback above)"}
            self.stdout.write(f"{conn}: {result}")
            key = "%s|%s" % (conn.client_email, conn.platform)
            if result["status"] == "error":
                fail_counts[key] = fail_counts.get(key, 0) + 1
                if fail_counts[key] >= REPEATED_FAILURE_THRESHOLD:
                    repeated.append("%s (%s runs in a row)" % (key, fail_counts[key]))
                failures += 1
                failed_list.append(f"{conn.client_email} {conn.platform}")
                err = str(result.get("error", ""))
                # Per-connection line so sync_cron.log shows WHY each store failed.
                if "reconnect" in err.lower():
                    log.warning("[CRON] NEEDS_RECONNECT %s %s: %s", conn.client_email, conn.platform, err[:300])
                else:
                    log.error("[CRON] FAILED %s %s: %s", conn.client_email, conn.platform, err[:500])
            else:
                ok += 1
                fail_counts.pop(key, None)
                log.info("[CRON] OK %s %s", conn.client_email, conn.platform)

        _save_fail_counts(counts_path, fail_counts)
        if repeated:
            log.error("[CRON] REPEATED_FAILURE - still failing, needs attention: %s", "; ".join(repeated))

        total = ok + failures
        elapsed = round(time.time() - started, 1)
        if failures:
            log.error("[CRON] FINISHED %s/%s ok, %s failed in %ss -> %s", ok, total, failures,
                      elapsed, "; ".join(failed_list))
            raise CommandError(f"{failures} connection(s) failed - see logs/sync.log")
        log.info("[CRON] FINISHED %s/%s ok in %ss", ok, total, elapsed)