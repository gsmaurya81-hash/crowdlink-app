"""
URL configuration for crowdlink_core project.
"""
import re
from html import escape

from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse, HttpResponse

SHOP_RE = re.compile(r'^[a-z0-9][a-z0-9\-]*\.myshopify\.com$')

WELCOME_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CrowdLink Portal installed</title>
<style>
  body { font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; background: #f4f6f8; margin: 0; display: flex; justify-content: center; padding: 48px 16px; color: #202223; }
  .card { background: #fff; max-width: 560px; padding: 32px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,.1); }
  h1 { color: #008060; font-size: 24px; margin-top: 0; }
  li { margin: 8px 0; line-height: 1.5; }
  a { color: #005bd3; }
</style>
</head>
<body>
<div class="card">
  <h1>CrowdLink Portal installed</h1>
  <p>Thanks for installing CrowdLink__SHOP__. Finish setup in 3 steps:</p>
  <ol>
    <li>In Shopify admin go to <b>Settings &rarr; Customer accounts</b> and open the customer accounts editor.</li>
    <li>Make sure the <b>CrowdLink</b> page is added to the customer account menu, then publish.</li>
    <li>Log in to the customer account, open the <b>link</b> tab and connect your Shopify or Square store.</li>
  </ol>
  <p>CrowdLink is read-only: it never changes data in your store or POS.</p>
  <p>Need help? <a href="mailto:sudhir.s@edreamz.in">sudhir.s@edreamz.in</a> &middot; <a href="/privacy/">Privacy policy</a></p>
</div>
</body>
</html>"""

def home(request):
    shop = request.GET.get("shop", "")
    shop_txt = f" on <b>{escape(shop)}</b>" if shop and SHOP_RE.match(shop) else ""
    html = WELCOME_HTML.replace("__SHOP__", shop_txt)
    return HttpResponse(html, content_type="text/html")
    return JsonResponse({"status": "CrowdLink API is running"})


def health(request):
    return JsonResponse({"status": "CrowdLink API is running"})


def privacy_policy(request):
    return HttpResponse(PRIVACY_POLICY_HTML, content_type="text/html")

PRIVACY_POLICY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Privacy Policy — CrowdLink Portal</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body { font-family: -apple-system, Arial, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 20px; line-height: 1.6; color: #1a1a1a; }
  h1 { font-size: 28px; }
  h2 { font-size: 20px; margin-top: 32px; }
  p, li { font-size: 15px; }
  .updated { color: #666; font-size: 13px; margin-bottom: 32px; }
</style>
</head>
<body>
<h1>Privacy Policy — CrowdLink Portal</h1>
<p class="updated">Last updated: September 2026</p>

<p>This Privacy Policy explains how CrowdLink Portal ("the App", "we", "us") collects, uses, and shares information when a merchant connects their Shopify or Square account.</p>

<h2>What we collect</h2>
<ul>
  <li><strong>Store connection details:</strong> the email address of the person connecting the store, the store's domain, and OAuth access/refresh tokens for Shopify or Square, so we can sync data on the merchant's behalf.</li>
  <li><strong>Product catalog data:</strong> product titles, SKUs/barcodes, and identifiers from the connected Shopify or Square catalog.</li>
  <li><strong>Sales data:</strong> order and sale records (e.g. payment type, sale channel, timestamps) needed to report verified sales to ARIA.</li>
</ul>

<h2>What we do NOT collect</h2>
<p>We do not collect or store payment card numbers, billing details, or other sensitive payment information. Payment processing is handled entirely by Shopify or Square directly.</p>

<h2>How we use this information</h2>
<p>We use the information above solely to: (1) authenticate the merchant's connected store, (2) sync product and sales data, and (3) report verified sales to ARIA on the merchant's behalf. We do not sell or rent this information to third parties.</p>

<h2>Who we share data with</h2>
<ul>
  <li><strong>ARIA</strong> — for the purpose of verified sales reporting, as configured by the merchant.</li>
  <li><strong>Shopify and Square</strong> — solely to make the API calls necessary for this app to function.</li>
</ul>

<h2>Data retention and deletion</h2>
<p>We retain connection and sales data for as long as the store remains connected. A merchant can disconnect their Shopify or Square account at any time from within the app, which removes the stored connection and associated synced product data. We also comply with Shopify's mandatory compliance webhooks (customer data request, customer redact, and shop redact) to handle data deletion requests as required by Shopify's Partner Program Agreement.</p>

<h2>Security</h2>
<p>Access tokens are stored securely and are never exposed to the browser or to any party other than the systems described above.</p>

<h2>Contact us</h2>
<p>For questions about this policy or to request data deletion, contact us at: <strong>sudhir.s@edreamz.in</strong></p>
</body>
</html>"""

urlpatterns = [
    path('', home),
    path('health/', health),
    path('privacy/', privacy_policy),
    path('admin/', admin.site.urls),
    path('api/', include('core_api.urls')),
    path('api/square/', include('square_integration.urls')),
    path('api/shopify/', include('shopify_integration.urls')),
]