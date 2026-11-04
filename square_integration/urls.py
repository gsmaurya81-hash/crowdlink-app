from django.urls import path
from .views import SquareConnectView, SquareCallbackView, SquareOrderWebhookView, SquareProductsView, disconnect_square, manual_resync_square

urlpatterns = [
    path('connect/', SquareConnectView.as_view(), name='square_connect'),
    path('callback/', SquareCallbackView.as_view(), name='square_callback'),
    path('webhooks/orders/', SquareOrderWebhookView.as_view(), name='square_webhook_orders'),
    path('synced-products/', SquareProductsView.as_view(), name='square_synced_products'),
    path('disconnect/', disconnect_square, name='square_disconnect'),
    path('debug/resync/', manual_resync_square, name='square_debug_resync'),
]