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


def _catalogue_categories():
    """Phase RC2.5.4c-x-5a: the closed goal-category vocabulary for the course
    form.

    Sourced from catalogue_service so the admin form and the runtime resolver
    cannot drift apart -- courses_for_category() returns nothing for a value
    outside this set, so a form offering a fifth option would let an admin
    save a category that puts their course in no menu at all. Imported lazily
    and fail-soft: an empty tuple renders a select with no options rather than
    500-ing the whole course form.
    """
    try:
        from app.services.catalogue_service import CATEGORIES
        return CATEGORIES
    except Exception:
        return ()


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


# ── Phase RC2.5.4c: Business Profile ─────────────────────────────────────────
#
# The identity ARCHITECTURE already existed and is production-validated:
# tenant_identity_service.resolve_business_identity() reads the
# `business_profile` section of TenantSettings and falls back PER FIELD to the
# platform defaults. tenant_settings_service.set_section() was written in
# RC2.5.2 as the write path and, until now, had no caller. This phase supplies
# only the UI and that write; it changes no resolution semantics.
#
# The form is deliberately fed from the RAW STORED SECTION, never from
# resolve_business_identity(). Prefilling with the resolver's output would
# show a tenant Oxford's address and phone as if they were its own saved data,
# and a save would then silently copy them in -- turning a fallback into
# authored content. Blank means "inherit the platform default", and the
# template says so.
#
# Flat form field -> nested section path. Nesting is expressed here rather
# than parsed out of the field names so a crafted field cannot invent a new
# structure inside the JSON.
_BP_SCALARS = ('legal_name', 'description', 'tagline', 'location_url',
               'brand_voice')
_BP_NESTED = {
    'address': ('line', 'locality', 'city', 'region', 'country', 'postal_code'),
    'contact': ('phone', 'whatsapp', 'email', 'website'),
    'hours': ('general', 'extended'),
}
# `description` is a textarea; the others are single-line inputs.
_BP_MULTILINE = ('description',)


def _business_profile_form(form):
    """Build the `business_profile` section from submitted form fields.

    Only the keys this phase owns are read, so a stray field cannot inject a
    new key. Blank fields are OMITTED rather than stored as "", because an
    empty string is a value the per-field resolver would honour, and the
    contract's meaning of "not set" is "absent". That also keeps the two-state
    semantics honest: a wholly blank submission yields {}, which
    resolve_business_identity() reads as is_configured=False.
    """
    section = {}
    for key in _BP_SCALARS:
        raw = form.get(f'bp_{key}', '')
        value = raw.strip() if key not in _BP_MULTILINE else raw.strip('\r\n ')
        if value:
            section[key] = value
    for group, keys in _BP_NESTED.items():
        sub = {}
        for key in keys:
            value = form.get(f'bp_{group}_{key}', '').strip()
            if value:
                sub[key] = value
        if sub:
            section[group] = sub
    return section


@tenant_bp.route('/profile', methods=['GET', 'POST'])
@login_required
@tenant_admin_required
def tenant_profile():
    """Phase 13-B3B: Company Profile — editable Tenant columns.
    Phase RC2.5.4c: plus the tenant-authored Business Profile section.

    The two are persisted separately on purpose: `name`, `industry` and
    `billing_email` are Tenant COLUMNS with their existing semantics, while
    the Business Profile is a JSON section written through set_section() so
    sibling sections (branding, locale, features) survive untouched.
    """
    from app.services import tenant_settings_service
    from app.services.tenant_identity_service import SETTINGS_KEY

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

        # ── Tenant columns: existing behaviour, unchanged ─────────────────
        tenant.name = name
        if industry:
            tenant.industry = industry
        if billing_email:
            tenant.billing_email = billing_email

        # ── Business Profile: the RC2.5.4c addition ───────────────────────
        # tenant.id, never a tenant identifier from the request. The form
        # carries no tenant field at all, so there is nothing to trust.
        try:
            tenant_settings_service.set_section(
                tenant.id, SETTINGS_KEY, _business_profile_form(request.form))
            db.session.commit()
            flash('Company profile updated successfully.', 'success')
        except Exception:
            db.session.rollback()
            flash('An error occurred while saving. Please try again.', 'danger')

        return redirect(url_for('tenant.tenant_profile'))

    # Raw stored section -- deliberately NOT the resolved identity. See the
    # comment above _BP_SCALARS.
    profile = tenant_settings_service.get_section(tenant.id, SETTINGS_KEY) or {}
    if not isinstance(profile, dict):
        profile = {}
    return render_template('tenant/profile.html', tenant=tenant,
                           profile=profile,
                           bp_address=profile.get('address') or {},
                           bp_contact=profile.get('contact') or {},
                           bp_hours=profile.get('hours') or {})


