import os
import sys
from datetime import datetime, timedelta

# Add the project root to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from app.extensions import db
from app.models import Tenant

app = create_app()

def run_billing_worker():
    """
    Phase 13-B4.1C: Trial and Subscription State Machine Worker.
    Runs periodically to enforce billing states.
    """
    with app.app_context():
        now = datetime.utcnow()
        print(f"[{now.isoformat()}] Running Billing Worker...")
        
        # 1. TRIAL -> PAST_DUE (Grace period starts)
        # Find tenants where trial has expired, not exempt, and still in TRIAL state
        expired_trials = Tenant.query.filter(
            Tenant.status == 'TRIAL',
            Tenant.trial_ends_at < now,
            Tenant.billing_exempt == False
        ).all()
        
        for t in expired_trials:
            print(f"Tenant {t.slug} (ID: {t.id}) trial expired. Transitioning to PAST_DUE.")
            t.status = 'PAST_DUE'
            t.past_due_at = now
            
        # 2. PAST_DUE -> SUSPENDED (Grace period ends)
        # Find tenants where PAST_DUE > 3 days
        grace_period_cutoff = now - timedelta(days=3)
        expired_grace = Tenant.query.filter(
            Tenant.status == 'PAST_DUE',
            Tenant.past_due_at < grace_period_cutoff,
            Tenant.billing_exempt == False
        ).all()
        
        for t in expired_grace:
            print(f"Tenant {t.slug} (ID: {t.id}) grace period expired. Transitioning to SUSPENDED.")
            t.status = 'SUSPENDED'
            
        try:
            db.session.commit()
            print("Billing worker execution completed successfully.")
        except Exception as e:
            db.session.rollback()
            print(f"Error during billing worker execution: {e}")

if __name__ == '__main__':
    run_billing_worker()
