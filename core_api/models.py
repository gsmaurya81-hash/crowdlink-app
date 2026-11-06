import uuid
from django.db import models

class ClientStoreConnection(models.Model):
    PLATFORM_CHOICES = [
        ('SHOPIFY', 'Shopify'),
        ('SQUARE', 'Square'),
    ]

    REPORT_FREQUENCY_CHOICES = [
        ('NEVER', 'Never'),
        ('WEEKLY', 'Weekly'),
        ('MONTHLY', 'Monthly'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client_email = models.EmailField()
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    store_identifier = models.CharField(max_length=255)
    access_token = models.TextField()
    refresh_token = models.TextField(null=True, blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    report_frequency = models.CharField(max_length=10, choices=REPORT_FREQUENCY_CHOICES, default='WEEKLY')
    show_price_on_charts = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.client_email} - {self.platform}"

# Stage 2 SOW: CrowdLink product category. Exactly these choices, no others.
# The "-----------" separator in the client's spec is a UI-only divider
# between merch and music categories; it is NOT a selectable value here.
CATEGORY_MERCH_CHOICES = [
    ('Tee', 'Tee'),
    ('Hoodie', 'Hoodie'),
    ('Other Garment', 'Other Garment'),
    ('Headwear', 'Headwear'),
    ('Drinkware', 'Drinkware'),
    ('Other Merch', 'Other Merch'),
    ('Define Bundle', 'Define Bundle'),
]
CATEGORY_MUSIC_CHOICES = [
    ('Vinyl Album 1LP', 'Vinyl Album 1LP'),
    ('Vinyl Album 2LP', 'Vinyl Album 2LP'),
    ('Vinyl Single', 'Vinyl Single'),
    ('CD Album', 'CD Album'),
    ('CD Single', 'CD Single'),
    ('Cassette Album', 'Cassette Album'),
    ('Cassette Single', 'Cassette Single'),
    ('Digital Album', 'Digital Album'),
    ('Digital Single', 'Digital Single'),
    ('Other Album', 'Other Album'),
    ('Other Single', 'Other Single'),
]
CATEGORY_CHOICES = CATEGORY_MERCH_CHOICES + CATEGORY_MUSIC_CHOICES


def is_category_aria_reportable(category):
    """Stage 2 SOW rule: a category is ARIA-reportable if its label contains
    'Album' or 'Single' (case-insensitive). Merch categories never qualify."""
    if not category:
        return False
    lowered = category.lower()
    return 'album' in lowered or 'single' in lowered


class Product(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(ClientStoreConnection, on_delete=models.CASCADE)
    
    # Parent Shopify Product GID / Square parent Item ID (1 row per parent product)
    external_product_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    
    # All real variant SKUs/barcodes combined (space separated) for display on
    # the Products tab. Derived after every sync from the Variant rows with
    # placeholders (MISSING-...) excluded - see refresh_product_identifiers()
    # in core_api/utils/post_sync.py. Never edit by hand.
    combined_sku_bar = models.TextField(null=True, blank=True)
    has_missing_identifier = models.BooleanField(default=False)
    
    title = models.CharField(max_length=255)
    
    # Products can have pre-sales before this date
    release_date = models.DateField(null=True, blank=True)
    
    # Presale tracking logic
    webstore_creation_date = models.DateTimeField(null=True, blank=True)
    all_presales_loaded = models.BooleanField(default=False)
    
    # SOW Requirement: Client can opt out if already reporting elsewhere
    report_to_aria = models.BooleanField(default=True)

    # Stage 2 SOW Requirement: CrowdLink-owned product category.
    # NEVER set/overwritten by Shopify/Square sync — see core_api/utils/product_sync.py.
    category = models.CharField(max_length=32, choices=CATEGORY_CHOICES, null=True, blank=True)
    
    # Stage 3 SOW: Bundle definition storing selected sub-products and their unit prices.
    json_bundle = models.JSONField(null=True, blank=True)
    
    image_url = models.URLField(max_length=500, null=True, blank=True)
    
    channel = models.CharField(max_length=50, null=True, blank=True, default='POS')

    # Ex-tax selling price. Lowest price among the product's variants (equal to the
    # variant price for single-variant products). NULL = platform gave no usable price.
    # Platform-owned: refreshed by every sync (see core_api/utils/product_sync.py).
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        # Prevent duplicate parent product imports per store connection
        unique_together = ('connection', 'external_product_id')
        
    def __str__(self):
        return f"{self.title} ({self.external_product_id})"


class Variant(models.Model):
    """
    Child variation row belonging to a parent Product.
    Holds the variant-level SKU/barcode and platform catalog ID.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(ClientStoreConnection, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants')

    sku_or_barcode = models.CharField(max_length=100, null=True, blank=True)
    external_catalog_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    external_product_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    has_missing_identifier = models.BooleanField(default=False)

    title = models.CharField(max_length=255, null=True, blank=True)
    image_url = models.URLField(max_length=500, null=True, blank=True)
    # Ex-tax selling price of this variant (see Product.price). NULL = not available.
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # A variant is identified by its PLATFORM id, never by its SKU/barcode: clients
        # can enter the same barcode on two products (bundles, re-listed products) and
        # each product must keep its own variant and its own sales. sku_or_barcode is a
        # plain (non-unique) lookup column.
        indexes = [
            models.Index(fields=['connection', 'sku_or_barcode'], name='variant_conn_sku_idx'),
        ]
        constraints = [
            # One row per platform variant.
            models.UniqueConstraint(fields=['connection', 'external_catalog_id'],
                                    name='unique_variant_per_catalog_id'),
        ]

    def __str__(self):
        return f"{self.product.title} - {self.sku_or_barcode}"


class Sale(models.Model):
    """
    Order Header Table (1 row per order; external_order_id has no duplicates).
    SOW CONSTRAINT: This model must NEVER store customer/buyer personal data 
    (name, address, contact info) or financial account/payment-card details.
    """
    PAYMENT_TYPE_CHOICES = [
        ('CREDIT_CARD', 'Credit Card'),
        ('MANUAL_CARD_ENTRY', 'Manual Card Entry'),
        ('CARD_ON_FILE', 'Card on File'),
        ('PAYMENT_LINK', 'Payment Link'),
        ('GIFT_CARD_REDEMPTION', 'Platform Gift Card'),
        ('OTHER_GIFT_CARD_CERTIFICATE', '3rd Party Gift Card/Certificate'),
        ('CASH', 'Cash'),
        ('CHEQUE', 'Cheque'),
        ('HOUSE_ACCOUNT_CHARGE', 'House Account Charge'),
        ('OTHER_USER_DEFINED', 'Other/User Defined'),
    ]

    SALE_CHANNEL_CHOICES = [
        ('POS', 'POS'),
        ('WEBSTORE', 'Webstore'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(ClientStoreConnection, on_delete=models.CASCADE)
    
    # SOW Requirement: Unique order ID with no duplicates in Sale header table
    external_order_id = models.CharField(max_length=255, unique=True)
    
    sale_date = models.DateTimeField(db_index=True)
    
    # Location tracking
    system_location = models.CharField(max_length=255)
    actual_location = models.CharField(max_length=255, null=True, blank=True)
    
    sale_channel = models.CharField(max_length=20, choices=SALE_CHANNEL_CHOICES, default='POS')
    is_presale = models.BooleanField(default=False)
    payment_type = models.CharField(max_length=30, choices=PAYMENT_TYPE_CHOICES)
    
    # Formatted sale type: e.g., Shopify-POS-Card, Square-Webstore-Other
    type = models.CharField(max_length=100, null=True, blank=True)

    # SSID = <numeric_date>-<type> (e.g. 739887-Shopify-POS-Card). Links a Sale to
    # its SalesSummary line; refreshed after every sync (core_api/utils/post_sync.py).
    ssid = models.CharField(max_length=150, null=True, blank=True, db_index=True)
    
    notes = models.TextField(null=True, blank=True)
    audience_size = models.IntegerField(null=True, blank=True)
    
    source_platform = models.CharField(max_length=50) # e.g. Shopify, Square
    
    is_within_current_aria_period = models.BooleanField(default=True)
    
    # Official Reporting Dates
    aria_reporting_date = models.DateField(null=True, blank=True)
    clau_reporting_date = models.DateField(null=True, blank=True)
    reported_flag = models.BooleanField(default=False, db_index=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
        
    def __str__(self):
        return f"Sale {self.external_order_id}"


class SaleDetail(models.Model):
    """
    Order Line-Item Table (links to Sale header and Variant).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(ClientStoreConnection, on_delete=models.CASCADE)
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name='details')
    variant = models.ForeignKey(Variant, on_delete=models.CASCADE, related_name='sale_details')

    external_order_id = models.CharField(max_length=255, db_index=True, null=True, blank=True)

    sales_units = models.IntegerField(default=1)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    # Official Reporting Dates (per line item)
    aria_reporting_date = models.DateField(null=True, blank=True)
    clau_reporting_date = models.DateField(null=True, blank=True)

    # Denormalized snapshots for reporting
    product_title = models.CharField(max_length=255, null=True, blank=True)
    product_category = models.CharField(max_length=32, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('sale', 'variant')

    def __str__(self):
        return f"SaleDetail {self.external_order_id}"


class SalesLocation(models.Model):
    """
    Client SOW Requirement: Dedicated Sales Location table in the external SQL database.
    Extended for Stage 2 to support custom CrowdLink locations, Webstore defaults, 
    and Geo-coordinates.
    """
    PLATFORM_CHOICES = [
        ('SHOPIFY', 'Shopify'),
        ('SQUARE', 'Square'),
        ('CROWDLINK', 'CrowdLink'),
        ('WEBSTORE', 'Webstore'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Connection made optional because CrowdLink custom locations aren't tied to a specific store sync
    connection = models.ForeignKey(ClientStoreConnection, on_delete=models.CASCADE, null=True, blank=True)
    
    # Used to tie CrowdLink-created locations directly to the user's account
    client_email = models.EmailField(db_index=True, null=True, blank=True)
    
    # The ID from Shopify/Square (so we can update it later without creating duplicates)
    external_location_id = models.CharField(max_length=255, null=True, blank=True)
    platform = models.CharField(max_length=30, choices=PLATFORM_CHOICES, default='SHOPIFY')
    
    # Official CLAU Locality ID (Used for ARIA reporting mapping)
    locid = models.IntegerField(null=True, blank=True)
    
    # Details about the location
    name = models.CharField(max_length=255)
    formatted_address = models.CharField(max_length=500, null=True, blank=True)
    address1 = models.CharField(max_length=255, null=True, blank=True)
    city = models.CharField(max_length=100, null=True, blank=True)
    province = models.CharField(max_length=100, null=True, blank=True)
    postal_code = models.CharField(max_length=20, null=True, blank=True)
    country = models.CharField(max_length=100, null=True, blank=True, default='AU')
    
    # Coordinates for Insights mapping
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Prevents creating the exact same synced location twice for the same store
        constraints = [
            models.UniqueConstraint(
                fields=['connection', 'external_location_id'], 
                name='unique_store_location'
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.platform})"


class SalesSummary(models.Model):
    """
    Stage 3 SOW: Sales tab groups a client's individual Sale rows into one
    line per (connection, calendar date, type) - e.g. "Shopify-POS-Card".
    This model holds the fields the client assigns at that GROUP level
    (location, audience estimate, notes), rather than per individual sale.

    Rows are created on-demand (get_or_create) whenever the Sales API groups a
    day's transactions for the first time - see SalesView.get() in views.py.

    Albums / Merch counts and the auto-detected location name are now ALSO
    stored here (denormalised) so reporting/Insights queries don't need to
    re-aggregate the underlying Sale rows every time. They are refreshed
    whenever SalesView.get() runs for that (connection, date, type).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(ClientStoreConnection, on_delete=models.CASCADE)

    # Formatted SSID: <numeric_date>-<sales.type> (e.g., 739887-Shopify-POS-Card)
    ssid = models.CharField(max_length=150, null=True, blank=True)

    summary_date = models.DateField(db_index=True)
    type = models.CharField(max_length=100, null=True, blank=True)

    # Client-assigned event location for this day's POS sales. Left null for
    # Webstore rows (those are always "Webstore Sales" - see Locations tab
    # convention) and defaults to "Unknown" in the UI until the client picks one.
    assigned_location = models.ForeignKey(
        SalesLocation, on_delete=models.SET_NULL, null=True, blank=True
    )

    audience_size = models.IntegerField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    # ✅ NEW FIELDS — auto-populated from underlying Sale rows on every
    # SalesView.get() call. Kept in sync with the live calculation so they
    # can be used directly for reporting without re-aggregating Sale rows.
    albums_units = models.IntegerField(default=0)
    merch_units = models.IntegerField(default=0)
    
    # Official CLAU link. Name can change, locid cannot.
    locid = models.IntegerField(null=True, blank=True)
    location = models.CharField(max_length=255, default="Unknown")  # Denormalized name cache

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['connection', 'summary_date', 'type'],
                name='unique_sales_summary_per_day_type'
            )
        ]

    def __str__(self):
        return f"{self.summary_date} - {self.type} ({self.connection.client_email})"


class StandardLocality(models.Model):
    # Nullable fields allow the migration to pass without terminal prompts
    locid = models.IntegerField(unique=True, null=True, blank=True)
    locality = models.CharField(max_length=255, null=True, blank=True)
    suburb = models.CharField(max_length=255, null=True, blank=True)
    postcode = models.CharField(max_length=20, null=True, blank=True)
    state = models.CharField(max_length=10, null=True, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    
    # Matching exact CSV demographic columns
    location_class = models.CharField(max_length=255, null=True, blank=True)
    region = models.CharField(max_length=255, null=True, blank=True)
    lga = models.CharField(max_length=255, null=True, blank=True)
    federal_electorate = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['suburb', 'postcode']),
        ]

    def __str__(self):
        return self.locality or f"Locality {self.locid}"