@tenant_bp.route('/staff', methods=['GET', 'POST'])
@login_required
@tenant_admin_required
def tenant_staff():
    """
    Tenant-portal staff management, backed by the User table.

    Phase 13-B3B introduced this alongside the legacy CRM registry, creating
    two parallel staff authorities — the architectural split that RC2.2D was
    later run to close. Since RC2.2D the CRM staff screens read the same User
    rows this page writes, so staff created here appear throughout the CRM.

    This page creates LOGIN accounts (username, email and password required).
    The CRM's own Staff Management screen creates directory entries without
    credentials. Both write User rows in the same tenant.
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
            email = request.form.get('email', '').strip().lower()
            password = request.form.get('password', '')

            if not username or not password or not email:
                flash('Username, Email, and Password are required.', 'danger')
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
        
    from app.models import Tenant

    phone_number_id = request.form.get('phone_number_id', '').strip()
    access_token = request.form.get('access_token', '').strip()

    if not phone_number_id or not phone_number_id.isdigit():
        flash('Valid numeric Phone Number ID is required.', 'danger')
        return redirect(url_for('tenant.tenant_whatsapp'))

    # Phase RC2.4.2: refuse another tenant's WhatsApp identity.
    #
    # This path previously validated only .isdigit(), so a tenant admin could
    # enter ANY Phone Number ID — including one already claimed by another
    # tenant. The inbound webhook resolves the tenant from this column with
    # .first() and no ORDER BY, so a duplicate would be resolved arbitrarily
    # and one tenant could start receiving another's customer conversations.
    #
    # Keeping your OWN existing id must still succeed: the form posts the
    # current value back on every save, so excluding this tenant is what makes
    # a no-op save work rather than reporting a collision with itself.
    #
    # This check is a FRIENDLY GUARD, not the integrity boundary. Two
    # concurrent requests can both pass it; the partial unique index added in
    # migration b8f4c2e97d15 is what actually prevents the duplicate, and the
    # IntegrityError handler below turns that into the same readable message.
    #
    # The message deliberately does not name the other tenant — a tenant admin
    # has no business learning which customer of ours holds an id.
    _clash = Tenant.query.filter(
        Tenant.waba_phone_number_id == phone_number_id,
        Tenant.id != tenant.id,
    ).first()
    if _clash is not None:
        flash('That WhatsApp Phone Number ID is already configured for '
              'another account. Each WhatsApp number can belong to only one '
              'account.', 'danger')
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
    except IntegrityError:
        # Phase RC2.4.2: the unique index is the final boundary. Reached when
        # two requests race past the check above, or if the index is enforced
        # against data the check did not see. The user gets the same readable
        # message rather than a raw database error.
        db.session.rollback()
        flash('That WhatsApp Phone Number ID is already configured for '
              'another account. Each WhatsApp number can belong to only one '
              'account.', 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Failed to save settings: {e}', 'danger')

    return redirect(url_for('tenant.tenant_whatsapp'))


@tenant_bp.route('/whatsapp/clear', methods=['POST'])
@login_required
@tenant_admin_required
def tenant_whatsapp_clear():
    """Phase RC2.4.2: release this tenant's WhatsApp identity.

    WHY THIS EXISTS
    ---------------
    tenant_whatsapp_save() rejects an empty Phone Number ID, so before this
    route there was no way to UNSET one. Combined with the uniqueness index and
    ADR-011 (tenants are never deleted, only SUSPENDED), a churned tenant would
    have held its Phone Number ID forever and permanently blocked reuse of that
    WhatsApp number — a realistic scenario when a number is reassigned.

    IT CLEARS THE CREDENTIAL TOO, AND THAT IS NOT AN INVENTION
    ----------------------------------------------------------
    The existing architecture already treats id + token as a PAIR, at three
    independent sites:

        whatsapp_service._get_waba_credentials  `if id and token:`
        tenant_whatsapp_test                    `if not id or not token:`
        templates/tenant/whatsapp.html          `{% if has_token and id %}`

    and production holds 0 half-configured rows in either direction. Clearing
    only the id would therefore create a state this system has never had and
    that no consumer expects. Releasing the identity releases its credential.

    SCOPE: this tenant only. _get_current_tenant() resolves the caller's own
    tenant, so no other tenant's configuration is reachable. No lead,
    conversation, message or tenant row is deleted — ADR-011's data-permanence
    policy is untouched; only the WhatsApp binding is released.
    """
    tenant = _get_current_tenant()
    if not tenant:
        flash('No tenant associated with your account.', 'danger')
        return redirect(url_for('tenant.tenant_home'))

    if not tenant.waba_phone_number_id and not tenant.waba_access_token_encrypted:
        flash('No WhatsApp configuration to clear.', 'warning')
        return redirect(url_for('tenant.tenant_whatsapp'))

    _released = tenant.waba_phone_number_id
    tenant.waba_phone_number_id = None
    tenant.waba_access_token_encrypted = None

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash(f'Failed to clear WhatsApp settings: {e}', 'danger')
        return redirect(url_for('tenant.tenant_whatsapp'))

    # Auditable through the existing mechanism (Constitution I.7). Records that
    # the binding was released and by whom; the id itself is configuration, not
    # customer data, and naming it is what makes a later reassignment traceable.
    try:
        from app.services.audit_service import log_audit, request_ip
        log_audit('TENANT_SETTINGS_CHANGE',
                  actor=getattr(current_user, 'email', None)
                  or getattr(current_user, 'username', None),
                  tenant_id=tenant.id,
                  target='waba_phone_number_id',
                  detail={'event': 'waba_identity_released',
                          'released_phone_number_id': _released},
                  ip=request_ip())
    except Exception:                                       # noqa: BLE001
        # An audit failure must not undo a completed, committed release.
        import logging
        logging.exception('RC2.4.2: audit write failed for WABA clear')

    flash('WhatsApp configuration cleared. This Phone Number ID is now '
          'available to be configured again.', 'success')
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


# \u2500\u2500 Phase RC2.5.4a: Courses & Knowledge (READ-ONLY) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
#
# A management SURFACE over the existing tenant_knowledge architecture. No
# writes in this phase: no create, edit, delete, pricing, offer or payment-URL
# mutation. Those are RC2.5.4b+.
#
# Reads go through knowledge_admin_service, NOT knowledge_service: the prompt
# path is bounded at MAX_ITEMS=8 and filters is_active=True, which would
# silently show 8 of Oxford's 18 rows and hide every inactive one. See that
# module's docstring. knowledge_service is untouched by this phase.
#
# ISOLATION: tenant_id comes from _get_current_tenant() -- the authenticated
# session -- and never from the query string, a form field, or the URL path.
# The <int:row_id> below is the ROW id, and it is resolved by (id AND
# tenant_id) together, so another tenant's row id 404s rather than leaking.

@tenant_bp.route('/courses', methods=['GET'])
@login_required
@tenant_admin_required
def tenant_courses():
    """Phase RC2.5.4a: paginated, read-only listing of this tenant's
    knowledge rows -- active AND inactive, so an admin can see everything
    they own."""
    from app.services import knowledge_admin_service

    tenant = _get_current_tenant()
    if not tenant:
        flash('No tenant associated with your account.', 'danger')
        return redirect(url_for('tenant.tenant_home'))

    kind = (request.args.get('kind') or '').strip() or None
    page = request.args.get('page', 1, type=int) or 1

    result = knowledge_admin_service.list_knowledge(
        tenant.id, kind=kind, page=page
    )

    rows = []
    for row in result['rows']:
        attrs = knowledge_admin_service.parse_attributes(row)
        commercial = attrs.get('commercial') or {}
        rows.append({
            'row': row,
            'duration': attrs.get('duration'),
            'currency': commercial.get('currency'),
            'base_price': commercial.get('base_price'),
        })

    return render_template(
        'tenant/courses.html',
        tenant=tenant,
        rows=rows,
        result=result,
        active_kind=kind,
    )


@tenant_bp.route('/courses/<int:row_id>', methods=['GET'])
@login_required
@tenant_admin_required
def tenant_course_detail(row_id):
    """Phase RC2.5.4a: read-only detail for one knowledge row owned by the
    current tenant. A row belonging to another tenant is simply not found."""
    from app.services import knowledge_admin_service

    tenant = _get_current_tenant()
    if not tenant:
        flash('No tenant associated with your account.', 'danger')
        return redirect(url_for('tenant.tenant_home'))

    row = knowledge_admin_service.get_knowledge(tenant.id, row_id)
    if row is None:
        abort(404)

    attrs = knowledge_admin_service.parse_attributes(row)
    commercial = attrs.get('commercial') or {}
    regulatory = attrs.get('regulatory') or {}

    offers = commercial.get('offers')
    if not isinstance(offers, list):
        offers = []

    components = regulatory.get('components')
    if not isinstance(components, list):
        components = []

    # Scalars that are neither commercial nor regulatory (e.g. duration,
    # historical_alias) -- shown as-is so an admin can see exactly what the
    # AI layer has to work with.
    other_attrs = {
        k: v for k, v in attrs.items()
        if k not in ('commercial', 'regulatory')
        and isinstance(v, (str, int, float, bool))
    }

    return render_template(
        'tenant/course_detail.html',
        tenant=tenant,
        row=row,
        commercial=commercial,
        regulatory=regulatory,
        offers=offers,
        components=components,
        other_attrs=other_attrs,
    )


# ── Phase RC2.5.4b: Courses & Knowledge CRUD ─────────────────────────────────
#
# Create, edit and activate/deactivate. There is deliberately NO hard-delete
# route: deactivation is the only removal, and it is reversible.
#
# ISOLATION, restated because it is the whole game on a write path:
# tenant_id ALWAYS comes from _get_current_tenant() -- the authenticated
# session. It is never read from request.form, request.args, a hidden field
# or the URL. The <int:row_id> in the path is the ROW id, and every mutation
# resolves it via get_knowledge(tenant.id, row_id), i.e. by (id AND
# tenant_id) together, so another tenant's row id yields an ordinary 404 that
# does not reveal whether the row exists elsewhere.
#
# All DB mutation lives in knowledge_admin_service -- these routes resolve
# the tenant, delegate, and translate the result into flash + redirect,
# matching the POST-redirect-GET pattern every other tenant route uses.
#
# CSRF: this project has no CSRF mechanism (Flask-WTF is not installed and
# CSRFProtect is never initialised), so these forms match the existing
# posture of /tenant/profile, /tenant/staff, /tenant/ai and
# /tenant/whatsapp/save rather than inventing a parallel one. Flagged in the
# RC2.5.4b report as a real, pre-existing platform-wide gap.

@tenant_bp.route('/courses/new', methods=['GET'])
@login_required
@tenant_admin_required
def tenant_course_new():
    """Phase RC2.5.4b: blank create form."""
    tenant = _get_current_tenant()
    if not tenant:
        flash('No tenant associated with your account.', 'danger')
        return redirect(url_for('tenant.tenant_home'))

    from app.services import knowledge_admin_service
    return render_template(
        'tenant/course_form.html',
        tenant=tenant,
        row=None,
        values={},
        allowed_kinds=knowledge_admin_service.ALLOWED_KINDS,
        # RC2.5.4c-x-5a: the closed goal-category vocabulary, sourced
        # from catalogue_service so the form and the resolver cannot
        # drift apart.
        allowed_categories=_catalogue_categories(),
    )


@tenant_bp.route('/courses/create', methods=['POST'])
@login_required
@tenant_admin_required
def tenant_course_create():
    """Phase RC2.5.4b: create one knowledge row for the CURRENT tenant."""
    from app.services import knowledge_admin_service

    tenant = _get_current_tenant()
    if not tenant:
        flash('No tenant associated with your account.', 'danger')
        return redirect(url_for('tenant.tenant_home'))

    row, errors = knowledge_admin_service.create_knowledge(
        tenant.id, request.form
    )
    if errors:
        for message in errors:
            flash(message, 'danger')
        return render_template(
            'tenant/course_form.html',
            tenant=tenant,
            row=None,
            values=request.form,
            allowed_kinds=knowledge_admin_service.ALLOWED_KINDS,
            # RC2.5.4c-x-5a: the closed goal-category vocabulary, sourced
            # from catalogue_service so the form and the resolver cannot
            # drift apart.
            allowed_categories=_catalogue_categories(),
        ), 400

    flash(f'"{row.title}" created.', 'success')
    return redirect(url_for('tenant.tenant_course_detail', row_id=row.id))


@tenant_bp.route('/courses/<int:row_id>/edit', methods=['GET', 'POST'])
@login_required
@tenant_admin_required
def tenant_course_edit(row_id):
    """Phase RC2.5.4b: edit one row owned by the current tenant."""
    from app.services import knowledge_admin_service

    tenant = _get_current_tenant()
    if not tenant:
        flash('No tenant associated with your account.', 'danger')
        return redirect(url_for('tenant.tenant_home'))

    row = knowledge_admin_service.get_knowledge(tenant.id, row_id)
    if row is None:
        abort(404)

    if request.method == 'POST':
        updated, errors = knowledge_admin_service.update_knowledge(
            tenant.id, row_id, request.form
        )
        if errors is None:
            abort(404)
        if errors:
            for message in errors:
                flash(message, 'danger')
            return render_template(
                'tenant/course_form.html',
                tenant=tenant,
                row=row,
                values=request.form,
                allowed_kinds=knowledge_admin_service.ALLOWED_KINDS,
                # RC2.5.4c-x-5a: the closed goal-category vocabulary, sourced
                # from catalogue_service so the form and the resolver cannot
                # drift apart.
                allowed_categories=_catalogue_categories(),
            ), 400

        flash(f'"{updated.title}" updated.', 'success')
        return redirect(url_for('tenant.tenant_course_detail', row_id=row_id))

    # GET: prefill from the stored row.
    attrs = knowledge_admin_service.parse_attributes(row)
    commercial = attrs.get('commercial') or {}
    components = (attrs.get('regulatory') or {}).get('components')
    by_type = {}
    if isinstance(components, list):
        for c in components:
            if isinstance(c, dict) and c.get('type') is not None:
                by_type[c['type']] = c.get('amount')

    # RC2.5.4c-x-5a: the form edits these as one comma-separated string and a
    # multi-select. Stored shape is a list of strings at the top level; a row
    # that has neither key prefills empty rather than erroring.
    stored_keywords = attrs.get('keywords')
    stored_categories = attrs.get('categories')

    values = {
        'title': row.title,
        'kind': row.kind,
        'body': row.body or '',
        'sort_order': row.sort_order,
        'duration': attrs.get('duration') or '',
        'keywords': ', '.join(
            k for k in (stored_keywords or []) if isinstance(k, str)),
        'categories': [c for c in (stored_categories or [])
                       if isinstance(c, str)],
        # RC2.5.5b-1: the stable key the payment resolver matches on.
        'code': commercial.get('code') or '',
        'currency': commercial.get('currency') or '',
        'base_price': commercial.get('base_price')
        if commercial.get('base_price') is not None else '',
        'payment_url': commercial.get('payment_url') or '',
        'registration_fee': by_type.get('registration_fee', ''),
        'tuition_fee': by_type.get('max_tuition_fee', ''),
        'concession': by_type.get('concession', ''),
        'net_tuition_fee': by_type.get('net_tuition_fee', ''),
        'exam_fee': by_type.get('exam_fee', ''),
    }
    return render_template(
        'tenant/course_form.html',
        tenant=tenant,
        row=row,
        values=values,
        allowed_kinds=knowledge_admin_service.ALLOWED_KINDS,
        # RC2.5.4c-x-5a: the closed goal-category vocabulary, sourced
        # from catalogue_service so the form and the resolver cannot
        # drift apart.
        allowed_categories=_catalogue_categories(),
    )


@tenant_bp.route('/courses/<int:row_id>/toggle', methods=['POST'])
@login_required
@tenant_admin_required
def tenant_course_toggle(row_id):
    """Phase RC2.5.4b: deactivate or reactivate one row.

    Soft only -- there is no hard-delete route anywhere in this blueprint.
    """
    from app.services import knowledge_admin_service

    tenant = _get_current_tenant()
    if not tenant:
        flash('No tenant associated with your account.', 'danger')
        return redirect(url_for('tenant.tenant_home'))

    desired = (request.form.get('active') or '').strip().lower() in (
        '1', 'true', 'yes', 'on'
    )
    row, errors = knowledge_admin_service.set_active(tenant.id, row_id, desired)
    if row is None and errors is None:
        abort(404)
    if errors:
        for message in errors:
            flash(message, 'danger')
    else:
        flash(
            f'"{row.title}" {"reactivated" if desired else "deactivated"}.',
            'success',
        )
    return redirect(url_for('tenant.tenant_course_detail', row_id=row_id))
