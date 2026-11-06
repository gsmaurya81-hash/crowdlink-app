from django.urls import path
from .views import (
    ConnectionSettingsView,
    ProductReleaseDateView,
    StoreConnectionTextView,
    ProductsHelpTextView,
    CategoryOptionsView,
    BundleProductsView,
    SalesView,
    SalesSummaryDetailView,
    SalesHelpTextView,
    ManualSyncView,
    LocationsHelpTextView,
    LocationsListView,
    LocationDetailView,
    LocationAutocompleteView, 
    import_localities_csv,
)
from core_api.debug_views import find_duplicate_products
urlpatterns = [
    path('account/connections/', ConnectionSettingsView.as_view(), name='connection_settings'),
    # NOTE: URL/name kept as "release-date" for backward compatibility even
    # though the view now also handles category + report_to_aria (Stage 2).
    path('account/products/<uuid:product_id>/release-date/', ProductReleaseDateView.as_view(), name='product_release_date'),
    path('account/sync/manual/', ManualSyncView.as_view(), name='manual_sync'),
    path('shop/store-connection-text/', StoreConnectionTextView.as_view(), name='store_connection_text'),
    path('products/help-text/', ProductsHelpTextView.as_view(), name='products_help_text'),
    path('products/category-options/', CategoryOptionsView.as_view(), name='products_category_options'),
    path('products/bundle-options/', BundleProductsView.as_view(), name='products_bundle_options'),
    path('sales/help-text/', SalesHelpTextView.as_view(), name='sales_help_text'),
    path('sales/', SalesView.as_view(), name='api_sales'),
    path('sales/summary/<uuid:summary_id>/', SalesSummaryDetailView.as_view(), name='sales_summary_detail'),
    path('debug/duplicate-products/', find_duplicate_products, name='find_duplicate_products'),
    # --- STAGE 2 LOCATION ROUTES ---
    path('locations/help-text/', LocationsHelpTextView.as_view(), name='locations_help_text'),
    path('locations/autocomplete/', LocationAutocompleteView.as_view(), name='location_autocomplete'),
    path('locations/', LocationsListView.as_view(), name='locations_list_create'),
    path('locations/<uuid:location_id>/', LocationDetailView.as_view(), name='location_detail_update_delete'),

    # 1-Click CSV Importer
    path('import-csv/', import_localities_csv, name='import_csv'),
]