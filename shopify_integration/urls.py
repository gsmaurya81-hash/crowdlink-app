from django.urls import path
from .views import (
    ShopifyConnectView, 
    ShopifyCallbackView, 
    ShopifyOrderWebhookView, 
    ShopifyProductWebhookView,
    ProductsView,
    disconnect_shopify,
    manual_resync_shopify,
    ShopifyCustomerDataRequestView,
    ShopifyCustomerRedactView,
    ShopifyShopRedactView,
)

# NOTE: every Shopify-delivered webhook below is registered TWICE — with
# and without a trailing slash. Shopify strips the trailing slash on
# actual delivery even when the subscription URI (in shopify.app.toml)
# includes one, and Django's APPEND_SLASH can't redirect a POST without
# losing the body, so it raises instead of redirecting (this is exactly
# what caused the shop/redact 500/RuntimeError incident). Non-webhook,
# browser-initiated routes (connect/, callback/, etc.) don't need this —
# only endpoints Shopify's servers POST to directly.

urlpatterns = [
    path('connect/', ShopifyConnectView.as_view(), name='shopify_connect'),
    path('callback/', ShopifyCallbackView.as_view(), name='shopify_callback'),

    path('webhooks/orders/', ShopifyOrderWebhookView.as_view(), name='shopify_webhook_orders'),
    path('webhooks/orders', ShopifyOrderWebhookView.as_view(), name='shopify_webhook_orders_noslash'),

    path('webhooks/products/create/', ShopifyProductWebhookView.as_view(), name='shopify_webhook_products_create'),
    path('webhooks/products/create', ShopifyProductWebhookView.as_view(), name='shopify_webhook_products_create_noslash'),
    path('webhooks/products/update/', ShopifyProductWebhookView.as_view(), name='shopify_webhook_products_update'),
    path('webhooks/products/update', ShopifyProductWebhookView.as_view(), name='shopify_webhook_products_update_noslash'),
    path('webhooks/products/delete/', ShopifyProductWebhookView.as_view(), name='shopify_webhook_products_delete'),
    path('webhooks/products/delete', ShopifyProductWebhookView.as_view(), name='shopify_webhook_products_delete_noslash'),

    path('webhooks/customers/data_request/', ShopifyCustomerDataRequestView.as_view(), name='shopify_webhook_customer_data_request'),
    path('webhooks/customers/data_request', ShopifyCustomerDataRequestView.as_view(), name='shopify_webhook_customer_data_request_noslash'),
    path('webhooks/customers/redact/', ShopifyCustomerRedactView.as_view(), name='shopify_webhook_customer_redact'),
    path('webhooks/customers/redact', ShopifyCustomerRedactView.as_view(), name='shopify_webhook_customer_redact_noslash'),
    path('webhooks/shop/redact/', ShopifyShopRedactView.as_view(), name='shopify_webhook_shop_redact'),
    path('webhooks/shop/redact', ShopifyShopRedactView.as_view(), name='shopify_webhook_shop_redact_noslash'),

    path('synced-products/', ProductsView.as_view(), name='synced_products'),
    path('disconnect/', disconnect_shopify, name='disconnect_shopify'),
    path('debug/resync/', manual_resync_shopify, name='shopify_debug_resync'),
]