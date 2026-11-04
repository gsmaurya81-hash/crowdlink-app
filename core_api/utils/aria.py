from django.utils import timezone
import datetime

def get_current_aria_week_start():
    """
    Standard ARIA reporting weeks run from Friday 00:00:00 to Thursday 23:59:59.
    This function calculates and returns a timezone-aware datetime for the most recent Friday at midnight.
    """
    now = timezone.now()
    days_since_friday = (now.weekday() - 4) % 7
    last_friday = now - datetime.timedelta(days=days_since_friday)
    return last_friday.replace(hour=0, minute=0, second=0, microsecond=0)
