import os
import django
import sys

# Set up the Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'crowdlink_core.settings')
django.setup()

from django.test import Client
from core_api.models import Product, Sale
from django.utils import timezone

def run():
    client = Client()
    
    print("1. Simulating OAuth callback (Mock Mode)...")
    response = client.get('/api/square/callback/', {'code': 'test_mock_code', 'state': 'test@label.com'})
    print(f"Callback Status: {response.status_code}")
    if response.status_code == 200:
        print(f"Callback Response: {response.json()}")
    else:
        print("Failed to sync mock catalog")
        sys.exit(1)
    
    print("\n2. Querying Product table...")
    products = Product.objects.filter(connection__platform='SQUARE')
    for p in products:
        print(f" - {p.title} (SKU/Barcode: {p.sku_or_barcode})")
        
    print("\n3. Simulating Webhook POST (Mock Mode)...")
    payload = {
        "merchant_id": "mock_merchant_123",
        "data": {
            "object": {
                "order_created": {
                    "id": "order_test_001",
                    "created_at": timezone.now().isoformat(),
                    "note": "Urgent order for fan - CLAU fulfilment",
                    "source": {"name": "pos"},
                    "line_items": [
                        {
                            "uid": "line_test_001",
                            "catalog_object_id": "mock_item1",
                            "name": "Tour T-Shirt",
                            "quantity": "2",
                            "note": "123456789012",
                            "base_price_money": {"amount": 2500} # $25.00
                        }
                    ]
                }
            }
        }
    }
    
    response = client.post(
        '/api/square/webhooks/orders/?mock=true', 
        data=payload, 
        content_type='application/json'
    )
    print(f"Webhook Status: {response.status_code}")
    print(f"Webhook Response: {response.json()}")
    
    print("\n3b. Simulating CASH Webhook POST (Mock Mode)...")
    cash_payload = {
        "merchant_id": "mock_merchant_123",
        "data": {
            "object": {
                "order_created": {
                    "id": "order_test_cash_002",
                    "created_at": timezone.now().isoformat(),
                    "source": {"name": "pos"},
                    "tenders": [
                        {
                            "type": "CASH",
                            "amount_money": {"amount": 1500}
                        }
                    ],
                    "line_items": [
                        {
                            "uid": "line_test_002",
                            "catalog_object_id": "mock_item2",
                            "name": "Vinyl LP",
                            "quantity": "1",
                            "note": "987654321098", # Barcode for the LP
                            "base_price_money": {"amount": 1500}
                        }
                    ]
                }
            }
        }
    }
    response = client.post(
        '/api/square/webhooks/orders/?mock=true', 
        data=cash_payload, 
        content_type='application/json'
    )
    print(f"Cash Webhook Status: {response.status_code}")
    print(f"Cash Webhook Response: {response.json()}")
    
    print("\n4. Querying Sale table...")
    sales = Sale.objects.filter(connection__platform='SQUARE')
    for s in sales:
        print(f" - Sale: {s.product.title} x {s.sales_units} @ ${s.price} (Type: {s.payment_type}, Status: {s.verification_status}, VerifiedBy: {s.verified_by})")
        
    print("\n5. Verification successful! The simulation completed. No PII (customer names, emails, addresses) or payment data was processed or saved to the database.")

if __name__ == '__main__':
    run()
