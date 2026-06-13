"""
app/routes/tenant.py
Phase 13-B3B: Tenant Admin Portal

Blueprint for the Tenant Admin self-service portal.
Prefix: /tenant

Security rules:
- tenant_admin_required: allows ADMIN + SUPER_ADMIN, denies STAFF
- All queries scoped via tenant_query() / tenant_filter()
- SUPER_ADMIN bypasses tenant isolation (read + write visibility across tenants)

Forbidden: webhook.py, models.py, migrations, services/*, bot/*
"""

from functools import wraps
from flask import (
    Blueprint, request, render_template, redirect,
    url_for, flash, abort
)
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from app.extensions import db

tenant_bp = Blueprint('tenant', __name__, url_prefix='/tenant')

@tenant_bp.before_request
def tenant_security_guard():
    """
    Phase 13-B4.1C: Provider-Agnostic SaaS Billing Middleware
    Ensures tenants with blocked statuses cannot access configuration routes.
    """
    from app.routes.admin import check_billing_status
    if request.path.startswith('/tenant/billing'):
        return
        
    # Phase 13-B4.1B: Allow read-only access to WhatsApp page during suspension
    if request.path == '/tenant/whatsapp' and request.method == 'GET':
        return
        
    billing_redirect = check_billing_status()
    if billing_redirect:
        return billing_redirect



# ── Phase 13-B3B: Decorator ───────────────────────────────────────────────────

