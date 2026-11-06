import os
import requests
from django.conf import settings
from datetime import timedelta
import logging
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger("square_integration")

# Square access tokens last 30 days. Renew once fewer than 7 days remain so a
# daily sync never runs on a token that is about to expire.
REFRESH_WINDOW = timedelta(days=7)


class SquareReconnectRequired(RuntimeError):
    """The token can no longer be renewed - the merchant must reconnect Square."""

def get_square_auth_url(state_token):
    client_id = getattr(settings, 'SQUARE_APPLICATION_ID', os.environ.get('SQUARE_APPLICATION_ID', ''))
    environment = getattr(settings, 'SQUARE_ENVIRONMENT', os.environ.get('SQUARE_ENVIRONMENT', 'sandbox')).lower()
    redirect_uri = getattr(settings, 'SQUARE_REDIRECT_URI', os.environ.get('SQUARE_REDIRECT_URI', 'http://localhost:8000/api/square/callback/'))
    
    if environment == 'production':
        base_url = "https://connect.squareup.com/oauth2/authorize"
        session_param = "false"
    else:
        base_url = "https://connect.squareupsandbox.com/oauth2/authorize"
        session_param = "true"
    
    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": "ITEMS_READ ORDERS_READ MERCHANT_PROFILE_READ",
        "state": state_token,
        "redirect_uri": redirect_uri,
        "session": session_param
    }
    
    req = requests.Request('GET', base_url, params=params).prepare()
    return req.url

def exchange_code_for_token(auth_code):
    client_id = getattr(settings, 'SQUARE_APPLICATION_ID', os.environ.get('SQUARE_APPLICATION_ID', ''))
    client_secret = getattr(settings, 'SQUARE_APPLICATION_SECRET', os.environ.get('SQUARE_APPLICATION_SECRET', ''))
    environment = getattr(settings, 'SQUARE_ENVIRONMENT', os.environ.get('SQUARE_ENVIRONMENT', 'sandbox')).lower()
    redirect_uri = getattr(settings, 'SQUARE_REDIRECT_URI', os.environ.get('SQUARE_REDIRECT_URI', 'http://localhost:8000/api/square/callback/'))
    
    if not client_id or not client_secret:
        raise RuntimeError("SQUARE_APPLICATION_ID / SQUARE_APPLICATION_SECRET are not configured")

    if environment == 'production':
        url = "https://connect.squareup.com/oauth2/token"
    else:
        url = "https://connect.squareupsandbox.com/oauth2/token"
        
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": auth_code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri
    }
    
    response = requests.post(url, json=data, timeout=15)
    response.raise_for_status()
    return response.json()

def refresh_access_token(connection):
    client_id = getattr(settings, 'SQUARE_APPLICATION_ID', os.environ.get('SQUARE_APPLICATION_ID', ''))
    client_secret = getattr(settings, 'SQUARE_APPLICATION_SECRET', os.environ.get('SQUARE_APPLICATION_SECRET', ''))
    environment = getattr(settings, 'SQUARE_ENVIRONMENT', os.environ.get('SQUARE_ENVIRONMENT', 'sandbox')).lower()
    
    if not client_id or not client_secret or not connection.refresh_token:
        raise Exception("Missing credentials or refresh token")
        
    if environment == 'production':
        url = "https://connect.squareup.com/oauth2/token"
    else:
        url = "https://connect.squareupsandbox.com/oauth2/token"
        
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": connection.refresh_token,
        "grant_type": "refresh_token"
    }
    
    response = requests.post(url, json=data, timeout=15)
    response.raise_for_status()
    token_data = response.json()
    
    connection.access_token = token_data.get("access_token", connection.access_token)
    connection.refresh_token = token_data.get("refresh_token", connection.refresh_token)
    
    expires_at_str = token_data.get("expires_at")
    if expires_at_str:
        connection.token_expires_at = parse_datetime(expires_at_str)
        
    connection.save(update_fields=["access_token", "refresh_token", "token_expires_at"])
    return connection


def get_valid_access_token(connection, force=False):
    """Return a usable Square access token, renewing it first when it is within
    REFRESH_WINDOW of expiry (or when force=True, e.g. after a 401).
    Raises SquareReconnectRequired when it cannot be renewed."""
    expires = connection.token_expires_at
    now = timezone.now()
    due = force or (expires is not None and now >= expires - REFRESH_WINDOW)
    if not due:
        return connection.access_token

    still_valid = (not force) and (expires is None or now < expires)
    try:
        refresh_access_token(connection)
    except Exception as exc:
        if still_valid:
            # Renewal failed but the current token still works - keep going and
            # try again on the next sync.
            logger.warning(f"[SYNC] Square token renewal failed for {connection}: {exc}")
            return connection.access_token
        raise SquareReconnectRequired(
            "Square access has expired and could not be renewed - the store must "
            f"reconnect Square ({exc})")
    return connection.access_token