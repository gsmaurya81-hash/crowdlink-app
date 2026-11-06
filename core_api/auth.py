"""
core_api/auth.py

Access control for maintenance / debug routes (security review C3).
These routes (CSV import, duplicate-product report, debug re-sync) are for
CrowdLink staff only, so they require a Django admin login with is_superuser.
Python 3.6 compatible.
"""
from functools import wraps

from django.http import JsonResponse


def require_superuser(view_func):
    """Allow the request only for a logged-in Django superuser (log in at /admin/)."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated or not user.is_superuser:
            return JsonResponse(
                {"error": "Forbidden - sign in at /admin/ as a superuser first"}, status=403)
        return view_func(request, *args, **kwargs)
    return wrapper