def tenant_admin_required(f):
    """
    Allows: ADMIN, SUPER_ADMIN
    Denies: STAFF (403), unauthenticated (redirect to login)
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('admin.crm_login'))
        role = getattr(current_user, 'role', None)
        if role not in ('ADMIN', 'SUPER_ADMIN'):
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_current_tenant():
    """
    Returns the Tenant object for the current user.
    SUPER_ADMIN: must pass ?tenant_id= in query string (future support).
    ADMIN: always uses their own tenant_id.
    Returns None if no valid tenant found.
    """
    from app.models import Tenant
    role = getattr(current_user, 'role', None)
    if role == 'SUPER_ADMIN':
        # Future: support ?tenant_id= for cross-tenant inspection
        tid = request.args.get('tenant_id') or getattr(current_user, 'tenant_id', None)
    else:
        tid = getattr(current_user, 'tenant_id', None)
    if not tid:
        return None
    return Tenant.query.get(tid)


def _tenant_user_query():
    """
    Returns a User query scoped to the current tenant.
    SUPER_ADMIN: unscoped (sees all).
    ADMIN: scoped to their tenant_id.
    """
    from app.models import User
    role = getattr(current_user, 'role', None)
    tid = getattr(current_user, 'tenant_id', None)
    if role == 'SUPER_ADMIN':
        return User.query
    return User.query.filter_by(tenant_id=tid)


# ── Routes ────────────────────────────────────────────────────────────────────

@tenant_bp.route('/home', methods=['GET'])
@login_required
@tenant_admin_required
def tenant_home():
    """Phase 13-B3B: Tenant Admin Overview (read-only)."""
    from app.models import User
    tenant = _get_current_tenant()
    if not tenant:
        flash('No tenant associated with your account.', 'danger')
        return redirect(url_for('admin.crm_login'))

    # Staff count scoped to this tenant
    tid = tenant.id
    staff_count = User.query.filter_by(tenant_id=tid, role='STAFF').count()
    admin_count = User.query.filter_by(tenant_id=tid, role='ADMIN').count()

    return render_template(
        'tenant/home.html',
        tenant=tenant,
        staff_count=staff_count,
        admin_count=admin_count
    )


@tenant_bp.route('/profile', methods=['GET', 'POST'])
@login_required
@tenant_admin_required
def tenant_profile():
    """Phase 13-B3B: Company Profile — editable fields only (name, industry, billing_email)."""
    tenant = _get_current_tenant()
    if not tenant:
        flash('No tenant associated with your account.', 'danger')
        return redirect(url_for('tenant.tenant_home'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        industry = request.form.get('industry', '').strip()
        billing_email = request.form.get('billing_email', '').strip()

        if not name:
            flash('Business Name cannot be empty.', 'danger')
            return redirect(url_for('tenant.tenant_profile'))

        tenant.name = name
        if industry:
            tenant.industry = industry
        if billing_email:
            tenant.billing_email = billing_email

        try:
            db.session.commit()
            flash('Company profile updated successfully.', 'success')
        except Exception:
            db.session.rollback()
            flash('An error occurred while saving. Please try again.', 'danger')

        return redirect(url_for('tenant.tenant_profile'))

    return render_template('tenant/profile.html', tenant=tenant)


@tenant_bp.route('/staff', methods=['GET', 'POST'])
@login_required
@tenant_admin_required
def tenant_staff():
    """
    Phase 13-B3B: Staff Management via User table only.
    Does NOT touch staff_master.json — legacy system left untouched.
    """
    from app.models import User
    tenant = _get_current_tenant()
    if not tenant:
        flash('No tenant associated with your account.', 'danger')
        return redirect(url_for('tenant.tenant_home'))

    if request.method == 'POST':
        action = request.form.get('action')

        # ── Create Staff ──────────────────────────────────────────────────
        if action == 'create':
            username = request.form.get('username', '').strip()
            email = request.form.get('email', '').strip() or None
            password = request.form.get('password', '')

            if not username or not password:
                flash('Username and Password are required.', 'danger')
                return redirect(url_for('tenant.tenant_staff'))

            # Duplicate username within this tenant
            existing = User.query.filter_by(
                tenant_id=tenant.id, username=username
            ).first()
            if existing:
                flash(f'A staff member with username "{username}" already exists.', 'danger')
                return redirect(url_for('tenant.tenant_staff'))

            # Duplicate email globally (email must be unique per Phase 13-A2B)
            if email:
                email_exists = User.query.filter_by(email=email).first()
                if email_exists:
                    flash(f'Email "{email}" is already in use.', 'danger')
                    return redirect(url_for('tenant.tenant_staff'))

            try:
                new_staff = User(
                    username=username,
                    email=email,
                    password_hash=generate_password_hash(password),
                    role='STAFF',
                    tenant_id=tenant.id,
                    is_active=True,
                    require_password_change=True
                )
                db.session.add(new_staff)
                db.session.commit()
                flash(f'Staff member "{username}" created successfully.', 'success')
            except IntegrityError:
                db.session.rollback()
                flash('Could not create staff member. Please try again.', 'danger')

            return redirect(url_for('tenant.tenant_staff'))

        # ── Deactivate / Reactivate Staff ─────────────────────────────────
        elif action == 'toggle':
            user_id = request.form.get('user_id', type=int)
            if not user_id:
                flash('Invalid user.', 'danger')
                return redirect(url_for('tenant.tenant_staff'))

            # Ensure the target user belongs to THIS tenant
            staff = User.query.filter_by(
                id=user_id, tenant_id=tenant.id, role='STAFF'
            ).first()
            if not staff:
                flash('Staff member not found.', 'danger')
                return redirect(url_for('tenant.tenant_staff'))

            staff.is_active = not staff.is_active
            db.session.commit()
            status_word = 'activated' if staff.is_active else 'deactivated'
            flash(f'Staff member "{staff.username}" has been {status_word}.', 'success')
            return redirect(url_for('tenant.tenant_staff'))

    # ── GET: list staff scoped to this tenant ────────────────────────────
    from app.models import User
    staff_list = User.query.filter_by(
        tenant_id=tenant.id, role='STAFF'
    ).order_by(User.created_at.desc()).all()

    return render_template(
        'tenant/staff.html',
        tenant=tenant,
        staff_list=staff_list
    )


@tenant_bp.route('/ai', methods=['GET', 'POST'])
@login_required
@tenant_admin_required
def tenant_ai():
    """Phase 13-B3B: AI Persona Settings — saves to Tenant model."""
    tenant = _get_current_tenant()
    if not tenant:
        flash('No tenant associated with your account.', 'danger')
        return redirect(url_for('tenant.tenant_home'))

    if request.method == 'POST':
        persona_name = request.form.get('ai_persona_name', '').strip() or None
        prompt_override = request.form.get('ai_prompt_override', '').strip() or None

        tenant.ai_persona_name = persona_name
        tenant.ai_prompt_override = prompt_override

        try:
            db.session.commit()
            flash('AI settings saved successfully.', 'success')
        except Exception:
            db.session.rollback()
            flash('An error occurred while saving AI settings.', 'danger')

        return redirect(url_for('tenant.tenant_ai'))

    return render_template('tenant/ai.html', tenant=tenant)


@tenant_bp.route('/billing', methods=['GET'])
@login_required
@tenant_admin_required
def tenant_billing():
    """Phase 13-B3B: Billing — read-only status display. No Stripe."""
    tenant = _get_current_tenant()
    if not tenant:
        flash('No tenant associated with your account.', 'danger')
        return redirect(url_for('tenant.tenant_home'))

    return render_template('tenant/billing.html', tenant=tenant)


@tenant_bp.route('/whatsapp', methods=['GET'])
@login_required
@tenant_admin_required
def tenant_whatsapp():
    """Phase 13-B4E2: WABA Onboarding UI"""
    tenant = _get_current_tenant()
    if not tenant:
        flash('No tenant associated with your account.', 'danger')
        return redirect(url_for('tenant.tenant_home'))
        
    has_token = bool(tenant.waba_access_token_encrypted)
    return render_template('tenant/whatsapp.html', tenant=tenant, has_token=has_token)


@tenant_bp.route('/whatsapp/save', methods=['POST'])
@login_required
@tenant_admin_required
def tenant_whatsapp_save():
    from app.services.encryption_service import encrypt_token
    tenant = _get_current_tenant()
    if not tenant:
        flash('No tenant associated with your account.', 'danger')
        return redirect(url_for('tenant.tenant_home'))
        
    phone_number_id = request.form.get('phone_number_id', '').strip()
    access_token = request.form.get('access_token', '').strip()
    
    if not phone_number_id or not phone_number_id.isdigit():
        flash('Valid numeric Phone Number ID is required.', 'danger')
        return redirect(url_for('tenant.tenant_whatsapp'))
        
    tenant.waba_phone_number_id = phone_number_id
    
    if access_token:
        # Encrypt and save new token
        try:
            tenant.waba_access_token_encrypted = encrypt_token(access_token)
        except Exception as e:
            flash(f'Encryption failed: {e}', 'danger')
            return redirect(url_for('tenant.tenant_whatsapp'))
            
    try:
        db.session.commit()
        flash('WhatsApp settings saved successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Failed to save settings: {e}', 'danger')
        
    return redirect(url_for('tenant.tenant_whatsapp'))


@tenant_bp.route('/whatsapp/test', methods=['POST'])
@login_required
@tenant_admin_required
def tenant_whatsapp_test():
    import requests
    from app.services.encryption_service import decrypt_token
    
    tenant = _get_current_tenant()
    if not tenant:
        flash('No tenant associated with your account.', 'danger')
        return redirect(url_for('tenant.tenant_home'))
        
    if not tenant.waba_phone_number_id or not tenant.waba_access_token_encrypted:
        flash('Cannot test: Missing Phone Number ID or Access Token.', 'warning')
        return redirect(url_for('tenant.tenant_whatsapp'))
        
    try:
        token = decrypt_token(tenant.waba_access_token_encrypted)
        url = f"https://graph.facebook.com/v21.0/{tenant.waba_phone_number_id}"
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"})
        
        if r.status_code == 200:
            flash('WhatsApp connection successful! \u2705', 'success')
        else:
            flash(f'Meta API Error ({r.status_code}): {r.text}', 'danger')
    except Exception as e:
        flash(f'Test connection failed: {e}', 'danger')
        
    return redirect(url_for('tenant.tenant_whatsapp'))
