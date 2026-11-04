import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'crowdlink_core.settings')
django.setup()
from django.core.management import call_command
call_command('migrate', interactive=False)
call_command('collectstatic', interactive=False)