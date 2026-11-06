import base64
import hashlib
import hmac
import json

from django.test import TestCase, override_settings
from django.urls import reverse

from core_api.models import ClientStoreConnection

SHOPIFY_SECRET = "shpss_test_secret"
SQUARE_KEY = "square_test_key"


def make_conn(platform, store, email="a@example.com"):
    return ClientStoreConnection.objects.create(
        client_email=email, platform=platform, store_identifier=store,
        access_token="tok", is_active=True)


@override_settings(SHOPIFY_CLIENT_SECRET=SHOPIFY_SECRET, SQUARE_WEBHOOK_SIGNATURE_KEY=SQUARE_KEY)
class UninstallWebhooks(TestCase):
    def _shopify(self, shop, body=b"{}", secret=SHOPIFY_SECRET, path=None):
        sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
        return self.client.post(path or reverse("shopify_webhook_app_uninstalled"), data=body,
                                content_type="application/json",
                                HTTP_X_SHOPIFY_HMAC_SHA256=sig, HTTP_X_SHOPIFY_SHOP_DOMAIN=shop)

    def _square(self, payload, key=SQUARE_KEY):
        body = json.dumps(payload)
        url = "https://testserver" + reverse("square_webhook_orders")
        sig = base64.b64encode(hmac.new(key.encode(), (url + body).encode(), hashlib.sha256).digest()).decode()
        return self.client.post(reverse("square_webhook_orders"), data=body,
                                content_type="application/json",
                                HTTP_X_SQUARE_HMACSHA256_SIGNATURE=sig)

    def test_shopify_uninstall_deactivates_only_that_store(self):
        gone = make_conn("SHOPIFY", "gone.myshopify.com")
        other = make_conn("SHOPIFY", "other.myshopify.com")
        self.assertEqual(self._shopify("gone.myshopify.com").status_code, 200)
        gone.refresh_from_db(); other.refresh_from_db()
        self.assertFalse(gone.is_active)
        self.assertTrue(other.is_active)

    def test_shopify_uninstall_works_without_trailing_slash(self):
        c = make_conn("SHOPIFY", "gone.myshopify.com")
        path = reverse("shopify_webhook_app_uninstalled").rstrip("/")
        self.assertEqual(self._shopify("gone.myshopify.com", path=path).status_code, 200)
        c.refresh_from_db()
        self.assertFalse(c.is_active)

    def test_shopify_bad_signature_is_rejected_and_changes_nothing(self):
        c = make_conn("SHOPIFY", "gone.myshopify.com")
        self.assertEqual(self._shopify("gone.myshopify.com", secret="wrong").status_code, 401)
        c.refresh_from_db()
        self.assertTrue(c.is_active)

    def test_shopify_unknown_store_is_acknowledged(self):
        self.assertEqual(self._shopify("nobody.myshopify.com").status_code, 200)

    def test_square_revoked_deactivates_merchant(self):
        gone = make_conn("SQUARE", "MERCH1")
        other = make_conn("SQUARE", "MERCH2")
        r = self._square({"merchant_id": "MERCH1", "type": "oauth.authorization.revoked",
                          "data": {"type": "revocation", "object": {"revocation": {"revoker_type": "MERCHANT"}}}})
        self.assertEqual(r.status_code, 200)
        gone.refresh_from_db(); other.refresh_from_db()
        self.assertFalse(gone.is_active)
        self.assertTrue(other.is_active)

    def test_square_revoked_bad_signature_is_rejected(self):
        c = make_conn("SQUARE", "MERCH1")
        r = self._square({"merchant_id": "MERCH1", "type": "oauth.authorization.revoked"}, key="wrong")
        self.assertEqual(r.status_code, 401)
        c.refresh_from_db()
        self.assertTrue(c.is_active)

    def test_square_does_not_deactivate_shopify_with_same_identifier(self):
        s = make_conn("SHOPIFY", "MERCH1")
        self._square({"merchant_id": "MERCH1", "type": "oauth.authorization.revoked"})
        s.refresh_from_db()
        self.assertTrue(s.is_active)
