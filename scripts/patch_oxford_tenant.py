import os
import sys

# Add the project root to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from app.extensions import db
from app.models import Tenant

app = create_app()

with app.app_context():
    print("Looking for Oxford tenant...")
    oxford_tenant = Tenant.query.filter_by(slug="oxford-computers").first()
    
    if oxford_tenant:
        print(f"Found Oxford tenant: ID={oxford_tenant.id}, Name={oxford_tenant.name}")
        oxford_tenant.billing_exempt = True
        # Explicitly ensure status is ACTIVE to prevent any accidental trial logic
        oxford_tenant.status = 'ACTIVE'
        db.session.commit()
        print("Successfully set billing_exempt = True for Oxford tenant. Grandfathering complete.")
    else:
        print("Oxford tenant not found (slug='oxford'). Please verify the slug in the database.")
