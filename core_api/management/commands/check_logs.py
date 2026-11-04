"""
Run this ONCE on the server (same command line your cron uses) to prove logging works:

    <venv>/bin/python manage.py check_logs

It prints which database this process really uses, writes a test line to every log
file, and lists the connections it can see. If `engine` is sqlite3 here, cron is
looking at the wrong database (DJANGO_DB_* variables are missing for cron).
"""
import logging
import os

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from core_api.models import ClientStoreConnection


class Command(BaseCommand):
    help = "Verify log files, database engine and visible store connections."

    def handle(self, *args, **opts):
        db = connection.settings_dict
        engine = db.get("ENGINE", "?").rsplit(".", 1)[-1]
        self.stdout.write("engine      : %s" % engine)
        self.stdout.write("database    : %s" % db.get("NAME"))
        self.stdout.write("DJANGO_DB_HOST set: %s" % bool(os.environ.get("DJANGO_DB_HOST")))
        self.stdout.write("log folder  : %s" % settings.LOG_DIR)

        for name in ("crowdlink.cron", "crowdlink.audit", "shopify_integration"):
            logging.getLogger(name).info("[CHECK_LOGS] test line from %s (engine=%s)", name, engine)
        for fn in ("sync.log", "sync_cron.log", "app.log"):
            path = settings.LOG_DIR / fn
            self.stdout.write("  %-14s %s" % (fn, "OK (%s bytes)" % path.stat().st_size if path.exists() else "MISSING"))

        try:
            conns = list(ClientStoreConnection.objects.filter(is_active=True))
            self.stdout.write("active connections visible: %s" % len(conns))
            for c in conns:
                self.stdout.write("  - %s %s last_sync_at=%s" % (c.client_email, c.platform, c.last_sync_at))
        except Exception as exc:
            self.stdout.write(self.style.ERROR("cannot read connections: %s" % exc))
            return

        if engine == "sqlite3" and not os.environ.get("DJANGO_DB_HOST"):
            self.stdout.write(self.style.ERROR(
                "WRONG DATABASE: running on SQLite. Add DJANGO_DB_HOST / _NAME / _USER / _PASSWORD "
                "to .env so cron uses the same MySQL as the web app."))
        else:
            self.stdout.write(self.style.SUCCESS("Database looks right."))
