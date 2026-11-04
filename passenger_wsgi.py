import os
import sys
import subprocess
import traceback

def install_requirements():
    req_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'requirements.txt')
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-r', req_path])

try:
    try:
        import pymysql
    except ImportError:
        install_requirements()
        import pymysql

    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'crowdlink_core.settings')
    django.setup()

    from django.core.management import call_command

    # NOTE: the old startup code ran "DELETE FROM core_api_salessummary" here
    # (with FOREIGN_KEY_CHECKS=0) as a one-off workaround for migration 0018.
    # That wiped every user's audience size / notes / assigned location on each
    # restart, so it has been removed.
    call_command('migrate', interactive=False)
    call_command('collectstatic', interactive=False)

    from crowdlink_core.wsgi import application

except Exception:
    error_text = traceback.format_exc()
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wsgi_error.log')
    import datetime
    with open(log_path, 'a') as f:   # append: older startup errors are kept
        f.write("\n===== %s =====\n%s" % (datetime.datetime.now().isoformat(), error_text))

    def application(environ, start_response):
        start_response('500 Internal Server Error', [('Content-Type', 'text/plain')])
        return [error_text.encode('utf-8')]