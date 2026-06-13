"""
patch_admin.py — Phase 13-B3E2: Legacy Admin Migration
=======================================================
One-time script to migrate the legacy "admin" user account from the
ADMIN role (which requires a tenant_id) into the SUPER_ADMIN role
(which is exempt from tenant isolation rules), restoring login access.

Safety rules:
  - Idempotent: Safe to run multiple times.
  - Aborts cleanly if no "admin" user is found.
  - Preserves: password_hash, username, email, is_active.
  - Modifies: role, require_password_change.

Execution:
    python patch_admin.py

Rollback (if needed):
    python patch_admin.py --rollback
"""

import sys
import os

# ── Load environment variables from .env if present ────────────────────────
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

# ── Fail-fast: DATABASE_URL must be present ─────────────────────────────────
if not os.environ.get("DATABASE_URL"):
    print("ERROR: DATABASE_URL environment variable is not set.")
    print("       Export DATABASE_URL before running this script.")
    sys.exit(1)

# ── Bootstrap Flask app ──────────────────────────────────────────────────────
from app import create_app
from app.extensions import db

app = create_app()

ROLLBACK = "--rollback" in sys.argv

with app.app_context():
    from app.models import User

    # ── Locate the target user ───────────────────────────────────────────────
    user = User.query.filter_by(username="admin").first()

    if not user:
        print("ABORT: No user with username='admin' found in the database.")
        print("       Nothing was changed.")
        sys.exit(0)

    # ── Print before-state ───────────────────────────────────────────────────
    print("=" * 60)
    print("PHASE 13-B3E2: Legacy Admin Migration")
    print("=" * 60)
    print(f"  Username              : {user.username}")
    print(f"  Email                 : {user.email}")
    print(f"  Is Active             : {user.is_active}")
    print(f"  Role (BEFORE)         : {user.role}")
    print(f"  Req. PW Change(BEFORE): {user.require_password_change}")
    print(f"  Tenant ID             : {user.tenant_id}")
    print("-" * 60)

    if ROLLBACK:
        # ── Rollback: revert to ADMIN + require_password_change ─────────────
        if user.role == "ADMIN" and user.require_password_change is False:
            print("NOTE: User is already in the original ADMIN state. Nothing to rollback.")
            sys.exit(0)
        user.role = "ADMIN"
        user.require_password_change = True
        db.session.commit()
        print(f"  Role (AFTER)          : {user.role}")
        print(f"  Req. PW Change(AFTER) : {user.require_password_change}")
        print("=" * 60)
        print("ROLLBACK COMPLETE. User restored to original ADMIN role.")
        sys.exit(0)

    # ── Idempotency guard ────────────────────────────────────────────────────
    if user.role == "SUPER_ADMIN" and user.require_password_change is False:
        print("INFO: User is already a SUPER_ADMIN with no pending password change.")
        print("      Nothing to do. Migration is already applied. Exiting safely.")
        sys.exit(0)

    # ── Apply migration ──────────────────────────────────────────────────────
    user.role = "SUPER_ADMIN"
    user.require_password_change = False

    try:
        db.session.commit()
        print(f"  Role (AFTER)          : {user.role}")
        print(f"  Req. PW Change(AFTER) : {user.require_password_change}")
        print("=" * 60)
        print("SUCCESS: Migration complete.")
        print("  → Login at  : /crm/login")
        print("  → Username  : admin")
        print("  → Password  : (unchanged — your existing password)")
        print("  → Landing   : /crm/super/dashboard")
        print("=" * 60)
    except Exception as e:
        db.session.rollback()
        print(f"ERROR: Transaction failed and was rolled back.")
        print(f"       Details: {e}")
        sys.exit(1)
