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
  .card { background: #fff; max-width: 640px; padding: 32px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,.1); }
  h1 { color: #008060; font-size: 24px; margin-top: 0; }
  h2 { font-size: 17px; margin: 28px 0 6px; }
  p, li { line-height: 1.5; }
  li { margin: 6px 0; }
  a { color: #005bd3; }
  .btn { display: inline-block; background: #008060; color: #fff; padding: 10px 16px; border-radius: 6px; text-decoration: none; font-weight: 600; margin: 6px 0; }
  .note { background: #f1f8f5; border-left: 4px solid #008060; padding: 10px 14px; margin-top: 24px; }
</style>
</head>
<body>
<div class="card">
  <h1>CrowdLink Portal installed__SHOP__</h1>
  <p>CrowdLink does <b>not</b> open inside Shopify admin. It appears as a page inside your store's
     <b>customer account</b> (the page where customers log in to see their orders).
     CrowdLink is read-only: it never changes data in your store or POS.</p>

  <div class="note"><b>Important:</b> CrowdLink is a <b>customer account extension</b>, not a checkout extension.
     It does not appear on the checkout page, product pages or the storefront theme.
     You will find it only inside the customer account at <b>/account</b>, after you complete Steps 1 to 3 below.</div>

  <div class="note"><b>Quick summary:</b> turn on <b>New customer accounts</b>
     &rarr; add the <b>CrowdLink Portal</b> page in the customer accounts editor
     &rarr; add a <b>CrowdLink</b> link to the <b>Customer account main menu</b>
     &rarr; log in to your customer account &rarr; click <b>CrowdLink</b>.
     Steps 1 to 3 are done only once.</div>

  <h2>Step 1: Turn on New customer accounts</h2>
  <ol>
    <li>Click the button below. It opens <b>Settings &rarr; Customer accounts</b> in Shopify admin.</li>
    <li>Make sure <b>New customer accounts</b> is selected.</li>
  </ol>
  __BTN_SETTINGS__

  <h2>Step 2: Add the CrowdLink Portal page</h2>
  <ol>
    <li>On the same Customer accounts page, click <b>Customize</b>. The checkout and accounts editor opens.</li>
    <li>Open the <b>Apps</b> sidebar and add <b>CrowdLink Portal</b> as a page.</li>
    <li>Click <b>Save</b>. (The page does not load until it has been added here.)</li>
  </ol>

  <h2>Step 3: Add CrowdLink to the customer account menu</h2>
  <ol>
    <li>Click the button below. It opens <b>Content &rarr; Menus</b> in Shopify admin.</li>
    <li>Open <b>Customer account main menu</b>.</li>
    <li>Click <b>Add menu item</b>, enter the name <b>CrowdLink</b> and, in the Link field, choose <b>CrowdLink Portal</b>.</li>
    <li>Click <b>Add</b>, then <b>Save</b> the menu.</li>
  </ol>
  __BTN_MENUS__

  <h2>Step 4: Open CrowdLink from your customer account</h2>
  <ol>
    <li>Click the button below to open your store's customer account page.</li>
    <li>Log in with your email address (Shopify sends you a login code).</li>
    <li>Click <b>CrowdLink</b> in the account menu.</li>
  </ol>
  __BTN_ACCOUNT__

  <h2>Step 5: Use CrowdLink</h2>
  <p>Inside CrowdLink you will see these tabs:</p>
  <ol>
    <li><b>Connect:</b> read and accept the Terms, then connect your Shopify or Square store.</li>
    <li><b>Sync:</b> your store syncs automatically every day. Use <b>Run Manual Sync</b> to refresh it now.</li>
    <li><b>Products:</b> for each product choose a category, a release date and whether to report to ARIA.</li>
    <li><b>Locations:</b> check your store locations or add an event location.</li>
    <li><b>Sales:</b> assign each sales line to a location and add an audience estimate and notes.</li>
  </ol>

  <div class="note">Need help? <a href="mailto:sudhir.s@edreamz.in">sudhir.s@edreamz.in</a> &middot; <a href="/privacy/">Privacy policy</a></div>
</div>
</body>
</html>"""


def home(request):
    shop = request.GET.get("shop", "")
    html = WELCOME_HTML
    if shop and SHOP_RE.match(shop):
        handle = shop[: -len(".myshopify.com")]
        html = html.replace("__SHOP__", f" on <b>{escape(shop)}</b>")
        html = html.replace(
            "__BTN_SETTINGS__",
            f'<a class="btn" target="_blank" rel="noopener" '
            f'href="https://admin.shopify.com/store/{escape(handle)}/settings/customer_accounts">'
            f"Open Customer accounts settings</a>",
        )
        html = html.replace(
            "__BTN_MENUS__",
            f'<a class="btn" target="_blank" rel="noopener" '
            f'href="https://admin.shopify.com/store/{escape(handle)}/menus">'
            f"Open Menus</a>",
        )
        html = html.replace(
            "__BTN_ACCOUNT__",
            f'<a class="btn" target="_blank" rel="noopener" '
            f'href="https://{escape(shop)}/account">Open my customer account</a>',
        )
    else:
        html = html.replace("__SHOP__", "")
        html = html.replace("__BTN_SETTINGS__", "")
        html = html.replace("__BTN_MENUS__", "")
        html = html.replace("__BTN_ACCOUNT__", "")
    return HttpResponse(html, content_type="text/html")


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