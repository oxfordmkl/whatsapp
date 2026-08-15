import logging
from sqlalchemy import or_
from flask import Blueprint, request, jsonify, render_template, redirect, flash, url_for, current_app, session
from app.config import ADMIN_KEY

import json

# ── Phase RC2.2G Stage 4C: the legacy staff registry is RETIRED ──────────────
#
# get_staff_json_path(), load_staff_registry() and save_staff_registry() lived
# here, backed by app/data/staff_master.json. All three and the file are now
# deleted.
#
# The registry was a single GLOBAL file with no tenant dimension: every tenant
# read the same rows, so a newly provisioned institute saw Oxford's staff. It
# also shipped inside the deployed image, so Railway discarded every staff edit
# on the next deploy. RC2.2D migrated all 16 consumers to the tenant-scoped
# User table (Stages 1-2 and Batches 1-3, each deployed and production
# validated); by Batch 3 the file had zero runtime readers and zero writers.
#
# staff_service.as_registry() / active_display_names() are the replacements and
# return the same shape. tests/legacy_staff_registry.py holds a frozen snapshot
# of what the file contained at retirement, so the Oxford-parity assertions
# written during the migration still compare against the original values.
#
# `import os` went with them — nothing else in this module used it.


def normalize_staff_name(name):
    """
    Normalizes staff names for reporting (e.g. 'kiran', 'KIRAN', ' Kiran ' -> 'Kiran').
    Does not modify database records.
    """
    if not name:
        return "Unassigned"
    cleaned = name.strip()
    if not cleaned:
        return "Unassigned"
    return cleaned.title()

from app.state import count_states, count_pending_followups, get_all_states, get_stage_breakdown
from app.services.whatsapp_service import send_text
# Phase 10.9B.2: warn-only transition diagnostics. Safe to import at module
# level — the engine imports no Flask and touches no database until called.
from app.services import sales_transition_service as _sts_mod
# Phase RC2.3D: dual-write helper. Reads STAFF_IDENTITY_DUAL_WRITE internally
# and is a no-op while that flag is OFF, so importing it changes nothing.
from app.services.staff_backfill_service import sync_assigned_user as _sync_assigned_user
# Phase RC2.2D Stage 1: tenant-scoped staff registry. Read-only, imports no
# Flask and touches no database until called.
from app.services import staff_service
# Phase H3-1B-a: assigned_staff write validation. Read-only, imports no Flask,
# and touches no database until called.
from app.services import staff_identity_service

from functools import wraps
from flask import abort

def super_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('admin.crm_super_login'))
        if getattr(current_user, 'role', None) != 'SUPER_ADMIN':
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """Decorator: allows ADMIN and SUPER_ADMIN only. STAFF receives 403."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('admin.crm_login'))
        if getattr(current_user, 'role', None) not in ('ADMIN', 'SUPER_ADMIN'):
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

# ── Phase 13-B3D: Hardened Tenant Isolation Helpers ──────────────────────────

def tenant_query(model, tenant_id=None):
    """
    Phase 13-B3D: Returns a safely tenant-scoped query object.
    - If tenant_id is explicitly provided, filters by that tenant.
    - If SUPER_ADMIN, checks for session['impersonate_tenant_id'] and filters if present.
      If not impersonating, returns unfiltered query (deliberate — see below).
    - Otherwise, falls back to current_user.tenant_id.

    Phase 14B.3 (C2) — FAILS CLOSED.
    ---------------------------------
    This previously ended in `return model.query`, unfiltered, whenever a
    tenant could not be resolved. The primitive the entire codebase trusts for
    isolation therefore DEFAULTED TO EXPOSING EVERY TENANT'S ROWS, and did so
    silently: a caller that forgot to pass a tenant, or ran outside a request,
    got a working query returning other customers' data rather than an error.

    It now returns an empty query instead. A missing tenant context yields no
    rows, so the failure mode is a visibly empty screen — annoying, reported in
    minutes, and harmless — rather than a cross-customer data leak nobody sees.
    The query object is still a real Query, so every existing call site can
    keep chaining .filter()/.order_by()/.count()/.first() unchanged.

    The SUPER_ADMIN branch is deliberately NOT changed. A non-impersonating
    SUPER_ADMIN is the platform operator and its unfiltered view is a feature,
    not the C2 defect: it is reached only for an authenticated user holding
    that role, never by an unresolved context. Middleware already confines such
    a session to /crm/super/ routes.
    """
    from sqlalchemy import false

    try:
        from flask_login import current_user as _cu
        from flask import session

        if getattr(_cu, 'role', None) == 'SUPER_ADMIN':
            impersonate_id = session.get('impersonate_tenant_id')
            if impersonate_id:
                return model.query.filter_by(tenant_id=impersonate_id)
            return model.query

        tid = tenant_id or getattr(_cu, 'tenant_id', None)
    except Exception:
        tid = tenant_id

    if tid:
        return model.query.filter_by(tenant_id=tid)

    # Unresolvable tenant — return NOTHING, and say so. This is unreachable
    # from any authenticated route (every caller either passes a tenant or has
    # current_user.tenant_id set), so a log line here means a genuine bug.
    logging.warning(
        "tenant_query(%s) could not resolve a tenant — returning an empty "
        "query (Phase 14B.3 fail-closed). This indicates a missing tenant "
        "scope at the call site.", getattr(model, "__name__", model))
    return model.query.filter(false())


def tenant_filter(query_obj, model, tenant_id=None):
    """
    Phase 13-B3D: Appends tenant scoping to a db.session.query(...) chain.

    Phase 14B.4 (C2 twin) — FAILS CLOSED, exactly as tenant_query() does.
    This carried the identical defect: it ended in `return query_obj`,
    unscoped, whenever a tenant could not be resolved. Leaving one of the two
    helpers fail-open while the other failed closed would be worse than either
    state on its own, because two similarly-named primitives would behave
    oppositely under the same failure and no call site could tell which it had.

    The SUPER_ADMIN branch is deliberately unchanged, for the reason given in
    tenant_query(): a non-impersonating platform operator's unfiltered view is
    a feature, and is reached only for an authenticated holder of that role.
    """
    from sqlalchemy import false

    try:
        from flask_login import current_user as _cu
        from flask import session

        if getattr(_cu, 'role', None) == 'SUPER_ADMIN':
            impersonate_id = session.get('impersonate_tenant_id')
            if impersonate_id:
                return query_obj.filter(model.tenant_id == impersonate_id)
            return query_obj

        tid = tenant_id or getattr(_cu, 'tenant_id', None)
    except Exception:
        tid = tenant_id

    if tid:
        return query_obj.filter(model.tenant_id == tid)

    logging.warning(
        "tenant_filter(%s) could not resolve a tenant — returning an empty "
        "query (Phase 14B.4 fail-closed). This indicates a missing tenant "
        "scope at the call site.", getattr(model, "__name__", model))
    return query_obj.filter(false())


EVENT_SCORE_MAP = {
    "LEAD_CREATED": 2,
    "FIRST_MESSAGE_RECEIVED": 3,
    "AI_RESPONSE_SENT": 5,
    "COURSE_VIEWED": 10,
    "PLACEMENT_ASKED": 15,
    "FEES_REQUESTED": 20,
    "DEMO_REQUESTED": 25,
    "PAYMENT_PENDING": 30
}

INTELLIGENCE_CONSTANTS = {
    "THRESHOLD_HOT": 80,
    "THRESHOLD_WARM": 50,
}

def get_aging_bucket(days_inactive, mode="health"):
    """Phase 10N-B Unified Aging Helper. Preserves legacy mathematical behavior."""
    if mode == "health":
        if days_inactive <= 2: return "Fresh"
        if days_inactive <= 6: return "Attention"
        if days_inactive <= 13: return "Aging"
        return "Critical"
    elif mode == "automation":
        if days_inactive <= 3: return "fresh"
        if days_inactive <= 7: return "attention"
        if days_inactive <= 15: return "risk"
        return "dormant"


# ── Phase 7E: Course Journey helpers ───────────────────────────────────────
from app.bot.constants import normalize_course_name

def get_course_enquiries(phone: str, tenant_id=None) -> list:
    """
    Return chronologically-ordered, case-insensitively deduplicated list of
    course names derived from any of the following events for this phone:

        COURSE_ENQUIRY   — event_data is JSON: '{"course": "Python Programming"}'
        COURSE_VIEWED    — event_data is a plain string: "Python Programming"
        FEES_REQUESTED   — event_data is a plain string or NULL
        DEMO_REQUESTED   — event_data is usually NULL; skipped when absent

    Returns [] on any error, missing table, or no matching events.
    Zero writes. One DB query. Read-only.

    Phase 7E.1: expanded from COURSE_ENQUIRY-only to full enquiry union.
    """
    import json
    try:
        from app.models import LeadEvent
        events = (
            tenant_query(LeadEvent, tenant_id)
            .filter(
                LeadEvent.phone == phone,
                LeadEvent.event_type.in_([
                    "COURSE_ENQUIRY",
                    "COURSE_VIEWED",
                    "FEES_REQUESTED",
                    "DEMO_REQUESTED",
                ])
            )
            .order_by(LeadEvent.created_at.asc())
            .all()
        )

        seen   = set()   # lowercase course names already added
        result = []      # preserves first-seen casing, chronological order

        for e in events:
            raw = e.event_data

            # ── Extract course name using format appropriate to event type ──
            if e.event_type == "COURSE_ENQUIRY":
                # Written by crm_lead_update as JSON: {"course": "..."}
                try:
                    data   = json.loads(raw or "{}")
                    course = (data.get("course") or "").strip()
                except (ValueError, TypeError):
                    # Defensive fallback: treat as plain string if JSON fails
                    course = (raw or "").strip()
            else:
                # COURSE_VIEWED / FEES_REQUESTED written by router.py as plain string
                # DEMO_REQUESTED is usually NULL — strip() on None would fail,
                # so guard with (raw or "")
                course = (raw or "").strip()

            # ── Normalize alias → canonical name, then deduplicate ────────
            course = normalize_course_name(course)
            if course and course.lower() not in seen:
                seen.add(course.lower())
                result.append(course)

        return result
    except Exception:
        return []


def get_course_admissions(phone: str, tenant_id=None) -> list:
    """
    Return deduplicated list of course names for which a COURSE_ADMISSION
    event exists for this phone. Returns [] on any error or empty table.

    event_data is a JSON string: '{"course": "Python Programming"}'
    """
    import json
    try:
        from app.models import LeadEvent
        events = (
            tenant_query(LeadEvent, tenant_id)
            .filter_by(phone=phone, event_type="COURSE_ADMISSION")
            .order_by(LeadEvent.created_at.asc())
            .all()
        )
        seen = set()
        result = []
        for e in events:
            try:
                data = json.loads(e.event_data or "{}")
                course = (data.get("course") or "").strip()
            except (ValueError, TypeError):
                continue
            # ── Normalize alias → canonical name, then deduplicate ────────
            course = normalize_course_name(course)
            if course and course.lower() not in seen:
                seen.add(course.lower())
                result.append(course)
        return result
    except Exception:
        return []


def calculate_lead_intelligence(manual_score, events):
    unique_event_types = set(e.event_type for e in events)
    auto_score = sum(EVENT_SCORE_MAP.get(et, 0) for et in unique_event_types)
    final_score = min((manual_score or 0) + auto_score, 100)
    
    if final_score >= INTELLIGENCE_CONSTANTS["THRESHOLD_HOT"]:
        temperature = "HOT"
    elif final_score >= INTELLIGENCE_CONSTANTS["THRESHOLD_WARM"]:
        temperature = "WARM"
    else:
        temperature = "COLD"
        
    if "PAYMENT_PENDING" in unique_event_types:
        action = "Payment Follow-up"
    elif "DEMO_REQUESTED" in unique_event_types:
        action = "Send Demo"
    elif "FEES_REQUESTED" in unique_event_types:
        action = "Send Fees"
    elif "PLACEMENT_ASKED" in unique_event_types:
        action = "Discuss Placement"
    elif final_score >= INTELLIGENCE_CONSTANTS["THRESHOLD_HOT"]:
        action = "Call Today"
    elif "LEAD_CREATED" in unique_event_types and final_score <= 15:
        action = "Qualify Lead"
    else:
        action = "Admission Follow-up"
        
    return {
        "final_score": final_score,
        "temperature": temperature,
        "recommended_action": action,
        "_events": list(unique_event_types)
    }

def calculate_lead_health(state_updated_at, state_created_at, latest_msg_time, latest_event_time, intelligence, needs_reply, assigned_staff=None, is_admitted=False):
    latest_activity = latest_msg_time or latest_event_time or state_updated_at or state_created_at
    if not latest_activity:
        from datetime import datetime
        latest_activity = datetime.now()
        
    from datetime import datetime
    days_inactive = (datetime.now() - latest_activity).days
    if days_inactive < 0:
        days_inactive = 0
        
    aging_status = get_aging_bucket(days_inactive, mode="health")
        
    escalation = None
    events_set = intelligence.get("_events", [])
    if intelligence.get("temperature") == "HOT" and aging_status == "Critical":
        escalation = "🚨 HOT Lead Ignored"
    elif needs_reply and days_inactive >= 1:
        escalation = "💬 Waiting For Reply"
    elif assigned_staff and assigned_staff != "Unassigned" and days_inactive >= 7 and not is_admitted:
        escalation = "⚠️ Neglected Lead"
    elif "FEES_REQUESTED" in events_set and days_inactive >= 7:
        escalation = "💰 Fees Follow-up Needed"
    elif "DEMO_REQUESTED" in events_set and days_inactive >= 7:
        escalation = "🎓 Demo Follow-up Needed"

    return {
        "aging_status": aging_status,
        "days_inactive": days_inactive,
        "escalation": escalation
    }


# ── Phase 8.4: Lead Portfolio Dashboard ────────────────────────────────────

def calculate_lead_portfolio(lead, events: list, course_journey: dict) -> dict:
    """
    Build a complete portfolio summary for a single lead.

    ZERO new DB queries — all data derived from objects already loaded
    in crm_lead_detail() before this helper is called:

        lead          → ConversationState ORM row
        events        → list[LeadEvent] (already fetched, ASC order)
        course_journey→ {"enquiries": [...], "admissions": [...]}
                         (already computed by get_course_enquiries /
                          get_course_admissions)

    Returns a plain dict safe for Jinja2 template rendering.
    Read-only. No writes. No side effects.
    """
    from datetime import datetime

    # ── Event-type counters (single O(n) pass) ────────────────────────────
    total_events       = len(events)
    course_views       = 0
    fees_requests      = 0
    demo_requests      = 0
    admissions_count   = 0
    placement_asked    = 0

    timestamps = []   # collect all created_at values for timeline metrics

    for ev in events:
        et = ev.event_type
        if et == "COURSE_VIEWED":
            course_views += 1
        elif et == "FEES_REQUESTED":
            fees_requests += 1
        elif et == "DEMO_REQUESTED":
            demo_requests += 1
        elif et == "COURSE_ADMISSION":
            admissions_count += 1
        elif et == "PLACEMENT_ASKED":
            placement_asked += 1
        if ev.created_at:
            timestamps.append(ev.created_at)

    # ── Timeline Portfolio ────────────────────────────────────────────────
    # Seed with ConversationState created_at so leads with no events still
    # show a first-contact date (the moment they first messaged the bot).
    if lead.created_at:
        timestamps.append(lead.created_at)
    if lead.updated_at:
        timestamps.append(lead.updated_at)

    if timestamps:
        first_contact    = min(timestamps)
        latest_activity  = max(timestamps)
        relationship_days = max(0, (latest_activity - first_contact).days)
    else:
        now              = datetime.utcnow()
        first_contact    = now
        latest_activity  = now
        relationship_days = 0

    # ── Course Portfolio (from already-computed course_journey) ───────────
    courses_enquired  = course_journey.get("enquiries",  [])
    courses_admitted  = course_journey.get("admissions", [])

    return {
        # Engagement
        "total_events":            total_events,
        "course_views":            course_views,
        "fees_requests":           fees_requests,
        "demo_requests":           demo_requests,
        "admissions_count":        admissions_count,
        "placement_asked":         placement_asked,
        # Course
        "total_course_enquiries":  len(courses_enquired),
        "total_course_admissions": len(courses_admitted),
        "courses_enquired":        courses_enquired,
        "courses_admitted":        courses_admitted,
        # Timeline
        "first_contact":           first_contact,
        "latest_activity":         latest_activity,
        "relationship_days":       relationship_days,
    }


admin_bp = Blueprint("admin", __name__)

def check_billing_status():
    """
    Phase 13-B4.1C: Provider-Agnostic SaaS Billing Middleware
    Ensures tenants with blocked statuses cannot access the CRM.
    """
    from flask import request, redirect, url_for, flash, session
    from flask_login import current_user
    from app.models import Tenant
    
    if not current_user.is_authenticated:
        return None
        
    role = getattr(current_user, 'role', None)
    if role == 'SUPER_ADMIN':
        # Safely resolve impersonated tenant for Super Admin
        tid = request.args.get('tenant_id') or session.get('impersonate_tenant_id') or getattr(current_user, 'tenant_id', None)
    else:
        tid = getattr(current_user, 'tenant_id', None)
        
    if not tid:
        return None
        
    tenant = Tenant.query.get(tid)
    if not tenant or tenant.billing_exempt:
        return None
        
    if tenant.status in ['SUSPENDED', 'CANCELLED']:
        flash("Your subscription is suspended. Access is restricted.", "danger")
        return redirect(url_for('tenant.tenant_billing'))
    elif tenant.status == 'PAST_DUE':
        flash("Your account is past due. Please update your billing info to avoid suspension.", "warning")
        
    return None

@admin_bp.before_request
def admin_security_guard():
    if not request.path.startswith('/crm/'):
        return
        
    from flask_login import current_user
    
    if current_user.is_authenticated:
        # 1. Require Password Change Enforcement
        if getattr(current_user, 'require_password_change', False):
            allowed_paths = ('/crm/setup-password', '/crm/logout')
            if request.path not in allowed_paths and not request.path.startswith('/static/'):
                return redirect(url_for('admin.crm_setup_password'))
                
        # 2. SUPER_ADMIN CRM Protection
        if getattr(current_user, 'role', None) == 'SUPER_ADMIN':
            # Block access to regular CRM routes if not impersonating
            if request.path.startswith('/crm/') and not request.path.startswith('/crm/super/'):
                if request.path not in ('/crm/logout', '/crm/setup-password'):
                    from flask import session
                    if not session.get('impersonate_tenant_id'):
                        flash("You must impersonate a tenant to access CRM routes.", "warning")
                        return redirect(url_for('admin.crm_super_dashboard'))
                        
        # 3. Phase 13-B4.1: SaaS Billing Middleware
        if not request.path.startswith('/crm/super/'):
            if request.path not in ('/crm/logout', '/crm/setup-password'):
                billing_redirect = check_billing_status()
                if billing_redirect:
                    return billing_redirect

@admin_bp.context_processor
def inject_actor():
    from app.routes.admin import get_current_actor
    return dict(get_current_actor=get_current_actor)


@admin_bp.route("/trigger-followup", methods=["POST"])
def trigger_followup():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    body    = request.get_json(silent=True) or {}
    phone   = body.get("phone", "")
    message = body.get("message", "")

    if not phone or not message:
        return jsonify({"error": "phone and message are required"}), 400

    if not phone.startswith("91"):
        phone = "91" + phone.lstrip("0")

    r = send_text(phone, message)
    return jsonify({"ok": r.status_code == 200, "status": r.status_code, "phone": phone})


@admin_bp.route("/stats", methods=["GET"])
def stats():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    # Phase 14C: "active_conversations" removed. get_all_states() returned
    # EVERY lead in EVERY tenant with name, stage, course and last message
    # text — a full cross-customer PII dump behind a single static header key.
    # Aggregate counts are what a monitoring probe needs and carry no personal
    # data, so the endpoint keeps its operational value without the exposure.
    # A caller needing per-lead data must use the authenticated CRM, which is
    # tenant-scoped.
    return jsonify({
        "total_leads":          count_states(),
        "pending_followups":    count_pending_followups(),
        "stage_breakdown":      get_stage_breakdown(),
        "active_conversations": None,
        "note": "active_conversations removed in Phase 14C (PII); use the CRM",
    })


@admin_bp.route("/panel", methods=["GET"])
def admin_panel():
    if not check_auth():
        return (
            "<html><body style='font-family:sans-serif;text-align:center;"
            "padding:50px;background:#0a0f0d;color:#25D366'>"
            "<h2>🔒 Access Denied</h2>"
            "<p style='color:#888'>URL-il ?key=YOUR_ADMIN_KEY add cheyyuka</p>"
            "</body></html>"
        ), 403
    try:
        with open("templates/panel.html", "r", encoding="utf-8") as f:
            return f.read(), 200, {"Content-Type": "text/html"}
    except FileNotFoundError:
        return "templates/panel.html not found in project", 404


# ── Phase 9.7: CRM Home Dashboard ──────────────────────────────────────────
# Future Tenant Scope: tenant_id filtering will be applied here (Phase 11)
# Future Auth Scope: role-based KPI visibility will be applied here (Phase 10)

def calculate_home_kpis(tenant_id=None):
    """
    Lightweight summary aggregation for the Home Dashboard.
    Reuses existing model queries — no new DB schema required.

    # Future Tenant Scope: Add .filter_by(tenant_id=current_tenant) to all queries
    # Future Auth Scope: Scope to staff's assigned leads for STAFF role
    """
    from app.models import ConversationState, LeadEvent, ConversationMessage
    from app.extensions import db
    from sqlalchemy.sql import func
    from datetime import datetime

    # ── Phase RC2.2F (H1): ONE tenant context for the whole dashboard ──────
    #
    # This function scoped its lead and activity queries with the passed
    # tenant_id but called get_all_tasks() with NO argument, so the task KPIs
    # silently fell back to current_user while everything else honoured the
    # parameter. Nothing hit that today — crm_home passes nothing, so both
    # resolved to the same tenant — but any caller supplying an explicit
    # tenant (a platform report, an impersonation path, a scheduled job) would
    # have rendered ONE dashboard mixing two tenants' numbers.
    #
    # Resolving once and using _tid everywhere is behaviour-identical for
    # every existing caller: tenant_query()/tenant_filter() return on their
    # SUPER_ADMIN branch BEFORE consulting the argument, and for every other
    # role `tenant_id or current_user.tenant_id` yields exactly what
    # _actor_tenant_id() yields. The only case that changes is the one that
    # was wrong.
    #
    # Resolved defensively: _actor_tenant_id() reads current_user, which does
    # not exist outside a request context.
    _tid = tenant_id
    if not _tid:
        try:
            _tid = _actor_tenant_id()
        except Exception:                                   # noqa: BLE001
            _tid = None

    # Future Tenant Scope: total_leads = ConversationState.query.filter_by(tenant_id=tid).count()
    total_leads = tenant_query(ConversationState, _tid).count()
    admissions  = tenant_query(ConversationState, _tid).filter(ConversationState.is_admitted == True).count()

    # HOT leads: consistent with EVENT_SCORE_MAP logic in calculate_lead_intelligence
    # Using lead_score column as lightweight proxy — full intelligence calc runs on leads page
    hot_leads = tenant_query(ConversationState, _tid).filter(
        ConversationState.lead_score >= INTELLIGENCE_CONSTANTS["THRESHOLD_HOT"]
    ).count()

    # Needs reply: last message for each phone was incoming
    # Future Tenant Scope: join tenant_id filter here
    _subq_base = db.session.query(
        ConversationMessage.phone,
        func.max(ConversationMessage.id).label('max_id')
    )
    _subq_base = tenant_filter(_subq_base, ConversationMessage, _tid)
    subq = _subq_base.group_by(ConversationMessage.phone).subquery()
    needs_reply_count = tenant_filter(
        db.session.query(ConversationMessage), ConversationMessage, _tid
    ).join(
        subq, ConversationMessage.id == subq.c.max_id
    ).filter(ConversationMessage.direction == 'incoming').count()

    # Task KPIs — reuse existing get_all_tasks() helper
    try:
        # RC2.2F (H1): the tenant is now propagated. get_all_tasks() already
        # accepted one; it simply was not being given it.
        open_tasks, _ = get_all_tasks(_tid)
        now = datetime.utcnow()
        open_task_count = len(open_tasks)
        overdue_count = sum(
            1 for t in open_tasks
            if t.get("due_dt") and t["due_dt"] < now
        )
    except Exception:
        open_task_count = 0
        overdue_count = 0

    # Phase RC2.2D Batch 3: the "Staff Active" card now counts the TENANT's own
    # active staff instead of the global file.
    #
    # This is the card that produced the production contradiction: it reported
    # 3 for every tenant (Oxford's Anju/Kiran/Nisha) while the migrated
    # Dashboard, Workload and Allocation screens correctly showed 0 for a
    # tenant with no staff. After this batch every staff-related screen derives
    # its directory from the same tenant-scoped User source.
    #
    # RC2.2F (H1): uses the same _tid resolved once at the top of this
    # function. It previously resolved its own copy — identical logic, but two
    # resolutions of the same thing is how they drift apart later.
    staff_active = len(staff_service.active_display_names(_tid))

    # Recent leads (last 5 by created_at)
    # Future Tenant Scope: .filter_by(tenant_id=tid)
    recent_leads = tenant_query(ConversationState, _tid).order_by(
        ConversationState.created_at.desc()
    ).limit(5).all()

    # Recent events (last 10 LeadEvents for activity feed)
    recent_events = tenant_query(LeadEvent, _tid).order_by(
        LeadEvent.created_at.desc()
    ).limit(10).all()

    return {
        "total_leads":    total_leads,
        "hot_leads":      hot_leads,
        "open_tasks":     open_task_count,
        "overdue_tasks":  overdue_count,
        "needs_reply":    needs_reply_count,
        "admissions":     admissions,
        "staff_active":   staff_active,
        "recent_leads":   recent_leads,
        "recent_events":  recent_events,
    }


@admin_bp.route("/crm/home", methods=["GET"])
def crm_home():
    """
    Phase 9.7: CRM Home Dashboard — unified command center landing page.

    # Future Tenant Scope: kpis will be scoped per tenant (Phase 11)
    # Future Auth Scope: ADMIN | STAFF | SUPER_ADMIN (Phase 10)
    """
    actor = get_current_actor()
    if not check_auth():
        logging.warning(f"AUTH_FAILURE username={actor['username']} role={actor['role']} source={actor['source']} route=/crm/home")
        return _deny()
    logging.info(f"AUTH_SUCCESS username={actor['username']} role={actor['role']} source={actor['source']} route=/crm/home")

    if actor.get("role") == "STAFF":
        return render_template(
            "crm_home_staff.html",
            key=request.args.get("key", ""),
            actor=actor
        )

    kpis = calculate_home_kpis()

    return render_template(
        "crm_home.html",
        key=request.args.get("key", ""),
        kpis=kpis,
    )


# ── Phase 9.7: Marketing Hub ────────────────────────────────────────────────
# Future Tenant Scope: Per-tenant broadcast configs and contact lists (Phase 11)
# Future Auth Scope: ADMIN | SUPER_ADMIN only (Phase 10)

@admin_bp.route("/crm/marketing", methods=["GET"])
def crm_marketing():
    """
    Phase 9.7: Marketing Hub — unified CRM shell wrapping broadcast functionality.
    The legacy /panel route is preserved and remains fully functional.

    # Future Tenant Scope: Load per-tenant server URL + broadcast API key here
    # Future Auth Scope: Check role == ADMIN or SUPER_ADMIN
    """
    if not check_auth():
        return _deny()

    return render_template(
        "crm_marketing.html",
        key=request.args.get("key", ""),
    )

@admin_bp.route("/crm/marketing/start_job", methods=["POST"])
@admin_required
def crm_marketing_start_job():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 403
        
    data = request.get_json() or {}
    phones = data.get("phones", [])
    message = data.get("message", "").strip()
    campaign_name = data.get("campaign_name", "Marketing Hub Custom").strip()
    
    if not phones:
        return jsonify({"error": "No contacts provided"}), 400
    if not message:
        return jsonify({"error": "Message is required"}), 400
        
    from app.services.campaign_service import start_campaign
    
    try:
        start_campaign(phones, message, campaign_name, tenant_id=_actor_tenant_id())
        return jsonify({"success": True, "count": len(phones)})
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Phase 10.3: shared lead helpers ──────────────────────────────────────────

def normalize_lead_phone(raw):
    """Normalise a phone number to the storage form used by the WhatsApp path.

    Mirrors the inline rule already applied in trigger_followup() and
    broadcast.py ("91" + digits, leading zeros stripped) so a lead typed in by
    hand collides with the SAME (phone, tenant_id) row that an inbound WhatsApp
    message would create. Without this a walk-in entered as "09847312534" and
    the same person messaging from "919847312534" become two leads.

    Returns "" when nothing usable remains; callers must reject that.
    """
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if not digits:
        return ""
    digits = digits.lstrip("0")
    if not digits:
        return ""
    if not digits.startswith("91"):
        digits = "91" + digits
    return digits


def canonical_lead_status(raw, default=None):
    """Return the approved LEAD_STATUSES spelling of `raw`, else `default`.

    Phase 10.9A. Applies the Phase 10.5 strategy (case-insensitive match,
    canonicalised to the approved spelling) to the two operator-facing FORM
    paths, which had no validation at all: crm_lead_update assigned
    request.form["lead_status"] straight to the model, and crm_lead_new passed
    it straight to the constructor. The dropdown was the only constraint, and a
    crafted POST bypasses it.

    That mattered beyond tidiness. An unrecognised status has no matching
    PipelineStage, so _sync_sales_stage_link() clears sales_stage_id and the
    lead drops out of the Sales Pipeline entirely — silently undoing the 100%
    coverage established in Phase 10.8C.3.

    Returns `default` for blank input too, so callers can express "no value
    submitted" and "value rejected" with the same fallback: keep what is
    already stored. This never widens the vocabulary — the return value is
    always either an entry from LEAD_STATUSES or the caller's default.

    Deliberately NOT wired into crm_leads_import: that path already validates
    (Phase 10.5) and needs to report per-row errors into its import summary,
    which this helper has no way to express. Adopting it there is a tidy-up for
    a later phase, not part of this scope.
    """
    from app.models import LEAD_STATUSES
    value = (raw or "").strip()
    if not value:
        return default
    return next((s for s in LEAD_STATUSES if s.lower() == value.lower()), default)


def transition_verdict(tenant_id, from_status, to_status, context):
    """Phase 10.9B.2 — WARN-ONLY. Ask the transition engine, block nothing.

    Returns a TransitionVerdict, or None if the engine could not answer.

    Every caller ignores `verdict.allowed` entirely; the value is recorded and
    nothing else. That is the whole point of this phase: with only one operator
    transition in lead_stage_history, enforcing a matrix would mean enforcing
    assumptions nobody has tested. Observability first, rules second.

    NEVER RAISES. The engine already fails open internally, but a lead edit
    must not fail because a diagnostic did — this phase is required to preserve
    existing behaviour exactly, and an exception here would break that promise.
    """
    try:
        from app.services import sales_transition_service as _sts
        return _sts.can_transition(from_status, to_status,
                                   tenant_id=tenant_id, context=context)
    except Exception:
        logging.exception("Transition engine failed for %r -> %r (%s)",
                          from_status, to_status, context)
        return None


def transition_detail(verdict, context):
    """The audit-detail fragment for a verdict, or {} when there is none.

    Nested under a single "transition" key so it extends the existing
    LEAD_STATUS_CHANGE detail rather than colliding with from/to/stage ids.

    Deliberately NOT a new audit action: audit_service.VALID_ACTIONS gates
    every action and carries a guard test asserting its exact size, so adding
    one for a diagnostic that is temporary by design would permanently widen
    the audit vocabulary. `detail` is already JSON, so a new key inside it
    costs no migration and no constant change.

    `context` is passed in rather than read off the verdict — the engine is
    unmodified by this phase.
    """
    if verdict is None:
        return {}
    return {"transition": {
        "code": verdict.code,
        "rule": verdict.rule_id,
        "severity": verdict.severity,
        "context": context,
    }}


def _build_leads_query(tenant_id, actor, search="", stage_filter="", admitted_filter=""):
    """The canonical filtered lead query — shared by the list and the export.

    Ordering is updated_at DESC, which the Phase 10.2A composite index
    (tenant_id, updated_at) exists to serve.

    The STAFF ownership restriction lives HERE rather than at the call site so
    it cannot be forgotten by a future caller: any route reusing this helper
    inherits both tenant scoping and the "STAFF sees only their own leads"
    rule automatically.
    """
    from app.models import ConversationState
    from sqlalchemy.sql import func

    q = tenant_query(ConversationState, tenant_id)

    is_staff = (actor.get("source") == "SESSION" and actor.get("role") == "STAFF")
    if is_staff:
        # Phase RC2.3E-1 Batch 1a: ownership resolves through the dual-read
        # helper instead of a hand-rolled name comparison.
        #
        # source == "SESSION" means get_current_actor() built this dict FROM
        # current_user, so current_user IS the actor — no name lookup, and
        # therefore no ambiguity to resolve.
        #
        # This FIXES a latent defect. The old predicate compared
        # assigned_staff to the actor's USERNAME, but assigned_staff holds
        # DISPLAY LABELS: the assignment dropdown is active_display_names(),
        # which is display_label() per user. The two agree only while
        # display_name is unset. Staff Management writes display_name on both
        # create (admin.py:1927) and edit, so a staff member added as code
        # RAVI with display name "Ravi Kumar" owns leads reading
        # "Ravi Kumar" while this filter looked for "ravi" — and saw NOTHING.
        # owner_filter() keys off display_label(), and off the FK once the
        # flag is on. Production has no display_name set today, so no row
        # changes hands on this deploy.
        q = q.filter(staff_identity_service.owner_filter(
            ConversationState, current_user))

    if search:
        q = q.filter(or_(
            ConversationState.phone.ilike(f"%{search}%"),
            ConversationState.name.ilike(f"%{search}%"),
        ))
    if stage_filter:
        q = q.filter(ConversationState.stage == stage_filter)
    if admitted_filter == "yes":
        q = q.filter(ConversationState.is_admitted == True)   # noqa: E712
    elif admitted_filter == "no":
        q = q.filter(ConversationState.is_admitted != True)   # noqa: E712

    return q.order_by(ConversationState.updated_at.desc())


# Column order for CSV export/import. `phone` leads because it is the business
# key; the remainder is ConversationState.to_dict() plus the timestamps that
# to_dict() omits. Import accepts this same header, so an exported file can be
# edited and re-imported without transformation.
LEAD_CSV_FIELDS = [
    "phone", "name", "lead_status", "assigned_staff", "lead_score",
    "stage", "course", "goal", "batch_time", "offer_course",
    "is_admitted", "notes", "created_at", "updated_at",
]

# Fields an import is permitted to write. Deliberately excludes stage, last_msg,
# last_text and the timestamps: those are owned by the bot/state engine, and
# letting a spreadsheet overwrite conversation state would corrupt the funnel.
LEAD_IMPORT_WRITABLE = [
    "name", "lead_status", "assigned_staff", "lead_score",
    "course", "notes",
]


@admin_bp.route("/crm/leads", methods=["GET"])
def crm_leads():
    if not check_auth():
        return _deny()

    from app.models import ConversationState
    from app.extensions import db

    PAGE_SIZE = 25

    # ── Query params ──────────────────────────────────────────────────────────
    page            = max(1, request.args.get("page", 1, type=int))
    search          = request.args.get("search", "").strip()
    stage_filter    = request.args.get("stage", "").strip()
    admitted_filter = request.args.get("admitted", "").strip()
    key             = request.args.get("key", "")

    # \u2500\u2500 Build query safely \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    # Phase 10.3: the filter/scoping logic moved into _build_leads_query() so
    # the CSV export applies exactly the same tenant scoping, STAFF ownership
    # restriction and filters as the list. Duplicating it would let the two
    # drift, and an export that is broader than the list an operator can see is
    # a data-leak, not a cosmetic bug.
    actor = get_current_actor()
    # Phase RC2.3E-1 H4: _actor_tenant_id() honours impersonation; the getattr
    # form returns NULL for a SUPER_ADMIN. crm_leads_export already used
    # _actor_tenant_id(), so the list and its export were resolving the tenant
    # by different rules — precisely the list/export drift the comment above
    # calls a data-leak risk rather than a cosmetic bug.
    _tid = _actor_tenant_id()
    q = _build_leads_query(_tid, actor, search, stage_filter, admitted_filter)
    is_staff = (actor.get("source") == "SESSION" and actor.get("role") == "STAFF")

    pagination = q.paginate(page=page, per_page=PAGE_SIZE, error_out=False)

    # ── Dashboard metrics & Intelligence Cache (Phase 6C) ──────────────────
    from app.models import LeadEvent, ConversationMessage, FollowUpJob
    from sqlalchemy.sql import func
    
    total_leads = tenant_query(ConversationState, _tid).count()
    # Phase 14B.4: scoped. count_pending_followups() counts FollowUpJob across
    # EVERY tenant — correct for /health, which is a platform-level probe, but
    # wrong here: this figure is rendered on a tenant's own leads page, so it
    # disclosed a count derived from other institutes' follow-up queues.
    pending_fu = tenant_query(FollowUpJob, _tid).filter_by(done=False).count()
    
    # 1. Fetch all states and events for intelligence caching
    all_states = tenant_filter(db.session.query(
        ConversationState.phone, 
        ConversationState.lead_score,
        ConversationState.updated_at,
        ConversationState.created_at,
        ConversationState.assigned_staff,
        ConversationState.is_admitted
    ), ConversationState, _tid).all()
    all_events = tenant_filter(db.session.query(LeadEvent.phone, LeadEvent.event_type, LeadEvent.created_at), LeadEvent, _tid).all()
    
    events_by_phone = {}
    latest_event_time = {}
    for e in all_events:
        events_by_phone.setdefault(e.phone, []).append(e)
        if e.phone not in latest_event_time or (e.created_at and e.created_at > latest_event_time[e.phone]):
            latest_event_time[e.phone] = e.created_at
        
    hot_count = 0
    call_today_count = 0
    critical_count = 0
    intelligence_cache = {}

    # 2. Needs Reply logic & Latest Msg
    _subq_base2 = tenant_filter(db.session.query(
        ConversationMessage.phone,
        func.max(ConversationMessage.id).label('max_id'),
        func.max(ConversationMessage.created_at).label('max_created')
    ), ConversationMessage, _tid)
    subq = _subq_base2.group_by(ConversationMessage.phone).subquery()
    
    latest_msgs = tenant_filter(db.session.query(
        ConversationMessage.phone, 
        ConversationMessage.direction,
        subq.c.max_created
    ), ConversationMessage, _tid).join(
        subq, ConversationMessage.id == subq.c.max_id
    ).all()
    
    needs_reply_phones = {r.phone for r in latest_msgs if r.direction == 'incoming'}
    needs_reply_count = len(needs_reply_phones)
    latest_msg_time = {r.phone: r.max_created for r in latest_msgs}

    for state in all_states:
        phone = state.phone
        intel = calculate_lead_intelligence(state.lead_score, events_by_phone.get(phone, []))
        
        health = calculate_lead_health(
            state.updated_at,
            state.created_at,
            latest_msg_time.get(phone),
            latest_event_time.get(phone),
            intel,
            phone in needs_reply_phones,
            assigned_staff=state.assigned_staff,
            is_admitted=state.is_admitted
        )
        intel["_health"] = health
        intelligence_cache[phone] = intel
        
        if intel["temperature"] == "HOT":
            hot_count += 1
        if intel["recommended_action"] == "Call Today":
            call_today_count += 1
        if health["aging_status"] == "Critical":
            critical_count += 1

    # 3. Follow-up Due phones for badging
    pending_jobs = tenant_filter(db.session.query(FollowUpJob.phone), FollowUpJob, _tid).filter_by(done=False).all()
    pending_fu_phones = {j.phone for j in pending_jobs}

    for lead in pagination.items:
        lead.intelligence = intelligence_cache.get(lead.phone)
        lead.needs_reply = lead.phone in needs_reply_phones
        lead.has_pending_fu = lead.phone in pending_fu_phones

    # ── All distinct stages for filter dropdown ──────────────────────────────────
    stages = [r[0] for r in tenant_filter(db.session.query(ConversationState.stage), ConversationState, _tid).distinct().all() if r[0]]

    return render_template(
        "crm_leads.html",
        pagination=pagination,
        leads=pagination.items,
        total_leads=total_leads,
        hot_count=hot_count,
        call_today_count=call_today_count,
        pending_fu=pending_fu,
        needs_reply_count=needs_reply_count,
        critical_count=critical_count,
        stages=stages,
        search=search,
        stage_filter=stage_filter,
        admitted_filter=admitted_filter,
        key=key,
        page=page,
    )


# ── Phase 10.3: Manual lead creation ─────────────────────────────────────────

@admin_bp.route("/crm/lead/new", methods=["GET", "POST"])
@admin_required
def crm_lead_new():
    """Create a lead by hand — walk-in, phone enquiry or referral.

    Until this phase a lead could only exist if the person messaged the bot
    first, so every offline enquiry was invisible to the CRM.

    Duplicate protection reuses the existing (phone, tenant_id) unique
    constraint rather than a pre-check: a SELECT-then-INSERT would still race.
    We attempt the insert and treat IntegrityError as "already exists", then
    redirect to the existing lead — the operator wanted to reach that person,
    and landing on their record is the useful outcome, not an error page.

    ADMIN/SUPER_ADMIN only (@admin_required): creation sets assignment, and the
    existing STAFF rule is that a staff member may only act on leads already
    assigned to them.
    """
    if not check_auth():
        return _deny()

    from app.models import ConversationState
    from app.extensions import db

    _tid = _actor_tenant_id()
    # Phase RC2.2D Batch 1: the assignment picker is now the tenant's own
    # active staff. Previously every tenant was offered Oxford's Anju/Kiran/
    # Nisha from the global file — and since assigned_staff is stored as a free
    # string with no server-side validation, picking one wrote a foreign name
    # onto a real lead. That is why this screen is in the first batch: it is
    # the only place the defect creates BAD DATA rather than a bad display.
    active_staff = staff_service.active_display_names(_actor_tenant_id())

    if request.method == "GET":
        from app.models import LEAD_STATUSES
        return render_template(
            "crm_lead_new.html",
            active_staff=active_staff,
            lead_status_options=list(LEAD_STATUSES),
            err=request.args.get("err", ""),
        )

    # ADR-021: refuse to write without a resolved tenant.
    if not _tid:
        return render_template("crm_lead_new.html", active_staff=active_staff,
                               lead_status_options=[], err="Tenant context required."), 403

    phone = normalize_lead_phone(request.form.get("phone", ""))
    name  = (request.form.get("name", "") or "").strip()
    if not phone:
        return redirect(url_for("admin.crm_lead_new", err="A valid phone number is required."))
    if not name:
        return redirect(url_for("admin.crm_lead_new", err="Name is required."))

    existing = tenant_query(ConversationState, _tid).filter_by(phone=phone).first()
    if existing is not None:
        return redirect(url_for("admin.crm_lead_detail", phone=phone,
                                msg="This+lead+already+exists+-+opened+the+existing+record."))

    # ── Phase H3-1B-a: reject an owner who is not this tenant's staff ──────
    #
    # assigned_staff was a free string on every write path. The ROW was
    # tenant-scoped (14B.2 C1) but the VALUE was not, so a crafted POST — or a
    # stale page — could store another tenant's staff name, a deleted member,
    # or anything at all. Production carries one such row: lead id=4, whose
    # owner was overwritten with 'Anju_display' on 2026-07-14 while Anju kept
    # completing its follow-ups.
    #
    # Rejected rather than warned because this field is a dropdown listing
    # only valid options: an invalid value here is a crafted request or a
    # stale page, not an honest typo. CSV import warns instead (H3-1B-c) —
    # different input, different UX.
    #
    # resolve_assignment() delegates to the SAME resolver the dual-write uses,
    # so a value this accepts is one whose FK will populate. Blank stays legal
    # (is_unassignment) and inactive staff still resolve — they are real users
    # whose FK lands correctly, and BLOCK_DEACTIVATION already makes
    # inactive-but-assigned a supported state.
    _owner = staff_identity_service.resolve_assignment(
        _tid, request.form.get("assigned_staff", ""))
    if not _owner.ok:
        return redirect(url_for(
            "admin.crm_lead_new",
            err=f"'{_owner.value}' is not a current staff member of this "
                f"institute — choose from the Assign To list."))

    lead = ConversationState(
        phone=phone,
        name=name,
        tenant_id=_tid,
        stage="new",
        course="", goal="", batch_time="", offer_course="",
        last_msg="", last_text="",
        # Phase 10.9A: validated against LEAD_STATUSES. A blank or
        # unrecognised submission falls back to the entry status "Lead", so a
        # new lead can never be created with a status outside the vocabulary.
        lead_status=canonical_lead_status(request.form.get("lead_status"), default="Lead"),
        # Validated above. `.value` is the operator's own spelling, trimmed —
        # NOT `.canonical`, so this write stores exactly what it stored before.
        assigned_staff=_owner.value,
        notes=(request.form.get("notes", "") or "").strip() or None,
        lead_score=0,
        is_admitted=False,
    )
    # Phase RC2.3D: mirror the assignment into assigned_user_id. No-op while
    # STAFF_IDENTITY_DUAL_WRITE is OFF. Set on the instance BEFORE the commit
    # so both columns land in one INSERT.
    _sync_assigned_user(lead, _tid)
    db.session.add(lead)
    try:
        db.session.commit()
    except Exception:
        # Unique (phone, tenant_id) violation — the row appeared between our
        # check and this insert. Send the operator to the record they wanted.
        db.session.rollback()
        return redirect(url_for("admin.crm_lead_detail", phone=phone,
                                msg="This+lead+already+exists+-+opened+the+existing+record."))

    # Post-commit only: log_audit() commits, so calling it earlier would flush
    # a partial write (Phase 10.2A).
    from app.services.audit_service import log_audit, request_ip
    log_audit("LEAD_CREATE",
              actor=getattr(current_user, "email", None) or _actor_name(),
              tenant_id=_tid, target=f"lead:{phone}",
              detail={"entry": "manual",
                      "assigned_staff": lead.assigned_staff or ""},
              ip=request_ip())

    # Phase 10.8: a manually created lead enters the pipeline — record it as
    # its first movement (from_stage_id NULL). notify=False: assignment below
    # already sends TYPE_NEW_LEAD_ASSIGNED, and a second notification for the
    # same act would be noise.
    from app.services import sales_pipeline_service as _sps
    _sps.record_stage_change(_tid, lead, None, None,
                             actor=getattr(current_user, "email", None) or _actor_name(),
                             notify=False)

    if lead.assigned_staff:
        log_audit("LEAD_ASSIGN",
                  actor=getattr(current_user, "email", None) or _actor_name(),
                  tenant_id=_tid, target=f"lead:{phone}",
                  detail={"from": "", "to": lead.assigned_staff}, ip=request_ip())
        try:
            from app.services import notification_service
            from app.models import Notification as _Notif
            notification_service.notify(
                tenant_id=_tid, recipient=lead.assigned_staff,
                notif_type=_Notif.TYPE_NEW_LEAD_ASSIGNED,
                title=f"New lead assigned: {name}",
                body=f"Added by {_actor_name()}", lead_phone=phone)
        except Exception:
            pass   # notification failure must not undo a created lead

    return redirect(url_for("admin.crm_lead_detail", phone=phone,
                            msg="Lead+created+successfully."))


# ── Phase 10.3: CSV export ───────────────────────────────────────────────────

@admin_bp.route("/crm/leads/export", methods=["GET"])
@admin_required
def crm_leads_export():
    """Stream the CURRENT filtered lead set as CSV.

    Reuses _build_leads_query(), so the export is scoped identically to the
    list the operator is looking at — same tenant, same STAFF ownership rule,
    same search/stage/admitted filters carried through on the query string.

    Streamed with a generator rather than built in memory: an export is the one
    lead path with no LIMIT, so materialising it would scale with table size.
    yield_per keeps the DB cursor incremental too.

    Phase 10.3B.1 — the generator MUST be wrapped in stream_with_context().
    A generator handed to Response() is consumed lazily, after the view has
    returned, at which point Flask has already torn down the request context.
    The first DB access inside it then has no session to bind to and raises
    "RuntimeError: Working outside of application context" — which is exactly
    what production did: the DATA_EXPORT audit row was written (it runs before
    the return, inside the context) while the download itself died mid-stream,
    so an export appeared successful in the audit log and never reached anyone.
    """
    if not check_auth():
        return _deny()

    import csv, io
    from flask import Response, stream_with_context

    _tid = _actor_tenant_id()
    if not _tid:
        return _deny()

    actor = get_current_actor()
    q = _build_leads_query(
        _tid, actor,
        request.args.get("search", "").strip(),
        request.args.get("stage", "").strip(),
        request.args.get("admitted", "").strip(),
    )

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)

        def flush():
            data = buf.getvalue()
            buf.seek(0); buf.truncate(0)
            return data

        writer.writerow(LEAD_CSV_FIELDS)
        yield flush()

        for lead in q.yield_per(200):
            d = lead.to_dict()          # reused, not reimplemented
            d["phone"]      = lead.phone            # to_dict() omits the key
            d["created_at"] = lead.created_at.isoformat() if lead.created_at else ""
            d["updated_at"] = lead.updated_at.isoformat() if lead.updated_at else ""
            writer.writerow(["" if d.get(f) is None else d.get(f) for f in LEAD_CSV_FIELDS])
            yield flush()

    from datetime import datetime as _dt
    filename = f"leads-{_dt.utcnow().strftime('%Y%m%d-%H%M%S')}.csv"

    # DATA_EXPORT has been reserved in VALID_ACTIONS since Sprint 3 with no
    # route to use it. Bulk PII leaving the system is exactly what it was for.
    from app.services.audit_service import log_audit, request_ip
    log_audit("DATA_EXPORT",
              actor=getattr(current_user, "email", None) or _actor_name(),
              tenant_id=_tid, target="leads.csv",
              detail={"rows": q.count(),
                      "filters": {"search": request.args.get("search", ""),
                                  "stage": request.args.get("stage", ""),
                                  "admitted": request.args.get("admitted", "")}},
              ip=request_ip())

    # stream_with_context keeps the request context alive for the generator's
    # lifetime, so q.yield_per() still has a live session. Streaming itself is
    # unchanged — the generator is wrapped, never materialised.
    return Response(stream_with_context(generate()), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ── Phase 10.3: CSV import ───────────────────────────────────────────────────

@admin_bp.route("/crm/leads/import", methods=["GET", "POST"])
@admin_required
def crm_leads_import():
    """Idempotent CSV import — upsert on the existing (phone, tenant_id) key.

    Idempotency comes from the unique constraint, not from bookkeeping: the
    same file imported twice creates on the first run and updates on the
    second, converging on the same state.

    Only non-empty cells are applied. A blank cell means "no opinion", never
    "clear this field" — otherwise a partially-filled spreadsheet would wipe
    data the CRM already holds, which is unrecoverable.

    Conversation-owned fields (stage, last_msg, last_text, timestamps) are not
    importable: they belong to the bot/state engine and a spreadsheet must not
    be able to rewrite where someone sits in the funnel.
    """
    if not check_auth():
        return _deny()

    if request.method == "GET":
        return render_template("crm_lead_import.html",
                               fields=LEAD_CSV_FIELDS,
                               writable=LEAD_IMPORT_WRITABLE,
                               summary=None, err=request.args.get("err", ""))

    import csv, io
    from app.models import ConversationState, LEAD_STATUSES
    from app.extensions import db

    _tid = _actor_tenant_id()
    if not _tid:
        return render_template("crm_lead_import.html", fields=LEAD_CSV_FIELDS,
                               writable=LEAD_IMPORT_WRITABLE, summary=None,
                               err="Tenant context required."), 403

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return render_template("crm_lead_import.html", fields=LEAD_CSV_FIELDS,
                               writable=LEAD_IMPORT_WRITABLE, summary=None,
                               err="Choose a CSV file to import.")

    try:
        raw = upload.read().decode("utf-8-sig")   # -sig strips the Excel BOM
    except UnicodeDecodeError:
        return render_template("crm_lead_import.html", fields=LEAD_CSV_FIELDS,
                               writable=LEAD_IMPORT_WRITABLE, summary=None,
                               err="File must be UTF-8 encoded CSV.")

    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames or "phone" not in [f.strip().lower() for f in reader.fieldnames]:
        return render_template("crm_lead_import.html", fields=LEAD_CSV_FIELDS,
                               writable=LEAD_IMPORT_WRITABLE, summary=None,
                               err="CSV must contain a 'phone' column.")

    summary = {"total": 0, "created": 0, "updated": 0, "unchanged": 0,
               "skipped": 0, "errors": [], "duplicates": []}
    seen_in_file = set()
    MAX_ROWS = 5000     # bounded so one upload cannot run unboundedly

    for idx, row in enumerate(reader, start=2):     # row 1 is the header
        if summary["total"] >= MAX_ROWS:
            summary["errors"].append(f"Stopped at {MAX_ROWS} rows — split the file and re-import.")
            break
        summary["total"] += 1
        clean = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if k}

        phone = normalize_lead_phone(clean.get("phone", ""))
        if not phone:
            summary["skipped"] += 1
            summary["errors"].append(f"Row {idx}: missing or invalid phone")
            continue
        if phone in seen_in_file:
            # Duplicate WITHIN the file — reported, and the first row wins.
            summary["duplicates"].append(f"Row {idx}: {phone} repeated in file")
            summary["skipped"] += 1
            continue
        seen_in_file.add(phone)

        score = None
        if clean.get("lead_score"):
            try:
                score = max(0, min(100, int(float(clean["lead_score"]))))
            except (TypeError, ValueError):
                summary["errors"].append(f"Row {idx}: lead_score not a number — ignored")

        # Phase 10.5: validate lead_status against the approved vocabulary.
        #
        # Import previously wrote this cell straight through, so a spreadsheet
        # could introduce statuses the operator dropdown exists to constrain —
        # production picked up "fresh" this way. That matters beyond tidiness:
        # LEAD_TERMINAL_STATUSES decides whether a lead still counts as open
        # work, and an unrecognised value is silently non-terminal, so such a
        # lead never leaves anyone's queue.
        #
        # Handling mirrors lead_score directly above: report the problem, drop
        # that one field, keep the rest of the row. Rejecting the whole row
        # would discard a good name and phone over a single typo'd cell.
        #
        # A case-only difference is canonicalised rather than rejected
        # ("enrolled" -> "Enrolled"). That is validation, not widening: the
        # vocabulary stays exactly LEAD_STATUSES.
        status_value = None
        if clean.get("lead_status"):
            raw_status = clean["lead_status"]
            match = next((s for s in LEAD_STATUSES if s.lower() == raw_status.lower()), None)
            if match is None:
                summary["errors"].append(
                    f"Row {idx}: lead_status {raw_status!r} is not a recognised status — ignored"
                )
            else:
                status_value = match

        try:
            lead = tenant_query(ConversationState, _tid).filter_by(phone=phone).first()
            created = lead is None
            # Phase 10.8: stage state before the import touches this row.
            # None for a new lead — its first pipeline entry has no prior
            # stage, which the history table records as from_stage_id NULL.
            pre_stage_id = lead.sales_stage_id if lead is not None else None
            pre_status = lead.lead_status if lead is not None else None
            if created:
                lead = ConversationState(
                    phone=phone, name=clean.get("name") or "", tenant_id=_tid,
                    stage="new", course="", goal="", batch_time="", offer_course="",
                    last_msg="", last_text="", lead_score=0, is_admitted=False,
                    lead_status="Lead",
                )
                db.session.add(lead)

            changed = []
            for field in LEAD_IMPORT_WRITABLE:
                val = clean.get(field)
                if not val:
                    continue        # blank == "no opinion", never "clear it"
                if field == "lead_score":
                    if score is not None and lead.lead_score != score:
                        lead.lead_score = score; changed.append(field)
                    continue
                if field == "lead_status":
                    # Phase 10.5: only the validated/canonicalised value is
                    # written. None means the cell failed validation and was
                    # reported above — leave the stored status untouched.
                    if status_value is not None and lead.lead_status != status_value:
                        # Phase 10.9B.2: warn-only, LOG ONLY. Deliberately not
                        # added to summary["errors"]: that list is the
                        # operator's row-by-row report and this phase blocks
                        # nothing, so surfacing warnings there would make a
                        # successful import look partly failed. Only non-ok
                        # verdicts are logged — a 500-row file must not emit
                        # 500 lines saying everything was fine.
                        _v = transition_verdict(_tid, lead.lead_status,
                                                status_value,
                                                _sts_mod.CONTEXT_CSV_IMPORT)
                        if _v is not None and _v.severity != _sts_mod.SEVERITY_OK:
                            logging.warning(
                                "Transition %s (%s) on CSV import row %s: %s",
                                _v.code, _v.rule_id, idx, _v.reason)
                        lead.lead_status = status_value; changed.append(field)
                    continue
                if field == "assigned_staff":
                    # ── Phase H3-1B-c: WARN and drop the field ─────────────
                    #
                    # CSV is the one write path that warns instead of
                    # rejecting. The four form/JSON lead paths (H3-1B-a) and
                    # the task paths (H3-1B-b) take their owner from a
                    # dropdown listing only valid options, so an invalid value
                    # there is a crafted request or a stale page. A spreadsheet
                    # is different: a typo in one cell is an honest mistake,
                    # and failing a 500-row import over it — or silently
                    # dropping the whole row — would cost the operator far more
                    # than the bad cell is worth.
                    #
                    # So this follows the two precedents already in THIS loop:
                    # lead_score ("not a number — ignored") and lead_status
                    # ("not a recognised status — ignored"). The row imports,
                    # the bad field is not written, and the reason lands in
                    # summary["errors"], which the template already renders as
                    # "Rows needing attention".
                    #
                    # Dropping rather than writing is what matters for RC2.3E:
                    # an unresolvable owner would leave assigned_user_id NULL,
                    # and under FK reads that lead becomes invisible. Not
                    # writing it keeps whatever valid owner the lead already
                    # had.
                    _owner = staff_identity_service.resolve_assignment(_tid, val)
                    if not _owner.ok:
                        summary["errors"].append(
                            f"Row {idx}: assigned_staff {val!r} is not a "
                            f"current staff member — ignored"
                        )
                        continue
                    if lead.assigned_staff != _owner.value:
                        lead.assigned_staff = _owner.value
                        changed.append(field)
                    continue
                if getattr(lead, field, None) != val:
                    setattr(lead, field, val); changed.append(field)

            # ── Phase H3-1A: dual-write the imported owner ─────────────────
            #
            # THE GAP THIS CLOSES. assigned_staff is in LEAD_IMPORT_WRITABLE,
            # so this loop writes it — but via setattr() rather than a static
            # attribute assignment, which is why every AST audit from RC2.3D
            # onward missed it. This was the EIGHTH assigned_staff write path
            # and the only one that never mirrored into assigned_user_id: an
            # imported owner left the FK NULL whether or not the name was
            # valid.
            #
            # Harmless today (nothing reads the FK) but not after RC2.3E flips
            # reads: a NULL FK means the lead vanishes from every per-staff
            # view and reappears only under Unassigned. One import of a few
            # hundred assigned rows would have produced a few hundred
            # invisible leads.
            #
            # Gated on `changed` so an import that touches only name/course
            # does not re-sync rows this file never assigned — the fix stays
            # inside "the import wrote an owner".
            #
            # No blank case to handle: the loop skips falsy values ("blank ==
            # no opinion, never clear it"), so an import cannot unassign.
            if "assigned_staff" in changed:
                _sync_assigned_user(lead, _tid)

            db.session.commit()
            if created:
                summary["created"] += 1
            elif changed:
                summary["updated"] += 1
            else:
                summary["unchanged"] += 1       # re-import of an unchanged row

            # Post-commit audit, per row, so an import is attributable.
            from app.services.audit_service import log_audit, request_ip
            if created:
                log_audit("LEAD_CREATE",
                          actor=getattr(current_user, "email", None) or _actor_name(),
                          tenant_id=_tid, target=f"lead:{phone}",
                          detail={"entry": "csv_import"},
                          ip=request_ip())
            elif changed:
                # Field NAMES only — never the values, which are customer data.
                log_audit("LEAD_UPDATE",
                          actor=getattr(current_user, "email", None) or _actor_name(),
                          tenant_id=_tid, target=f"lead:{phone}",
                          detail={"entry": "csv_import", "fields": sorted(changed)},
                          ip=request_ip())

            # Phase 10.8: record the movement for history and the lead
            # timeline, with notify=False. A 5,000-row import must never emit
            # 5,000 notifications — the approved policy limits notification to
            # operator-initiated single changes, and LEAD_IMPORT already
            # summarises the run.
            if created or "lead_status" in changed:
                from app.services import sales_pipeline_service as _sps
                _sps.record_stage_change(
                    _tid, lead, pre_stage_id, pre_status,
                    actor=_sps.ACTOR_CSV_IMPORT, notify=False)
        except Exception as exc:
            db.session.rollback()
            summary["skipped"] += 1
            summary["errors"].append(f"Row {idx}: {type(exc).__name__}")

    from app.services.audit_service import log_audit, request_ip
    log_audit("LEAD_IMPORT",
              actor=getattr(current_user, "email", None) or _actor_name(),
              tenant_id=_tid, target="leads.csv",
              detail={k: summary[k] for k in ("total", "created", "updated",
                                              "unchanged", "skipped")},
              ip=request_ip())

    return render_template("crm_lead_import.html", fields=LEAD_CSV_FIELDS,
                           writable=LEAD_IMPORT_WRITABLE, summary=summary, err="")


# ── Phase 10.8: explicit stage move ──────────────────────────────────────────

@admin_bp.route("/crm/lead/<phone>/stage", methods=["POST"])
def crm_lead_move_stage(phone):
    """Move one lead to a different Sales Pipeline stage.

    A dedicated action so an operator can advance a lead without opening the
    full edit form and resubmitting every field. It writes through the SAME
    lead_status adapter as the form — sales_stage_id is never set directly —
    so Phase 10.6's architecture is observed, not bypassed.

    check_auth() rather than @admin_required: this follows existing lead
    visibility rules, and the STAFF ownership check below mirrors
    crm_lead_update exactly, so a staff member can only move their own leads.
    """
    if not check_auth():
        return _deny()

    from app.models import ConversationState
    from app.extensions import db
    from app.services import sales_pipeline_service as sps

    _tid = _actor_tenant_id()
    if not _tid:
        return _deny()

    lead = tenant_query(ConversationState, _tid).filter_by(phone=phone).first()
    if lead is None:
        return _not_found(phone)

    # Same ownership rule as crm_lead_update — STAFF may only act on leads
    # assigned to them.
    actor = get_current_actor()
    if actor.get("source") == "SESSION" and actor.get("role") == "STAFF":
        if (lead.assigned_staff or "").strip().lower() != (actor.get("username") or "").strip().lower():
            return _deny()

    # The target stage must belong to THIS tenant's sales pipeline. stage_id
    # arrives from a form and is never trusted; get_stage() refuses another
    # tenant's stage and refuses AI-funnel stages.
    stage = sps.get_stage(_tid, request.form.get("stage_id", type=int))
    if stage is None:
        return redirect(url_for("admin.crm_lead_detail", phone=phone,
                                err="Invalid+stage+selected."))

    old_stage_id = lead.sales_stage_id
    old_status = lead.lead_status
    if old_stage_id == stage.id:
        return redirect(url_for("admin.crm_lead_detail", phone=phone,
                                msg="Lead+is+already+in+that+stage."))

    # Phase 10.9B.2: warn-only. Asked before the write; `allowed` is ignored,
    # so an operator who could make this move yesterday can still make it.
    _transition = transition_verdict(_tid, old_status, stage.display_name,
                                     _sts_mod.CONTEXT_OPERATOR_MOVE)

    try:
        lead.lead_status = stage.display_name      # adapter resolves the link
        db.session.commit()
    except Exception:
        db.session.rollback()
        return redirect(url_for("admin.crm_lead_detail", phone=phone,
                                err="Could+not+move+the+lead."))

    # Post-commit only — log_audit() commits, so calling it earlier would
    # flush a partial write (Phase 10.2A contract).
    from app.services.audit_service import log_audit, request_ip
    _actor_id = getattr(current_user, "email", None) or _actor_name()
    moved = sps.record_stage_change(_tid, lead, old_stage_id, old_status,
                                    actor=_actor_id, notify=True,
                                    notify_actor_name=_actor_name())
    detail = {"from": old_status or "", "to": lead.lead_status or "",
              "entry": "stage_move"}
    if moved:
        detail.update(moved)
    detail.update(transition_detail(_transition, _sts_mod.CONTEXT_OPERATOR_MOVE))
    log_audit("LEAD_STATUS_CHANGE", actor=_actor_id, tenant_id=_tid,
              target=f"lead:{phone}", detail=detail, ip=request_ip())

    return redirect(url_for("admin.crm_lead_detail", phone=phone,
                            msg=f"Moved+to+{stage.display_name.replace(' ', '+')}."))


# ── Phase 10.7: Sales Pipeline (read-only) ───────────────────────────────────

@admin_bp.route("/crm/pipeline", methods=["GET"])
def crm_sales_pipeline():
    """Sales Pipeline dashboard — stage distribution for this tenant.

    check_auth() rather than @admin_required: pipeline visibility follows the
    existing lead visibility rules, so STAFF may view it. Their counts are
    filtered to leads they own by the service layer — a tenant-wide count is a
    leak even when no individual lead is shown.

    Holds no query logic: sales_pipeline_service owns every query.
    """
    if not check_auth():
        return _deny()

    from app.services import sales_pipeline_service as sps

    _tid = _actor_tenant_id()
    if not _tid:
        return _deny()

    actor = get_current_actor()
    summary = sps.get_pipeline_summary(_tid, actor)
    metrics = sps.get_conversion_metrics(summary)

    return render_template(
        "crm_sales_pipeline.html",
        key=request.args.get("key", ""),
        open_stages=[s for s in summary if not s["is_terminal"]],
        terminal_stages=[s for s in summary if s["is_terminal"]],
        metrics=metrics,
        actor=actor,
    )


@admin_bp.route("/crm/pipeline/stage/<int:stage_id>", methods=["GET"])
def crm_pipeline_stage(stage_id):
    """Leads currently sitting in one sales stage.

    stage_id comes from the URL and is never trusted: get_stage() resolves it
    only within the acting tenant's 'sales' pipeline, so another tenant's id —
    or an AI-funnel stage id — returns None and 404s here rather than leaking
    a stage name or a lead list.
    """
    if not check_auth():
        return _deny()

    from app.services import sales_pipeline_service as sps

    _tid = _actor_tenant_id()
    if not _tid:
        return _deny()

    stage = sps.get_stage(_tid, stage_id)
    if stage is None:
        return _not_found(f"stage {stage_id}")

    actor = get_current_actor()
    page = max(1, request.args.get("page", 1, type=int))
    pagination = sps.get_stage_leads(_tid, stage_id, actor, page=page, per_page=25)

    return render_template(
        "crm_pipeline_stage.html",
        key=request.args.get("key", ""),
        stage=stage,
        leads=pagination.items if pagination else [],
        pagination=pagination,
        actor=actor,
    )


# ── Phase 6G: Audience Calculation Helper ──
def _calculate_audiences(tenant_id=None):
    from app.extensions import db
    from app.models import ConversationState, LeadEvent, ConversationMessage
    from sqlalchemy.sql import func
    
    all_states = tenant_filter(db.session.query(
        ConversationState.phone, 
        ConversationState.lead_score,
        ConversationState.updated_at,
        ConversationState.created_at,
        ConversationState.assigned_staff,
        ConversationState.is_admitted
    ), ConversationState, tenant_id).all()
    all_events = tenant_filter(db.session.query(LeadEvent.phone, LeadEvent.event_type, LeadEvent.created_at), LeadEvent, tenant_id).all()
    
    events_by_phone = {}
    latest_event_time = {}
    for e in all_events:
        events_by_phone.setdefault(e.phone, []).append(e)
        if e.phone not in latest_event_time or (e.created_at and e.created_at > latest_event_time[e.phone]):
            latest_event_time[e.phone] = e.created_at
            
    _subq_aud_base = tenant_filter(db.session.query(
        ConversationMessage.phone,
        func.max(ConversationMessage.id).label('max_id'),
        func.max(ConversationMessage.created_at).label('max_created')
    ), ConversationMessage, tenant_id)
    subq = _subq_aud_base.group_by(ConversationMessage.phone).subquery()
    
    latest_msgs = tenant_filter(db.session.query(
        ConversationMessage.phone, 
        ConversationMessage.direction,
        subq.c.max_created
    ), ConversationMessage, tenant_id).join(
        subq, ConversationMessage.id == subq.c.max_id
    ).all()
    
    needs_reply_phones = {r.phone for r in latest_msgs if r.direction == 'incoming'}
    latest_msg_time = {r.phone: r.max_created for r in latest_msgs}
    
    audiences = {
        "HOT Leads": set(),
        "WARM Leads": set(),
        "Demo Requested": set(),
        "Fees Requested": set(),
        "Placement Interested": set(),
        "Needs Reply": set(),
        "Critical Leads": set(),
        "All Leads": set()
    }
    
    for state in all_states:
        phone = state.phone
        events = events_by_phone.get(phone, [])
        intel = calculate_lead_intelligence(state.lead_score, events)
        health = calculate_lead_health(
            state.updated_at, state.created_at, 
            latest_msg_time.get(phone), latest_event_time.get(phone), 
            intel, phone in needs_reply_phones,
            assigned_staff=state.assigned_staff,
            is_admitted=state.is_admitted
        )
        
        audiences["All Leads"].add(phone)
        
        if intel["temperature"] == "HOT":
            audiences["HOT Leads"].add(phone)
        elif intel["temperature"] == "WARM":
            audiences["WARM Leads"].add(phone)
            
        events_set = intel.get("_events", [])
        if "DEMO_REQUESTED" in events_set:
            audiences["Demo Requested"].add(phone)
        if "FEES_REQUESTED" in events_set:
            audiences["Fees Requested"].add(phone)
        if "PLACEMENT_ASKED" in events_set:
            audiences["Placement Interested"].add(phone)
            
        if phone in needs_reply_phones:
            audiences["Needs Reply"].add(phone)
            
        if health["aging_status"] == "Critical":
            audiences["Critical Leads"].add(phone)
            
    return audiences


# ── Phase 4C: Lead Detail helpers ─────────────────────────────────────────

# ── Phase 9.2A-Lite: Staff Management ────────────────────────────────────────

@admin_bp.route("/crm/staff-management", methods=["GET", "POST"])
@admin_required
def crm_staff_management():
    if not check_auth():
        return _deny()
        
    # ── Phase RC2.2D Stage 2: WRITE path migrated to the User table ───────
    # Stage 1 moved this screen's READ to staff_service; this stage moves its
    # CRUD. app/data/staff_master.json is no longer read OR written here.
    #
    # Why the write had to follow the read: a file write is invisible to the
    # tenant-scoped read, so add/edit/toggle would have appeared to do nothing.
    # Worse, staff_master.json lives INSIDE the deployed image — Railway
    # replaces the filesystem on every deploy, so every edit made through this
    # screen was silently discarded at the next `git push`. The User table is
    # the only store on this screen that survives a deployment.
    #
    # _tenant is resolved ONCE and every operation is scoped to it, so a code
    # belonging to another tenant resolves to nothing and cannot be mutated.
    _tenant = _actor_tenant_id()

    if request.method == "POST":
        # Deferred, matching this module's convention for model/db imports.
        import secrets
        from sqlalchemy.exc import IntegrityError
        from werkzeug.security import generate_password_hash
        from app.extensions import db

        action = request.form.get("action")

        # ADR-021: never write without a resolved tenant. A SUPER_ADMIN who is
        # not impersonating has no tenant, and guessing one is how 18 lead_event
        # rows were mis-filed. Refuse rather than fall back.
        if action in ("add", "edit", "toggle") and not _tenant:
            return redirect(url_for("admin.crm_staff_management",
                                    err="No tenant context — cannot modify staff"))

        if action == "add":
            code = request.form.get("staff_code", "").strip().upper()
            display_name = request.form.get("display_name", "").strip()
            role = request.form.get("role", "STAFF").strip()
            active = request.form.get("active") == "on"

            if not code or not display_name:
                return redirect(url_for("admin.crm_staff_management", err="Code and Name required"))
            # Duplicate check now spans the tenant's real staff. Compared on the
            # DERIVED code, not raw username, so it matches exactly what the
            # table shows — and it is tenant-scoped, so two institutes may both
            # employ an 'ANJU' (production already has one username in four).
            if staff_service.resolve_code(_tenant, code, include_admins=True) is not None:
                return redirect(url_for("admin.crm_staff_management", err="Staff code already exists"))

            # Phase RC2.3E-2B: the DISPLAY NAME must be unique in the namespace
            # resolve() reads — username OR display_name — not just the code.
            #
            # The check above only ever spanned codes, so a free code plus an
            # already-taken name passed validation and produced a label that
            # resolve() reports as ambiguous. Production holds that pair
            # (NIBU/'nibu' and NIBU01/'nibu'), and every assignment to that
            # staff member is now rejected by the H3 validators.
            _clash = staff_service.display_name_conflict(_tenant, display_name)
            if _clash is not None:
                return redirect(url_for(
                    "admin.crm_staff_management",
                    err=f"'{display_name}' is already used by staff "
                        f"'{_clash.username}' — choose a different name"))

            from app.models import User
            # The staff_code IS the username: as_registry() derives the code as
            # username.upper(), so storing the code round-trips unchanged and
            # existing Oxford rows keep their existing codes.
            #
            # This screen creates a DIRECTORY ENTRY, not a login — exactly what
            # a staff_master.json row was. It collects no email and no password,
            # so the row gets a discarded random hash and no email: the account
            # cannot be authenticated into, and has no reset path, until an
            # admin provisions credentials via /tenant/staff. That is a
            # deliberate preservation of the old semantics, not an oversight.
            new_staff = User(
                username=code,
                display_name=display_name,
                email=None,
                password_hash=generate_password_hash(secrets.token_urlsafe(32)),
                role=role or "STAFF",
                tenant_id=_tenant,
                is_active=active,
                require_password_change=True,
            )
            try:
                db.session.add(new_staff)
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                return redirect(url_for("admin.crm_staff_management", err="Staff code already exists"))
            # Phase 0 Sprint 3: sovereign audit log (Constitution I.7)
            from app.services.audit_service import log_audit, request_ip
            log_audit("ROLE_CHANGE", actor=getattr(current_user, "email", None) or getattr(current_user, "username", None),
                      tenant_id=_actor_tenant_id(), target=f"staff:{code}",
                      detail={"event": "staff_added", "role": role}, ip=request_ip())
            return redirect(url_for("admin.crm_staff_management", msg="Staff added"))

        elif action == "edit":
            code = request.form.get("staff_code", "").strip().upper()
            staff = staff_service.resolve_code(_tenant, code, include_admins=True)
            if staff is None:
                return redirect(url_for("admin.crm_staff_management", err="Staff not found"))

            new_active = request.form.get("active") == "on"

            # Phase RC2.3E-6: a tenant must never reach zero active ADMINs.
            #
            # Reachable only because the registry above now lists admins: with
            # no row there was no edit modal and no toggle, so this screen
            # could not strip admin status at all. The fix that makes admins
            # visible is what makes them removable, which is why the guard
            # ships with it rather than after it.
            #
            # BLOCK_DEACTIVATION below does NOT cover this: it counts assigned
            # LEADS, and production's 'admin' (id=1) and 'NIBU' (id=18) own
            # zero, so they would pass it and leave the tenant unadministered.
            #
            # ONE guard for both transitions the edit form can perform —
            # demotion (role away from ADMIN) and deactivation — because they
            # are the same hazard reached through two fields of one submission,
            # and a form can do both at once. _new_role mirrors line 2043's
            # expression exactly so the guard tests the value that will
            # actually be written.
            _new_role = request.form.get("role", "").strip() or staff.role
            if (staff.role == "ADMIN" and staff.is_active
                    and (_new_role != "ADMIN" or not new_active)):
                if staff_service.active_admin_count(
                        _tenant, exclude_user_id=staff.id) == 0:
                    return redirect(url_for(
                        "admin.crm_staff_management",
                        err=f"'{staff.display_label()}' is this tenant's only "
                            f"active admin — promote another member to admin "
                            f"first"))

            if not new_active and staff.is_active:
                from app.models import ConversationState
                # Phase RC2.3E-1 Batch 3: the deactivation guard now resolves
                # ownership through the dual-read helper — the FIRST consumer
                # of owner_filter(). Under the flag it is an indexed FK
                # comparison; without it, lower(trim(col)) == label.
                #
                # This FIXES a live defect. The predicate here was
                # `assigned_staff == normalize_staff_name(...)`, which is
                # case-SENSITIVE against a title-cased name, so leads spelled
                # in another case were invisible to the guard. Production
                # currently holds both 'Kiran' and 'kiran' (and 'Anju'/'anju'):
                # the count read 24 where the true figure is 27. A staff member
                # whose leads were ALL lowercase would have counted 0 and been
                # deactivable while still owning live leads — precisely what
                # BLOCK_DEACTIVATION exists to prevent.
                #
                # _tenant is passed explicitly for consistency with
                # resolve_code() above and with the RC2.2F convention, NOT as a
                # correctness fix: tenant_query()'s SUPER_ADMIN branch returns
                # before it consults the argument, and for a normal admin
                # `tenant_id or current_user.tenant_id` resolves to the same
                # value. The two forms are equivalent in every reachable case.
                norm_name = normalize_staff_name(staff.display_label())
                leads_count = tenant_query(ConversationState, _tenant).filter(
                    staff_identity_service.owner_filter(
                        ConversationState, staff)).count()
                if leads_count > 0:
                    err_msg = f"BLOCK_DEACTIVATION:{leads_count}:{norm_name}"
                    return redirect(url_for("admin.crm_staff_management", err=err_msg))

            # Phase RC2.3E-2B: same check on the EDIT path, which validated
            # nothing at all — an admin could rename any staff member onto
            # any other's label. exclude_user_id skips this row, so keeping
            # your own name is not a collision with yourself.
            #
            # Only a NON-EMPTY submission is validated: the assignment below
            # falls back to the current label when the field is blank, so a
            # blank submission changes nothing and cannot introduce a clash.
            # That fallback is pre-existing behaviour and is left alone.
            _proposed = request.form.get("display_name", "").strip()
            if _proposed:
                _clash = staff_service.display_name_conflict(
                    _tenant, _proposed, exclude_user_id=staff.id)
                if _clash is not None:
                    return redirect(url_for(
                        "admin.crm_staff_management",
                        err=f"'{_proposed}' is already used by staff "
                            f"'{_clash.username}' — choose a different name"))

            _old_role = staff.role
            staff.display_name = request.form.get("display_name", "").strip() or staff.display_label()
            staff.role = request.form.get("role", "").strip() or staff.role
            staff.is_active = new_active

            db.session.commit()
            # Phase 0 Sprint 3: audit only actual role mutations
            if staff.role != _old_role:
                from app.services.audit_service import log_audit, request_ip
                log_audit("ROLE_CHANGE", actor=getattr(current_user, "email", None) or getattr(current_user, "username", None),
                          tenant_id=_actor_tenant_id(), target=f"staff:{code}",
                          detail={"event": "role_edited", "from": _old_role,
                                  "to": staff.role}, ip=request_ip())
            return redirect(url_for("admin.crm_staff_management", msg="Staff updated"))

        elif action == "toggle":
            code = request.form.get("staff_code", "").strip().upper()
            staff = staff_service.resolve_code(_tenant, code, include_admins=True)
            if staff is not None:
                new_active = not staff.is_active

                # Phase RC2.3E-6: the toggle reaches the same hazard as the
                # edit path and must not diverge from it — the third and last
                # way to strip admin status from this screen. Only
                # DEACTIVATION is guarded: reactivating an admin can only
                # raise the count.
                if (not new_active and staff.role == "ADMIN"
                        and staff.is_active):
                    if staff_service.active_admin_count(
                            _tenant, exclude_user_id=staff.id) == 0:
                        return redirect(url_for(
                            "admin.crm_staff_management",
                            err=f"'{staff.display_label()}' is this tenant's "
                                f"only active admin — promote another member "
                                f"to admin first"))

                if not new_active:
                    from app.models import ConversationState
                    # Phase RC2.3E-1 Batch 3: the toggle path reaches the same
                    # guard as the edit path above and must not diverge from
                    # it — the two were already an exact duplicate, and a fix
                    # applied to only one of them would be worse than neither.
                    norm_name = normalize_staff_name(staff.display_label())
                    leads_count = tenant_query(ConversationState, _tenant).filter(
                        staff_identity_service.owner_filter(
                            ConversationState, staff)).count()
                    if leads_count > 0:
                        err_msg = f"BLOCK_DEACTIVATION:{leads_count}:{norm_name}"
                        return redirect(url_for("admin.crm_staff_management", err=err_msg))

                staff.is_active = new_active
                db.session.commit()
                return redirect(url_for("admin.crm_staff_management", msg="Staff status toggled"))


    # Calculate statistics based on existing analytics logic
    analytics_data = calculate_admission_analytics()
    # analytics_data["staff_rows"] contains {"name": ..., "leads": ..., "admissions": ...}
    stats_map = {row["name"]: {"leads": row["leads"], "admissions": row["admissions"]} for row in analytics_data["staff_rows"]}

    # ── Phase RC2.2D Stage 1: READ path only ──────────────────────────────
    # This screen now renders the CURRENT TENANT's staff from the User table
    # instead of app/data/staff_master.json, which is a single GLOBAL file with
    # no tenant dimension — every tenant read the same rows, so a brand-new
    # institute saw Oxford's Anju/Kiran/Nisha (RC2.2 / RC2.3X).
    #
    # as_registry() returns load_staff_registry()'s exact shape, so the loop
    # below and the template are unchanged.
    #
    # The WRITE path above is deliberately untouched and still reads and saves
    # `registry` (the JSON file). The two authorities coexist for this stage:
    # Stage 2 has now migrated the writers above, so this screen no longer
    # touches the JSON file at all — for THIS screen it is fully retired.
    # It remains the store for the other 15 consumers until Stage 3.
    # Phase RC2.3E-6: the registry lists BOTH roles of this tenant.
    #
    # This read was the only asymmetric one on the screen: all three write
    # paths above already resolve with include_admins=True, so an ADMIN could
    # be mutated but never seen. Promoting a staff member through this very
    # screen therefore deleted them from it — production lost Anju (id=2) on
    # 2026-08-14, and with no row there is no edit modal, so the promotion
    # could not be undone either. The role is rendered in its own column, so
    # an admin appears AS an admin rather than as an extra staff member.
    #
    # It also removes a latent wrong-row write: codes were assigned here over
    # STAFF only while resolve_code() re-assigned them over STAFF+ADMIN, and
    # _assign_codes() gives the unsuffixed base to the LOWEST id. A username
    # collision across the two roles would therefore have displayed a code
    # that resolved to a different user.
    #
    # Deliberately the CALL SITE, not as_registry()'s default. The service
    # default stays False: it feeds the assignment dropdowns and the "Staff
    # Active" card, where an admin genuinely does not belong (RC2.2D I1), and
    # 37 compat tests pin that contract.
    display_registry = staff_service.as_registry(_tenant, include_admins=True)

    staff_list = []
    for code, data in display_registry.items():
        name = data.get("display_name", "")
        # The analytics normalize_staff_name(staff) resolves the name for grouping
        norm_name = normalize_staff_name(name)
        stats = stats_map.get(norm_name, {"leads": 0, "admissions": 0})
        
        staff_list.append({
            "code": code,
            "display_name": name,
            "role": data.get("role", "STAFF"),
            "active": data.get("active", False),
            "assigned_leads": stats["leads"],
            "admissions": stats["admissions"]
        })
        
    staff_list.sort(key=lambda x: (not x["active"], x["display_name"]))
    
    return render_template(
        "crm_staff_management.html",
        key=request.args.get("key", ""),
        staff_list=staff_list,
        msg=request.args.get("msg", ""),
        err=request.args.get("err", "")
    )


# ── GET /crm/leads ─────────────────────────────────────────────────────────

def _deny():
    return (
        "<html><body style='font-family:sans-serif;text-align:center;"
        "padding:50px;background:#0a0f0d;color:#25D366'>"
        "<h2>\U0001f512 Access Denied</h2>"
        "<p style='color:#888'>URL-il ?key=YOUR_ADMIN_KEY add cheyyuka</p>"
        "</body></html>"
    ), 403


def _not_found(phone):
    return (
        "<html><body style='font-family:sans-serif;text-align:center;"
        "padding:50px;background:#0a0f0d;color:#f85149'>"
        f"<h2>Lead not found</h2><p style='color:#888'>Phone: {phone}</p>"
        "</body></html>"
    ), 404


# ── GET /crm/lead/<phone> ──────────────────────────────────────────────────

@admin_bp.route("/crm/lead/<phone>", methods=["GET"])
def crm_lead_detail(phone):
    if not check_auth():
        return _deny()

    from app.models import ConversationState, MessageLog, ConversationMessage, LeadEvent
    from datetime import datetime, timedelta

    # Phase H4-a: _sps.get_stage_history() consumes this tenant directly.
    _tid = _actor_tenant_id()
    lead = tenant_query(ConversationState, _tid).filter_by(phone=phone).first()
    if lead is None:
        return _not_found(phone)

    actor = get_current_actor()
    is_staff = (actor.get("source") == "SESSION" and actor.get("role") == "STAFF")
    if is_staff:
        actor_username_normalized = (actor.get("username") or "").strip().lower()
        lead_staff_normalized = (lead.assigned_staff or "").strip().lower()
        if lead_staff_normalized != actor_username_normalized:
            return _deny()

    # ── Fetch message timeline (newest first, capped at 100) ──
    logs = (
        tenant_query(MessageLog, _tid)
        .filter_by(phone=phone)
        .order_by(MessageLog.created_at.desc())
        .limit(100)
        .all()
    )

    # ── Phase 5C: CRM Conversation Search & Filters ──
    search_q = request.args.get("search", "").strip()[:100]
    source_q = request.args.get("source", "all").strip().lower()
    range_q  = request.args.get("range", "all").strip().lower()

    # Note: For future scaling on large datasets, consider PostgreSQL full-text
    # search (tsvector) or pg_trgm (trigram indexing) for ConversationMessage.message
    query = tenant_query(ConversationMessage, _tid).filter_by(phone=phone)

    if search_q:
        query = query.filter(ConversationMessage.message.ilike(f"%{search_q}%"))

    if source_q and source_q != "all":
        query = query.filter_by(source=source_q)

    if range_q and range_q != "all":
        now = datetime.utcnow()
        if range_q == "today":
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            query = query.filter(ConversationMessage.created_at >= start_date)
        elif range_q == "7days":
            start_date = now - timedelta(days=7)
            query = query.filter(ConversationMessage.created_at >= start_date)
        elif range_q == "30days":
            start_date = now - timedelta(days=30)
            query = query.filter(ConversationMessage.created_at >= start_date)

    # ── Calculate Metrics (Accurate regardless of limit) ──
    total_msgs = query.count()
    metrics = {
        "total": total_msgs,
        "user": query.filter_by(source="user").count() if source_q == "all" else (total_msgs if source_q == "user" else 0),
        "ai": query.filter_by(source="ai").count() if source_q == "all" else (total_msgs if source_q == "ai" else 0),
        "manual": query.filter_by(source="manual").count() if source_q == "all" else (total_msgs if source_q == "manual" else 0)
    }

    # Query newest 100 DESC, then reverse() in Python so oldest renders first.
    # This guarantees latest messages are never dropped on large histories.
    timeline = list(reversed(
        query
        .order_by(ConversationMessage.created_at.desc())
        .limit(100)
        .all()
    ))

    # ── Phase 6A: Lead events (guarded — safe if migration not yet applied) ──
    try:
        events = (
            tenant_query(LeadEvent, _tid)
            .filter_by(phone=phone)
            .order_by(LeadEvent.created_at.asc())
            .all()
        )
    except Exception:
        events = []

    # ── Phase 6B & 6F: Intelligence and Health ──
    intelligence = calculate_lead_intelligence(lead.lead_score, events)
    
    latest_msg = tenant_query(ConversationMessage, _tid).filter_by(phone=phone).order_by(ConversationMessage.created_at.desc()).first()
    latest_msg_time = latest_msg.created_at if latest_msg else None
    latest_event_time = events[-1].created_at if events else None
    needs_reply = (latest_msg.direction == 'incoming') if latest_msg else False
    
    health = calculate_lead_health(
        lead.updated_at,
        lead.created_at,
        latest_msg_time,
        latest_event_time,
        intelligence,
        needs_reply,
        assigned_staff=lead.assigned_staff,
        is_admitted=lead.is_admitted
    )
    intelligence["_health"] = health

    # ── Phase 6E: Unified Timeline ──
    unified_timeline = []
    for e in events:
        unified_timeline.append({"type": "event", "created_at": e.created_at, "data": e})
    for m in timeline:
        unified_timeline.append({"type": "message", "created_at": m.created_at, "data": m})
    
    # Sort strictly by created_at ASC
    unified_timeline.sort(key=lambda x: x["created_at"])

    # ── Phase 7E: Course Journey (derived from event history) ───────────────
    course_journey = {
        "enquiries":  get_course_enquiries(phone),
        "admissions": get_course_admissions(phone),
    }

    # ── Phase 7F.3: Pre-parse JSON event_data for timeline display ───────────
    # Builds {event_id: course_name} for COURSE_ENQUIRY and COURSE_ADMISSION
    # events so the template never has to call json.loads.
    # Any malformed record is silently skipped — the template falls back to
    # ev.event_data (raw string) when an id is not in the map.
    import json as _json
    event_course_map: dict = {}
    event_payload_map: dict = {}
    for ev in events:
        if ev.event_type in ("COURSE_ENQUIRY", "COURSE_ADMISSION"):
            try:
                parsed = _json.loads(ev.event_data or "{}")
                name   = (parsed.get("course") or "").strip()
                if name:
                    event_course_map[ev.id] = normalize_course_name(name)
            except Exception:
                pass
            except Exception:
                pass
        elif ev.event_type in ("LEAD_REASSIGNED", "MANUAL_MESSAGE", "FOLLOW_UP_TASK", "FOLLOW_UP_COMPLETED"):
            try:
                event_payload_map[ev.id] = _json.loads(ev.event_data or "{}")
            except Exception:
                event_payload_map[ev.id] = {}

    # ── Phase 8.4: Lead Portfolio — zero new queries ─────────────────────────
    # Passes already-loaded lead ORM row, events list, and course_journey dict.
    # All aggregation happens in Python memory inside calculate_lead_portfolio().
    portfolio = calculate_lead_portfolio(lead, events, course_journey)

    # ── Phase 9.2A-Lite: Staff Registry ──────────────────────────────────────
    # Phase RC2.2D Batch 1: tenant-scoped picker. Both selects on this page
    # (lead owner and task assignee) read this list. A lead whose current owner
    # is NOT in the list still renders — the template keeps it as
    # "<name> (Inactive)" and selected — so migrating the list cannot silently
    # blank an existing owner on save.
    active_staff = staff_service.active_display_names(_actor_tenant_id())

    # ── Phase 9.3A: Task Summary ─────────────────────────────────────────────
    # Phase 16.5A7-B (B2): sourced from the Task table (System of Record) with
    # the pre-16.5A7 legacy replay as the compatibility layer. Previously this
    # replayed LeadEvents exclusively, so edits/deletes on this lead's tasks
    # never reached the summary. Task rows win; legacy fills the gap.
    task_summary = {"open": 0, "overdue": 0, "completed": 0}
    task_map = {}
    seen_uids = set()

    from app.models import Task as _Task
    for _t in tenant_query(_Task, _tid).filter(_Task.lead_phone == phone).all():
        seen_uids.add(_t.task_uid)
        task_map[_t.task_uid] = {
            "due_date": _t.due_date or "",
            "_completed": (_t.status == "COMPLETED"),
        }

    for ev in events:
        payload = event_payload_map.get(ev.id, {})
        tid = payload.get("task_id")
        if not tid or tid in seen_uids:
            continue                      # Task row is authoritative
        if ev.event_type == "FOLLOW_UP_TASK":
            task_map[tid] = dict(payload)
        elif ev.event_type == "FOLLOW_UP_COMPLETED":
            if tid in task_map:
                task_map[tid]["_completed"] = True

    today_dt = datetime.now()
    for tid, t in task_map.items():
        if t.get("_completed"):
            task_summary["completed"] += 1
        else:
            task_summary["open"] += 1
            due = t.get("due_date")
            if due:
                try:
                    due_dt = datetime.strptime(due, "%Y-%m-%d")
                    if (today_dt.date() - due_dt.date()).days > 0:
                        task_summary["overdue"] += 1
                except:
                    pass

    # Phase 10.2A: the status vocabulary is owned by models.LEAD_STATUSES, not
    # by the template. Passed as a list so the template can append the lead's
    # current value when it predates the vocabulary.
    from app.models import LEAD_STATUSES

    # Phase 10.8: sales stages for the move widget, plus this lead's movement
    # history. Both come from the service — the route holds no query logic.
    from app.services import sales_pipeline_service as _sps
    _pipeline_stages = _sps.get_pipeline_summary(_tid, get_current_actor())
    _stage_history = _sps.get_stage_history(_tid, lead.id)

    return render_template(
        "crm_lead_detail.html",
        lead_status_options=list(LEAD_STATUSES),
        pipeline_stages=_pipeline_stages,
        stage_history=_stage_history,
        lead=lead,
        logs=logs,
        timeline=timeline,
        unified_timeline=unified_timeline,
        metrics=metrics,
        search_q=search_q,
        source_q=source_q,
        range_q=range_q,
        msg=request.args.get("msg", ""),
        err=request.args.get("err", ""),
        events=events,
        intelligence=intelligence,
        course_journey=course_journey,
        event_course_map=event_course_map,
        portfolio=portfolio,
        active_staff=active_staff,
        event_payload_map=event_payload_map,
        task_summary=task_summary,
        task_map=task_map
    )



# ── POST /crm/lead/<phone>/update ──────────────────────────────────────────

@admin_bp.route("/crm/lead/<phone>/update", methods=["POST"])
def crm_lead_update(phone):
    if not check_auth():
        return _deny()

    from app.models import ConversationState
    from app.extensions import db

    # Phase H4-a: _actor_tenant_id() honours session['impersonate_tenant_id'].
    # The getattr form returns NULL for a SUPER_ADMIN, and _tid is consumed here
    # by resolve_assignment(), _sync_assigned_user(), transition_verdict(),
    # log_audit() and notification_service.notify() -- not only by
    # tenant_query(), whose SUPER_ADMIN branch ignores the argument entirely.
    # With _tid None, H3's validator resolved no staff and rejected EVERY edit
    # an impersonating SUPER_ADMIN made, blaming the staff member for it.
    _tid = _actor_tenant_id()
    lead = tenant_query(ConversationState, _tid).filter_by(phone=phone).first()
    if lead is None:
        return _not_found(phone)

    actor = get_current_actor()
    is_staff = (actor.get("source") == "SESSION" and actor.get("role") == "STAFF")
    if is_staff:
        actor_username_normalized = (actor.get("username") or "").strip().lower()
        lead_staff_normalized = (lead.assigned_staff or "").strip().lower()
        if lead_staff_normalized != actor_username_normalized:
            return _deny()

    import urllib.parse
    qs = ""
    if request.args.get("search"): qs += f"&search={urllib.parse.quote(request.args.get('search'))}"
    if request.args.get("source"): qs += f"&source={urllib.parse.quote(request.args.get('source'))}"
    if request.args.get("range"):  qs += f"&range={urllib.parse.quote(request.args.get('range'))}"

    try:
        old_staff = lead.assigned_staff
        # Phase 10.2A: snapshot every audited field BEFORE mutation. Read off
        # the instance rather than re-querying — after assignment the ORM has
        # already overwritten the attribute, so the previous value is otherwise
        # unrecoverable for the audit record.
        old_status   = lead.lead_status
        old_score    = lead.lead_score
        old_admitted = lead.is_admitted
        old_notes    = lead.notes
        # Phase 10.8: the sales stage link, captured before the adapter setter
        # re-resolves it. Read from the column, not lead.lead_status, so this
        # is the id the lead actually sat on.
        old_sales_stage_id = lead.sales_stage_id

        # Phase 10.9A: validated against LEAD_STATUSES. Defaulting to the
        # CURRENT value means a blank or unrecognised submission leaves the
        # lead untouched rather than writing an unknown status — which would
        # clear sales_stage_id and drop the lead out of the Sales Pipeline.
        _submitted_status = request.form.get("lead_status")
        # Resolve with no default first, so a REJECTED value is distinguishable
        # from a legitimate case variant ("enrolled" -> "Enrolled", which is
        # accepted and must not warn).
        _resolved_status = canonical_lead_status(_submitted_status)
        if (_submitted_status or "").strip() and _resolved_status is None:
            # Unreachable from the dropdown; only a crafted POST or a bug gets
            # here, so it is logged rather than silently discarded.
            logging.warning(
                "Rejected lead_status %r for lead %s — not in LEAD_STATUSES; kept %r",
                _submitted_status, phone, lead.lead_status)
        # Phase 10.9B.2: warn-only. Asked BEFORE the write, so the engine sees
        # the transition the operator actually proposed. `allowed` is ignored —
        # nothing is blocked in this phase.
        _target_status = _resolved_status or lead.lead_status
        _transition_context = _sts_mod.CONTEXT_OPERATOR_FORM
        _transition = transition_verdict(_tid, old_status, _target_status,
                                         _transition_context)
        lead.lead_status    = _target_status
        lead.notes          = request.form.get("notes",          "").strip() or None

        if not is_staff:
            # ── Phase H3-1B-a: reject an owner who is not this tenant's staff
            #
            # Rolled back before returning: lead_status and notes were already
            # assigned above, so returning without this would leave the session
            # holding a partial edit — the same reason the admission hard-block
            # below rolls back. Rejecting must change nothing.
            _owner = staff_identity_service.resolve_assignment(
                _tid, request.form.get("assigned_staff", ""))
            if not _owner.ok:
                db.session.rollback()
                return redirect(url_for(
                    "admin.crm_lead_detail", phone=phone,
                    err=f"'{_owner.value}' is not a current staff member of "
                        f"this institute — choose from the Assigned Staff list."))
            # `.value`, not `.canonical` — store the operator's own spelling,
            # exactly as before.
            lead.assigned_staff = _owner.value
            # Phase RC2.3D: mirror into assigned_user_id (no-op while the flag
            # is OFF). Placed immediately after the legacy write so the two
            # cannot diverge.
            _sync_assigned_user(lead, _tid)
            score_raw = request.form.get("lead_score", "").strip()
            if score_raw.isdigit():
                lead.lead_score = max(0, min(100, int(score_raw)))
            new_admitted  = (request.form.get("is_admitted") == "1")
        else:
            new_admitted = lead.is_admitted

        # ── Snapshot values before commit for post-commit event firing ──
        new_course    = (lead.course or "").strip()
        new_staff     = lead.assigned_staff

        # ── Phase 8.2 Gap 2: Hard block — admission requires assigned staff ──────
        if new_admitted and not (lead.assigned_staff or "").strip():
            db.session.rollback()
            return redirect(url_for(
                "admin.crm_lead_detail", phone=phone,
                err="Admission+blocked%3A+please+assign+a+staff+member+before+marking+this+lead+as+admitted."
            ))

        # ── Phase 8.2 Gap 3: Auto-promote lead_status → Enrolled on admission ────
        _PROMOTE_STATUSES = {"Lead", "Contacted", "Interested"}
        if new_admitted and (lead.lead_status or "").strip() in _PROMOTE_STATUSES:
            # Phase 10.9B.2: the promotion is a SECOND, distinct transition —
            # the system's, not the operator's — so it gets its own verdict
            # under AUTO_ADMISSION rather than inheriting the form's. Asked
            # before the write, and it replaces the earlier verdict because the
            # audit detail below reports the lead's FINAL status.
            _transition_context = _sts_mod.CONTEXT_AUTO_ADMISSION
            _transition = transition_verdict(_tid, lead.lead_status, "Enrolled",
                                             _transition_context)
            lead.lead_status = "Enrolled"

        lead.is_admitted = new_admitted

        db.session.commit()

        # ── Phase 10.2A: sovereign audit trail, AFTER the business commit ──
        # log_audit() commits the session, so it must never run between a
        # mutation and its commit. Placed here, the lead write is already
        # durable and each audit row is its own small transaction. log_audit()
        # never raises, so a failed audit cannot undo a successful edit — it is
        # logged loudly instead.
        #
        # Emitted per changed field rather than one blanket LEAD_UPDATE: the
        # questions this log exists to answer ("who changed the score", "who
        # reassigned this") are field-specific, and a single row carrying a
        # whole form is far harder to query.
        from app.services.audit_service import log_audit, request_ip
        _audit_actor = getattr(current_user, "email", None) or _actor_name()
        _audit_ip     = request_ip()
        _audit_target = f"lead:{phone}"

        def _audit(action, detail):
            log_audit(action, actor=_audit_actor, tenant_id=_tid,
                      target=_audit_target, detail=detail, ip=_audit_ip)

        if old_staff != lead.assigned_staff:
            _audit("LEAD_ASSIGN", {"from": old_staff or "", "to": lead.assigned_staff or ""})
        if old_status != lead.lead_status:
            # Phase 10.8: record the movement (history + timeline + notify),
            # then enrich the existing audit detail with the stage ids. The
            # audit row stays the single security record — record_stage_change
            # deliberately does not write one.
            #
            # Admission auto-promotion is excluded from notification per the
            # approved policy: when is_admitted flips, the status change to
            # "Enrolled" was made by the system, not chosen by the operator.
            from app.services import sales_pipeline_service as _sps
            _auto_promoted = (old_admitted != lead.is_admitted) and bool(lead.is_admitted)
            _moved = _sps.record_stage_change(
                _tid, lead, old_sales_stage_id, old_status,
                actor=(_sps.ACTOR_AUTO_ADMISSION if _auto_promoted else _audit_actor),
                notify=not _auto_promoted,
                notify_actor_name=_actor_name(),
            )
            _detail = {"from": old_status or "", "to": lead.lead_status or ""}
            if _moved:
                _detail.update(_moved)      # from_stage_id / to_stage_id
            # Phase 10.9B.2: warn-only verdict, nested under its own key so it
            # extends this detail rather than colliding with from/to. No new
            # audit action — VALID_ACTIONS is untouched.
            _detail.update(transition_detail(_transition, _transition_context))
            _audit("LEAD_STATUS_CHANGE", _detail)
        if old_score != lead.lead_score:
            _audit("LEAD_SCORE_CHANGE", {"from": old_score, "to": lead.lead_score})
        if old_admitted != lead.is_admitted:
            _audit("LEAD_ADMISSION", {"from": bool(old_admitted), "to": bool(lead.is_admitted),
                                      "staff": lead.assigned_staff or ""})
        # Notes are free text and may contain personal detail about a student,
        # so the audit records THAT they changed, never their content — the
        # service contract forbids logging message bodies.
        if old_notes != lead.notes:
            _audit("LEAD_UPDATE", {"field": "notes", "changed": True})

        # ── Phase 7E & 9.1: Fire events AFTER successful commit ──────────
        import json
        from app.services.log_service import log_lead_event
        from app.models import LeadEvent

        # Phase 9.1: LEAD_REASSIGNED accountability audit
        if old_staff != new_staff:
            _actor_display = _actor_name()
            # Phase 16.5A7 (ADR-021): tenant_id is the ACTOR's tenant. This
            # previously used _get_default_tenant_id() (Tenant.query.first()),
            # which mis-filed the audit row under an unrelated tenant.
            log_lead_event(tenant_id=_tid,
                phone=phone,
                event_type="LEAD_REASSIGNED",
                event_data=json.dumps({
                    "from": old_staff or "",
                    "to": new_staff or "",
                    "by": _actor_display
                })
            )

            # ── Phase 16.5A7: lead assignment notifications (ADR-021) ──────
            # First assignment reads as "new lead"; a handover reads as
            # "reassigned" and also informs the staff member losing the lead.
            if _tid:
                from app.services import notification_service
                from app.models import Notification as _Notif
                _lead_label = (lead.name or "").strip() or phone
                if new_staff:
                    _is_new = not (old_staff or "").strip()
                    notification_service.notify(
                        tenant_id=_tid, recipient=new_staff,
                        notif_type=(_Notif.TYPE_NEW_LEAD_ASSIGNED if _is_new
                                    else _Notif.TYPE_LEAD_REASSIGNED),
                        title=("New lead assigned: " if _is_new
                               else "Lead reassigned to you: ") + _lead_label,
                        body=f"Assigned by {_actor_display}",
                        lead_phone=phone)
                if (old_staff or "").strip():
                    notification_service.notify(
                        tenant_id=_tid, recipient=old_staff,
                        notif_type=_Notif.TYPE_LEAD_REASSIGNED,
                        title=f"Lead reassigned away: {_lead_label}",
                        body=f"Now assigned to {new_staff or 'Unassigned'}",
                        lead_phone=phone)

        # COURSE_ENQUIRY — fire once per unique course name.
        if new_course:
            existing_enquiry = tenant_query(LeadEvent, _tid).filter_by(
                phone=phone, event_type="COURSE_ENQUIRY"
            ).all()
            already_logged = {
                (json.loads(e.event_data or "{}").get("course") or "").strip().lower()
                for e in existing_enquiry
                if e.event_data
            }
            if new_course.lower() not in already_logged:
                log_lead_event(tenant_id=_tid,
                    phone=phone,
                    event_type="COURSE_ENQUIRY",
                    event_data=json.dumps({"course": new_course}),
                )

        # COURSE_ADMISSION — fire once per unique admitted course name.
        if new_admitted and new_course:
            existing_admission = tenant_query(LeadEvent, _tid).filter_by(
                phone=phone, event_type="COURSE_ADMISSION"
            ).all()
            already_admitted = {
                (json.loads(e.event_data or "{}").get("course") or "").strip().lower()
                for e in existing_admission
                if e.event_data
            }
            if new_course.lower() not in already_admitted:
                log_lead_event(tenant_id=_tid,
                    phone=phone,
                    event_type="COURSE_ADMISSION",
                    event_data=json.dumps({
                        "course": new_course,
                        "staff": new_staff or ""
                    }),
                )

    except Exception as e:
        db.session.rollback()

    return redirect(url_for("admin.crm_lead_detail", phone=phone))

# ── Phase 6G: Campaigns ──
@admin_bp.route("/crm/campaigns", methods=["GET"])
def campaigns():
    if not check_auth():
        return _deny()
    
    from datetime import date
    from app.models import ConversationMessage
    from app.extensions import db
    
    today = date.today()
    # Phase H4-b: consistency. _tid here feeds ONLY tenant_query(), whose
    # SUPER_ADMIN branch returns before reading the argument, so this
    # changes no behaviour -- it makes all four/seven routes resolve the
    # tenant by one rule.
    _tid = _actor_tenant_id()
    campaign_msgs = tenant_query(ConversationMessage, _tid).filter(
        ConversationMessage.source == 'campaign',
        db.func.date(ConversationMessage.created_at) == today
    ).all()
    
    messages_sent_today = len(campaign_msgs)
    campaign_names = set()
    last_campaign_name = "None"
    last_campaign_time = None
    
    for m in campaign_msgs:
        import re
        match = re.search(r"\[CAMPAIGN:\s*(.*?)\]", m.message or "")
        if match:
            cname = match.group(1).strip()
            campaign_names.add(cname)
            if last_campaign_time is None or (m.created_at and m.created_at > last_campaign_time):
                last_campaign_time = m.created_at
                last_campaign_name = cname
                
    dashboard = {
        "campaigns_today": len(campaign_names),
        "messages_today": messages_sent_today,
        "last_campaign_name": last_campaign_name,
        "last_campaign_time": last_campaign_time.strftime("%H:%M") if last_campaign_time else "—"
    }
    
    return render_template("campaigns.html", dashboard=dashboard)


@admin_bp.route("/crm/campaigns/center", methods=["GET"])
def campaigns_center():
    """Phase 9.1b: Campaign Center (V2) — the operator surface for Campaign
    Engine V2.

    Serves a shell only. Every campaign operation is performed by the browser
    against the existing /crm/campaigns/v2 JSON API — this view issues no
    campaign queries and holds no campaign logic, so the ADR-023/024/025
    contracts stay enforced server-side exactly as tested. No V2 backend
    behaviour is changed by this phase.

    The legacy /crm/campaigns builder (V1) is deliberately left intact and
    reachable; retiring it is a separate phase with its own approval.

    Route naming: /crm/campaigns/center cannot collide with the marketing
    blueprint, which is mounted at the /crm/campaigns/v2 prefix.
    """
    if not check_auth():
        return _deny()

    return render_template("campaigns_center.html")


@admin_bp.route("/crm/campaigns/history", methods=["GET"])
def campaigns_history():
    """Phase 9.2: Campaign History (V2) — past campaigns for this tenant.

    Shell only, exactly like campaigns_center: the browser reads everything
    from the existing /crm/campaigns/v2 API, so this view issues no campaign
    queries and introduces no new backend contract. Read-only apart from the
    archive action, which reuses the existing lifecycle endpoint.

    Route naming: /crm/campaigns/history cannot collide with the marketing
    blueprint, which is mounted at the /crm/campaigns/v2 prefix.
    """
    if not check_auth():
        return _deny()

    return render_template("campaigns_history.html")


@admin_bp.route("/crm/campaigns/details/<int:campaign_id>", methods=["GET"])
def campaign_details(campaign_id):
    """Phase 9.3: Campaign Details (V2) — dedicated single-campaign inspection
    surface.

    Shell only, exactly like campaigns_center/campaigns_history: the browser
    reads everything from the existing /crm/campaigns/v2 API
    (GET /<id>, GET /<id>/progress) and reuses the existing lifecycle actions
    (cancel, archive). This view issues no campaign queries and introduces
    no new backend contract.

    Route naming: /crm/campaigns/details/<id> cannot collide with the
    marketing blueprint, which is mounted at the /crm/campaigns/v2 prefix.
    """
    if not check_auth():
        return _deny()

    return render_template("campaign_details.html", campaign_id=campaign_id)


@admin_bp.route("/crm/campaigns/preview", methods=["POST"])
@admin_required
def campaign_preview():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 403
        
    data = request.get_json()
    audience_type = data.get("audience", "")
    
    audiences = _calculate_audiences()
    phones = list(audiences.get(audience_type, set()))
    
    return jsonify({
        "count": len(phones),
        "examples": phones[:3],
        "estimated_duration_seconds": int(len(phones) * 1.5)
    })

@admin_bp.route("/crm/campaigns/send", methods=["POST"])
@admin_required
def campaign_send():
    if not check_auth():
        return _deny()
        
    name = request.form.get("campaign_name", "").strip()
    audience_type = request.form.get("audience", "").strip()
    message = request.form.get("message", "").strip()
    
    if not name or not message:
        flash("Campaign name and message are required.", "danger")
        return redirect(url_for("admin.campaigns"))
        
    audiences = _calculate_audiences()
    phones = list(audiences.get(audience_type, set()))
    
    if len(phones) == 0:
        flash(f"Audience '{audience_type}' has 0 leads. Campaign aborted.", "warning")
        return redirect(url_for("admin.campaigns"))
        
    if len(phones) > 100:
        flash("Campaigns are limited to 100 recipients max. Please split large batches.", "danger")
        return redirect(url_for("admin.campaigns"))
        
    from app.services.campaign_service import start_campaign
    try:
        start_campaign(phones, message, name, tenant_id=_actor_tenant_id())
        flash(f"Campaign '{name}' started successfully. Sending to {len(phones)} leads. Check dashboard later for results.", "success")
    except Exception as e:
        flash(f"Failed to start campaign: {str(e)}", "danger")
        
    return redirect(url_for("admin.campaigns"))


# ── POST /crm/lead/<phone>/send ────────────────────────────────────────────

@admin_bp.route("/crm/lead/<phone>/send", methods=["POST"])
def crm_lead_send(phone):
    if not check_auth():
        return _deny()

    from app.models import ConversationState
    # Phase H4-a: log_message() and save_conversation_message() consume this
    # tenant directly and do NOT guard against None, so the getattr form could
    # attribute an impersonating SUPER_ADMIN's message to no tenant at all.
    _tid = _actor_tenant_id()
    lead = tenant_query(ConversationState, _tid).filter_by(phone=phone).first()
    if lead is None:
        return _not_found(phone)
        
    actor = get_current_actor()
    is_staff = (actor.get("source") == "SESSION" and actor.get("role") == "STAFF")
    if is_staff:
        actor_username_normalized = (actor.get("username") or "").strip().lower()
        lead_staff_normalized = (lead.assigned_staff or "").strip().lower()
        if lead_staff_normalized != actor_username_normalized:
            return _deny()

    message = request.form.get("manual_message", "").strip()

    if not message:
        return redirect(f"/crm/lead/{phone}?err=Message+cannot+be+empty")

    import urllib.parse
    qs = ""
    if request.args.get("search"): qs += f"&search={urllib.parse.quote(request.args.get('search'))}"
    if request.args.get("source"): qs += f"&source={urllib.parse.quote(request.args.get('source'))}"
    if request.args.get("range"):  qs += f"&range={urllib.parse.quote(request.args.get('range'))}"

    try:
        r = send_text(phone, message)
        if r.status_code == 200:
            # ── Log manual outbound message (MessageLog — raw technical log) ──
            from app.services.log_service import log_message
            log_message(tenant_id=_tid,
                phone=phone,
                direction="outbound",
                message_type="manual",
                message_text=message,
            )
            # ── Persist manual send to ConversationMessage (CRM timeline) ──
            # Phase 10N-G Fix 1: Use authenticated actor identity, not lead owner.
            # actor is already resolved at line 1323 — no second DB query needed.
            from app.services.log_service import save_conversation_message, log_lead_event
            import json

            sender_name = actor.get("username") or "Admin"

            save_conversation_message(tenant_id=_tid,
                phone=phone,
                direction="outgoing",
                message=message,
                message_type="text",
                source="manual",
                staff_name=sender_name,
                wa_message_id=None,
            )

            # Phase 9.1: MESSAGE_OWNER audit using LeadEvent
            log_lead_event(tenant_id=_tid,
                phone=phone,
                event_type="MANUAL_MESSAGE",
                event_data=json.dumps({"staff": sender_name})
            )
            # Phase 10.2A: sovereign audit record of an outbound message to a
            # customer. Records sender and length only — never the body, which
            # the audit service contract explicitly forbids.
            from app.services.audit_service import log_audit, request_ip
            log_audit("LEAD_MESSAGE_SENT",
                      actor=getattr(current_user, "email", None) or sender_name,
                      tenant_id=_tid, target=f"lead:{phone}",
                      detail={"channel": "whatsapp", "length": len(message)},
                      ip=request_ip())
            return redirect(f"/crm/lead/{phone}?msg=Message+sent+successfully{qs}")

        else:
            return redirect(f"/crm/lead/{phone}?err=WhatsApp+API+returned+an+error{qs}")
    except Exception:
        logging.exception(f"Manual WhatsApp send failed for {phone}")
        return redirect(f"/crm/lead/{phone}?err=Unexpected+server+error{qs}")



# ── Phase 7A: Funnel Analytics ──

def calculate_funnel_metrics(tenant_id=None):
    from app.models import LeadEvent, ConversationState
    from app.extensions import db

    # Bulk queries (Max 1 LeadEvent, 1 ConversationState)
    events = tenant_filter(db.session.query(LeadEvent.phone, LeadEvent.event_type), LeadEvent, tenant_id).all()
    states = tenant_filter(db.session.query(ConversationState.phone, ConversationState.is_admitted), ConversationState, tenant_id).all()

    stages = {
        "LEAD_CREATED": set(),
        "COURSE_VIEWED": set(),
        "FEES_REQUESTED": set(),
        "DEMO_REQUESTED": set(),
        "PAYMENT_PENDING": set()
    }

    # Deduplicate events per phone
    for phone, event_type in events:
        if event_type in stages:
            stages[event_type].add(phone)

    admitted_phones = set()
    total_leads_phones = set()

    for phone, is_admitted in states:
        total_leads_phones.add(phone)
        if is_admitted:
            admitted_phones.add(phone)

    # Some leads might have LEAD_CREATED event but not be in ConversationState if DB is out of sync,
    # but the prompt states "Stage 1: LEAD_CREATED" and "100 Leads". We will use ConversationState for Total Leads
    # to be completely accurate for "Admissions" vs "Total Leads".
    # Wait, the rule says "A lead contributes only once per stage."
    
    # Calculate counts
    total_leads = len(total_leads_phones)
    c_created = len(stages["LEAD_CREATED"]) 
    # For safety, if LEAD_CREATED event wasn't fired historically, total_leads is more reliable for Stage 1. 
    # But prompt explicitly says: "Stage 1: LEAD_CREATED"
    c_created = max(c_created, total_leads) # Fallback if events are missing but states exist

    c_course = len(stages["COURSE_VIEWED"])
    c_fees = len(stages["FEES_REQUESTED"])
    c_demo = len(stages["DEMO_REQUESTED"])
    c_payment = len(stages["PAYMENT_PENDING"])
    c_admitted = len(admitted_phones)

    metrics = {
        "total_leads": total_leads,
        "course_viewed": c_course,
        "fees_requested": c_fees,
        "demo_requested": c_demo,
        "payment_pending": c_payment,
        "admitted": c_admitted
    }

    # Funnel sequence
    sequence = [
        ("Lead Created", c_created),
        ("Course Viewed", c_course),
        ("Fees Requested", c_fees),
        ("Demo Requested", c_demo),
        ("Payment Pending", c_payment),
        ("Admitted", c_admitted)
    ]

    funnel = []
    prev_count = c_created
    bottleneck = {"stage1": "", "stage2": "", "drop": -1, "drop_pct": 0}

    for name, count in sequence:
        pct = (count / prev_count * 100) if prev_count > 0 else 0
        drop = prev_count - count
        
        if name != "Lead Created":
            if drop > bottleneck["drop"]:
                bottleneck = {
                    "stage1": funnel[-1]["name"],
                    "stage2": name,
                    "drop": drop,
                    "drop_pct": round((100 - pct), 1) if prev_count > 0 else 0
                }
                
        funnel.append({
            "name": name,
            "count": count,
            "percentage": round(pct, 1) if name != "Lead Created" else 100.0
        })
        prev_count = count

    return {
        "metrics": metrics,
        "funnel": funnel,
        "bottleneck": bottleneck,
        "conversion_rate": round((c_admitted / total_leads * 100) if total_leads > 0 else 0, 1)
    }

@admin_bp.route("/crm/analytics", methods=["GET"])
def crm_analytics():
    if not check_auth():
        return _deny()

    data = calculate_funnel_metrics()
    
    return render_template(
        "crm_analytics.html",
        data=data
    )




# ── Phase 7B: Staff Performance ──

def calculate_staff_performance(tenant_id=None):
    from app.models import ConversationState, LeadEvent, ConversationMessage
    try:
        from app.models import FollowUpJob
    except ImportError:
        FollowUpJob = None
    from app.extensions import db

    # Bulk queries (Max 1 each)
    states = tenant_filter(db.session.query(
        ConversationState.phone,
        ConversationState.assigned_staff,
        ConversationState.is_admitted,
        ConversationState.lead_score
    ), ConversationState, tenant_id).all()
    
    events = tenant_filter(db.session.query(
        LeadEvent.phone, 
        LeadEvent.event_type
    ), LeadEvent, tenant_id).all()
    
    msgs = tenant_filter(db.session.query(
        ConversationMessage.phone,
        ConversationMessage.direction,
        ConversationMessage.created_at
    ), ConversationMessage, tenant_id).all()

    # FollowUpJob query for "Follow-up due count"
    pending_fu_phones = set()
    if FollowUpJob:
        pending_jobs = tenant_filter(db.session.query(FollowUpJob.phone), FollowUpJob, tenant_id).filter_by(done=False).all()
        pending_fu_phones = {j.phone for j in pending_jobs}

    events_by_phone = {}
    for p, et in events:
        events_by_phone.setdefault(p, set()).add(et)
        
    latest_msg = {}
    for p, d, c in msgs:
        if p not in latest_msg or c > latest_msg[p][1]:
            latest_msg[p] = (d, c)
            
    needs_reply_phones = {p for p, (d, c) in latest_msg.items() if d == 'incoming'}

    staff_stats = {}
    total_staff = set()
    total_assigned_leads = 0
    total_admissions = 0
    
    for phone, assigned_staff, is_admitted, lead_score in states:
        staff = normalize_staff_name(assigned_staff)
        if staff == "Unassigned":
            continue
            
        total_staff.add(staff)
        
        if staff not in staff_stats:
            staff_stats[staff] = {
                "assigned_leads": 0,
                "admissions": 0,
                "total_score": 0,
                "hot_leads": 0,
                "warm_leads": 0,
                "cold_leads": 0,
                "needs_reply": 0,
                "follow_up_due": 0
            }
            
        st = staff_stats[staff]
        st["assigned_leads"] += 1
        total_assigned_leads += 1
        
        if is_admitted:
            st["admissions"] += 1
            total_admissions += 1
            
        st["total_score"] += (lead_score or 0)
        
        unique_event_types = events_by_phone.get(phone, set())
        auto_score = sum(EVENT_SCORE_MAP.get(et, 0) for et in unique_event_types)
        final_score = min((lead_score or 0) + auto_score, 100)
        
        if final_score >= 80:
            st["hot_leads"] += 1
        elif final_score >= 50:
            st["warm_leads"] += 1
        else:
            st["cold_leads"] += 1
            
        if phone in needs_reply_phones:
            st["needs_reply"] += 1
            
        if phone in pending_fu_phones:
            st["follow_up_due"] += 1

    leaderboard = []
    for staff, data in staff_stats.items():
        assigned = data["assigned_leads"]
        adm = data["admissions"]
        conversion = round((adm / assigned * 100) if assigned > 0 else 0, 1)
        
        # Calculate Average Lead Score properly. 
        # Lead score conceptually applies to the whole pipeline. 
        # Wait, the total_score right now only aggregates manual score. Auto score needs to be included.
        # Let's fix that below.
        pass
        
    # Re-evaluating average score to include auto score
    for staff, data in staff_stats.items():
        pass
        
    leaderboard = []
    for staff, data in staff_stats.items():
        assigned = data["assigned_leads"]
        adm = data["admissions"]
        conversion = round((adm / assigned * 100) if assigned > 0 else 0, 1)
        
        # Avg Lead score needs to be derived. 
        # But wait, final_score is per lead. I need to aggregate it.
        # Let's keep a running sum of final_score in st["total_final_score"]
        pass

    return staff_stats, total_staff, total_assigned_leads, total_admissions

def calculate_staff_performance_fixed(tenant_id=None):
    from app.models import ConversationState, LeadEvent, ConversationMessage
    try:
        from app.models import FollowUpJob
    except ImportError:
        FollowUpJob = None
    from app.extensions import db

    # Bulk queries
    states = tenant_filter(db.session.query(
        ConversationState.phone,
        ConversationState.assigned_staff,
        ConversationState.is_admitted,
        ConversationState.lead_score
    ), ConversationState, tenant_id).all()
    
    events = tenant_filter(db.session.query(LeadEvent.phone, LeadEvent.event_type), LeadEvent, tenant_id).all()
    
    msgs = tenant_filter(db.session.query(
        ConversationMessage.phone,
        ConversationMessage.direction,
        ConversationMessage.created_at
    ), ConversationMessage, tenant_id).all()

    pending_fu_phones = set()
    if FollowUpJob:
        pending_jobs = tenant_filter(db.session.query(FollowUpJob.phone), FollowUpJob, tenant_id).filter_by(done=False).all()
        pending_fu_phones = {j.phone for j in pending_jobs}

    events_by_phone = {}
    for p, et in events:
        events_by_phone.setdefault(p, set()).add(et)
        
    latest_msg = {}
    for p, d, c in msgs:
        if p not in latest_msg or c > latest_msg[p][1]:
            latest_msg[p] = (d, c)
            
    needs_reply_phones = {p for p, (d, c) in latest_msg.items() if d == 'incoming'}

    staff_stats = {}
    total_staff = set()
    total_assigned_leads = 0
    total_admissions = 0
    
    for phone, assigned_staff, is_admitted, lead_score in states:
        staff = normalize_staff_name(assigned_staff)
        if staff == "Unassigned":
            continue
            
        total_staff.add(staff)
        
        if staff not in staff_stats:
            staff_stats[staff] = {
                "assigned_leads": 0,
                "admissions": 0,
                "total_final_score": 0,
                "hot_leads": 0,
                "warm_leads": 0,
                "cold_leads": 0,
                "needs_reply": 0,
                "follow_up_due": 0
            }
            
        st = staff_stats[staff]
        st["assigned_leads"] += 1
        total_assigned_leads += 1
        
        if is_admitted:
            st["admissions"] += 1
            total_admissions += 1
            
        unique_event_types = events_by_phone.get(phone, set())
        auto_score = sum(EVENT_SCORE_MAP.get(et, 0) for et in unique_event_types)
        final_score = min((lead_score or 0) + auto_score, 100)
        
        st["total_final_score"] += final_score
        
        if final_score >= 80:
            st["hot_leads"] += 1
        elif final_score >= 50:
            st["warm_leads"] += 1
        else:
            st["cold_leads"] += 1
            
        if phone in needs_reply_phones:
            st["needs_reply"] += 1
            
        if phone in pending_fu_phones:
            st["follow_up_due"] += 1

    leaderboard = []
    for staff, data in staff_stats.items():
        assigned = data["assigned_leads"]
        adm = data["admissions"]
        conversion = round((adm / assigned * 100) if assigned > 0 else 0, 1)
        avg_score = round((data["total_final_score"] / assigned) if assigned > 0 else 0, 1)
        
        leaderboard.append({
            "name": staff,
            "assigned_leads": assigned,
            "admissions": adm,
            "conversion": conversion,
            "avg_score": avg_score,
            "hot_leads": data["hot_leads"],
            "warm_leads": data["warm_leads"],
            "cold_leads": data["cold_leads"],
            "needs_reply": data["needs_reply"],
            "follow_up_due": data["follow_up_due"]
        })
        
    leaderboard.sort(key=lambda x: (x["admissions"], x["conversion"]), reverse=True)
    
    overall_conversion = round((total_admissions / total_assigned_leads * 100) if total_assigned_leads > 0 else 0, 1)
    
    team_summary = {
        "total_staff": len(total_staff),
        "total_assigned_leads": total_assigned_leads,
        "total_admissions": total_admissions,
        "overall_conversion": overall_conversion
    }
    
    return {
        "leaderboard": leaderboard,
        "team_summary": team_summary
    }

@admin_bp.route("/crm/staff-performance", methods=["GET"])
def crm_staff_performance():
    if not check_auth():
        return _deny()

    data = calculate_staff_performance_fixed()
    
    return render_template(
        "crm_staff_performance.html",
        key=request.args.get("key", ""),
        data=data
    )


# ── Phase 7C: Source Analytics ─────────────────────────────────────────────

# Website-indicator keywords checked against the first message text.
_WEBSITE_KEYWORDS = (
    "utm_", "landing", "webform",
    "course-details",
    "theoxfordedu.com",
)

def calculate_source_analytics(tenant_id=None):
    """
    Read-only lead source attribution analytics.

    Strategy:
    - Bulk query 1: ConversationState  (phone, is_admitted)
    - Bulk query 2: ConversationMessage (phone, source, message, created_at)
    - All attribution and aggregation done in Python memory.
    - Zero N+1 queries. Zero ORM loops.

    Source Priority (evaluated against EARLIEST message per phone):
        1. Campaign  — source == "campaign"
        2. Manual CRM — source == "manual"
        3. Website   — source == "user" AND message contains website keyword
        4. WhatsApp Direct — source == "user" AND no website keyword
        5. Unknown   — no messages found for phone
    """
    from app.models import ConversationState, ConversationMessage
    from app.extensions import db

    # ── Bulk Query 1: all leads ──────────────────────────────────────────
    states = tenant_filter(db.session.query(
        ConversationState.phone,
        ConversationState.is_admitted,
    ), ConversationState, tenant_id).all()

    # ── Bulk Query 2: all messages (phone, source, message, created_at) ──
    messages = tenant_filter(db.session.query(
        ConversationMessage.phone,
        ConversationMessage.source,
        ConversationMessage.message,
        ConversationMessage.created_at,
    ), ConversationMessage, tenant_id).all()

    # ── Build earliest-message index per phone (in memory) ───────────────
    # earliest_msg[phone] = (source, message_text)
    earliest_created: dict = {}   # phone -> created_at of earliest msg
    earliest_msg: dict = {}       # phone -> (source, message_text)

    for phone, source, message, created_at in messages:
        if phone not in earliest_created or (created_at and created_at < earliest_created[phone]):
            earliest_created[phone] = created_at
            earliest_msg[phone] = (source or "", (message or "").lower())

    # ── Attribution helper ────────────────────────────────────────────────
    def _attribute(phone: str) -> str:
        if phone not in earliest_msg:
            return "Unknown"
        src, text = earliest_msg[phone]
        if src == "campaign":
            return "Campaign"
        if src == "manual":
            return "Manual CRM"
        if src == "user":
            if any(kw in text for kw in _WEBSITE_KEYWORDS):
                return "Website"
            return "WhatsApp Direct"
        return "Unknown"

    # ── Aggregate per source ──────────────────────────────────────────────
    SOURCE_ORDER = ["WhatsApp Direct", "Campaign", "Manual CRM", "Website", "Unknown"]
    counts:     dict = {s: 0 for s in SOURCE_ORDER}
    admissions: dict = {s: 0 for s in SOURCE_ORDER}
    total_leads = 0
    total_admissions = 0

    for phone, is_admitted in states:
        total_leads += 1
        source = _attribute(phone)
        counts[source] = counts.get(source, 0) + 1
        if is_admitted:
            total_admissions += 1
            admissions[source] = admissions.get(source, 0) + 1

    # ── Build per-source rows ─────────────────────────────────────────────
    rows = []
    for src in SOURCE_ORDER:
        lead_count = counts[src]
        adm_count  = admissions[src]
        conversion = round((adm_count / lead_count * 100) if lead_count > 0 else 0.0, 1)
        share      = round((lead_count / total_leads * 100) if total_leads > 0 else 0.0, 1)
        rows.append({
            "source":     src,
            "leads":      lead_count,
            "admissions": adm_count,
            "conversion": conversion,
            "share":      share,
        })

    # ── Best / worst source (ignore zero-lead sources) ────────────────────
    active_rows = [r for r in rows if r["leads"] > 0]
    best_source  = max(active_rows, key=lambda r: r["conversion"])["source"] if active_rows else "—"
    worst_source = min(active_rows, key=lambda r: r["conversion"])["source"] if active_rows else "—"

    return {
        "total_leads":      total_leads,
        "total_admissions": total_admissions,
        "best_source":      best_source,
        "worst_source":     worst_source,
        "rows":             rows,
    }


@admin_bp.route("/crm/source-analytics", methods=["GET"])
def crm_source_analytics():
    if not check_auth():
        return _deny()

    data = calculate_source_analytics()

    return render_template(
        "crm_source_analytics.html",
        data=data,
    )


# ── Phase 7D: Admission Analytics ──────────────────────────────────────────

def calculate_admission_analytics(tenant_id=None):
    """
    Read-only admission analytics.

    Query strategy:
    - Bulk Query 1 (only query): SELECT phone, name, is_admitted,
                                        assigned_staff, course, offer_course
                                 FROM conversation_state

    All aggregation is performed in Python memory.
    Zero N+1 queries. Zero writes. Zero ORM loops with per-row queries.

    Course resolution priority:
        1. ConversationState.course       (AI-detected interest)
        2. ConversationState.offer_course (staff override)
        3. "Unknown" fallback
    """
    from app.models import ConversationState
    from app.extensions import db

    # ── Single bulk query ────────────────────────────────────────────────
    rows = tenant_filter(db.session.query(
        ConversationState.phone,
        ConversationState.is_admitted,
        ConversationState.assigned_staff,
        ConversationState.course,
        ConversationState.offer_course,
    ), ConversationState, tenant_id).all()

    # ── Bulk query 2: Fetch ADMISSION_OWNER locks (Phase 9.1) ───────────
    from app.models import LeadEvent
    import json
    admission_events = tenant_filter(db.session.query(LeadEvent.phone, LeadEvent.event_data), LeadEvent, tenant_id).filter_by(event_type="COURSE_ADMISSION").all()
    admission_staff_map = {}
    for phone_num, ev_data in admission_events:
        try:
            js = json.loads(ev_data or "{}")
            if "staff" in js and js["staff"]:
                admission_staff_map[phone_num] = js["staff"]
        except Exception:
            pass

    # ── In-memory aggregation ────────────────────────────────────────────
    total_leads      = 0
    total_admissions = 0

    # staff → {leads, admissions}
    staff_stats:  dict = {}
    # course → {leads, admissions}
    course_stats: dict = {}

    for phone, is_admitted, staff, course, offer_course in rows:
        total_leads += 1
        admitted = bool(is_admitted)
        if admitted:
            total_admissions += 1

        # ── Staff attribution ──────────────────────────────────────────
        # 1. Lead ownership (Pipeline metric) belongs to current staff
        current_staff_key = normalize_staff_name(staff)
        if current_staff_key not in staff_stats:
            staff_stats[current_staff_key] = {"leads": 0, "admissions": 0}
        staff_stats[current_staff_key]["leads"] += 1
        
        # 2. Admission ownership (Performance metric) belongs to staff who closed it
        if admitted:
            admission_staff = admission_staff_map.get(phone, staff)
            adm_staff_key = normalize_staff_name(admission_staff)
            if adm_staff_key not in staff_stats:
                staff_stats[adm_staff_key] = {"leads": 0, "admissions": 0}
            staff_stats[adm_staff_key]["admissions"] += 1

        # ── Course attribution (course → offer_course → Unknown) ───────
        course_key = (course or "").strip() or (offer_course or "").strip() or "Unknown"
        # Collapse internal whitespace, then apply alias normalization
        course_key = " ".join(course_key.split())
        course_key = normalize_course_name(course_key)
        if not course_key:
            course_key = "Unknown"
        if course_key not in course_stats:
            course_stats[course_key] = {"leads": 0, "admissions": 0}
        course_stats[course_key]["leads"] += 1
        if admitted:
            course_stats[course_key]["admissions"] += 1

    # ── Build staff breakdown rows ───────────────────────────────────────
    def _pct(adm, leads):
        return round(adm / leads * 100, 1) if leads > 0 else 0.0

    staff_rows = sorted(
        [
            {
                "name":       name,
                "leads":      s["leads"],
                "admissions": s["admissions"],
                "conversion": _pct(s["admissions"], s["leads"]),
            }
            for name, s in staff_stats.items()
        ],
        key=lambda r: (r["admissions"], r["conversion"]),
        reverse=True,
    )

    # ── Build course breakdown rows ──────────────────────────────────────
    course_rows = sorted(
        [
            {
                "course":     name,
                "leads":      s["leads"],
                "admissions": s["admissions"],
                "conversion": _pct(s["admissions"], s["leads"]),
            }
            for name, s in course_stats.items()
        ],
        key=lambda r: (r["admissions"], r["conversion"]),
        reverse=True,
    )

    # ── Top performers (ignore zero-admission rows for headline KPI) ─────
    admitted_staff   = [r for r in staff_rows  if r["admissions"] > 0]
    admitted_courses = [r for r in course_rows if r["admissions"] > 0]

    top_staff  = admitted_staff[0]["name"]   if admitted_staff   else "—"
    top_course = admitted_courses[0]["course"] if admitted_courses else "—"

    overall_conversion = _pct(total_admissions, total_leads)

    return {
        "total_leads":         total_leads,
        "total_admissions":    total_admissions,
        "overall_conversion":  overall_conversion,
        "top_staff":           top_staff,
        "top_course":          top_course,
        "staff_rows":          staff_rows,
        "course_rows":         course_rows,
    }


@admin_bp.route("/crm/admission-analytics", methods=["GET"])
def crm_admission_analytics():
    if not check_auth():
        return _deny()

    data = calculate_admission_analytics()

    return render_template(
        "crm_admission_analytics.html",
        data=data,
    )


# ── Phase 8.1: Revenue Analytics Dashboard ─────────────────────────────────
#
# READ-ONLY. No schema changes. No migrations. No model changes.
# No webhook changes. No campaign changes. No scoring changes.
#
# Data sources (existing only):
#   Query 1 — ConversationState: phone, is_admitted, assigned_staff, course,
#              offer_course, lead_score
#   Query 2 — LeadEvent: phone, event_type, event_data
#
# Revenue amount: NOT stored in database → displays "Revenue Tracking Not Yet
# Configured" per Phase 8.1 specification. No fabricated values.
#
# Rollback: remove this route + crm_revenue_analytics.html + nav link.
# No database rollback required.
# ────────────────────────────────────────────────────────────────────────────

def calculate_revenue_analytics(tenant_id=None):
    """
    Phase 8.1: Read-only revenue analytics.

    Query strategy:
    - Bulk Query 1: SELECT phone, is_admitted, assigned_staff, course,
                           offer_course, lead_score
                   FROM conversation_state
    - Bulk Query 2: SELECT phone, event_type, event_data
                   FROM lead_event
                   WHERE event_type IN (COURSE_VIEWED, COURSE_ENQUIRY, COURSE_ADMISSION)

    All aggregation is performed in Python memory.
    Zero N+1 queries. Zero writes. Zero ORM per-row loops.

    Revenue amount fields do NOT exist in the database.
    revenue_configured = False → template shows warning banner.
    """
    import json as _json
    from app.models import ConversationState, LeadEvent
    from app.extensions import db

    # ── Bulk Query 1: ConversationState ─────────────────────────────────
    states = tenant_filter(db.session.query(
        ConversationState.phone,
        ConversationState.is_admitted,
        ConversationState.assigned_staff,
        ConversationState.course,
        ConversationState.offer_course,
        ConversationState.lead_score,
    ), ConversationState, tenant_id).all()

    # ── Bulk Query 2: LeadEvent (admission + course events only) ─────────
    events = tenant_filter(db.session.query(
        LeadEvent.phone,
        LeadEvent.event_type,
        LeadEvent.event_data,
    ), LeadEvent, tenant_id).filter(
        LeadEvent.event_type.in_([
            "COURSE_VIEWED",
            "COURSE_ENQUIRY",
            "COURSE_ADMISSION",
        ])
    ).all()

    # ── Revenue amount check ─────────────────────────────────────────────
    # No payment_amount / fee_paid / revenue column exists in any table.
    # Audit confirmed: COURSE_FEES in constants.py are catalog prices only,
    # not per-lead payment records. revenue_configured stays False.
    revenue_configured = False

    # ── In-memory: aggregate ConversationState ───────────────────────────
    total_leads = 0
    total_admissions = 0
    # staff → {assigned, admissions}
    staff_agg: dict = {}

    for phone, is_admitted, staff, course, offer_course, lead_score in states:
        total_leads += 1
        admitted = bool(is_admitted)
        if admitted:
            total_admissions += 1

        # Staff aggregation (ConversationState.assigned_staff)
        #
        # Phase RC2.3E-1 Batch 4: normalized, so one person is ONE row.
        # This was `(staff or "").strip() or "Unassigned"` — case-sensitive,
        # so production rendered FIVE staff rows for THREE people: 'Kiran' 24
        # and 'kiran' 3 sat side by side, each with its own admissions, and
        # the Top Performing Staff KPI was picked from the split figures.
        # normalize_staff_name() also returns "Unassigned" for a blank, which
        # is the same bucket label this line already used.
        staff_key = normalize_staff_name(staff)
        if staff_key not in staff_agg:
            staff_agg[staff_key] = {"assigned": 0, "admissions": 0}
        staff_agg[staff_key]["assigned"] += 1
        if admitted:
            staff_agg[staff_key]["admissions"] += 1

    admitted_pct = round(
        (total_admissions / total_leads * 100) if total_leads > 0 else 0.0, 1
    )

    # ── In-memory: aggregate LeadEvents per course ───────────────────────
    # course → {enquiries: set(phones), admissions: set(phones), views: set(phones)}
    course_agg: dict = {}

    for phone, event_type, event_data in events:
        # Extract course name based on event type
        if event_type in ("COURSE_ENQUIRY", "COURSE_ADMISSION"):
            try:
                parsed = _json.loads(event_data or "{}")
                course_name = (parsed.get("course") or "").strip()
            except (ValueError, TypeError):
                course_name = (event_data or "").strip()
        else:
            # COURSE_VIEWED — plain string
            course_name = (event_data or "").strip()

        # Normalize alias → canonical name
        course_name = normalize_course_name(course_name)
        if not course_name:
            continue

        if course_name not in course_agg:
            course_agg[course_name] = {
                "enquiry_phones":   set(),
                "admission_phones": set(),
                "view_phones":      set(),
            }

        if event_type == "COURSE_ADMISSION":
            course_agg[course_name]["admission_phones"].add(phone)
            # Admission implies enquiry
            course_agg[course_name]["enquiry_phones"].add(phone)
        elif event_type == "COURSE_ENQUIRY":
            course_agg[course_name]["enquiry_phones"].add(phone)
        elif event_type == "COURSE_VIEWED":
            course_agg[course_name]["view_phones"].add(phone)
            # View implies enquiry signal
            course_agg[course_name]["enquiry_phones"].add(phone)

    # ── Build course performance rows ────────────────────────────────────
    def _pct(num, den):
        return round(num / den * 100, 1) if den > 0 else 0.0

    course_rows = []
    for name, agg in course_agg.items():
        enquiries  = len(agg["enquiry_phones"])
        admissions = len(agg["admission_phones"])
        conv       = _pct(admissions, enquiries)
        course_rows.append({
            "course":      name,
            "enquiries":   enquiries,
            "admissions":  admissions,
            "conversion":  conv,
        })
    course_rows.sort(key=lambda r: (r["admissions"], r["conversion"]), reverse=True)

    # ── Build staff performance rows ─────────────────────────────────────
    staff_rows = []
    for name, agg in staff_agg.items():
        assigned   = agg["assigned"]
        admissions = agg["admissions"]
        conv       = _pct(admissions, assigned)
        staff_rows.append({
            "name":       name,
            "assigned":   assigned,
            "admissions": admissions,
            "conversion": conv,
        })
    staff_rows.sort(key=lambda r: (r["admissions"], r["conversion"]), reverse=True)

    # ── Top performers (KPI headlines) ───────────────────────────────────
    admitted_staff   = [r for r in staff_rows   if r["admissions"] > 0 and r["name"] != "Unassigned"]
    admitted_courses = [r for r in course_rows  if r["admissions"] > 0]

    top_staff  = admitted_staff[0]["name"]     if admitted_staff   else "—"
    top_course = admitted_courses[0]["course"] if admitted_courses else "—"

    return {
        # Revenue gate
        "revenue_configured":  revenue_configured,
        # KPI Cards
        "total_admissions":    total_admissions,
        "total_leads":         total_leads,
        "admitted_pct":        admitted_pct,
        "top_staff":           top_staff,
        "top_course":          top_course,
        # Tables
        "course_rows":         course_rows,
        "staff_rows":          staff_rows,
    }


@admin_bp.route("/crm/revenue-analytics", methods=["GET"])
def crm_revenue_analytics():
    """
    Phase 8.1: Revenue Analytics Dashboard.
    Protected by ?key=ADMIN_KEY (same pattern as all CRM analytics pages).
    Read-only. No writes. No schema changes.
    """
    if not check_auth():
        return _deny()

    data = calculate_revenue_analytics()

    return render_template(
        "crm_revenue_analytics.html",
        data=data,
    )


# ── Phase 8.3A: Multi-Course Admission Selection ────────────────────────────

@admin_bp.route("/crm/course-admissions/<phone>", methods=["POST"])
@admin_required
def crm_course_admissions(phone):
    """
    POST /crm/course-admissions/<phone>?key=ADMIN_KEY

    Receives a list of admitted_courses[] checkbox values from the
    Multi-Course Admissions form in crm_lead_detail.html.

    Logic (append-only):
      1. Read existing COURSE_ADMISSION events for this phone.  (1 query)
      2. Read existing course enquiries via get_course_enquiries(). (1 query)
      3. For each submitted course:
           - Validate it exists in the enquiry list (prevents injection)
           - If NOT already in admitted-event history → fire log_lead_event(tenant_id=_get_default_tenant_id(), )
           - If ALREADY in admitted-event history → skip silently
      4. NEVER delete or modify existing COURSE_ADMISSION events.
      5. Redirect back to lead detail with msg= on success or err= on failure.

    Query count: 2 reads + N writes (one per newly admitted course, typically 0-3).
    Schema changes: none. Model changes: none. Analytics: unchanged.
    """
    if not check_auth():
        return _deny()

    import json
    from app.models import LeadEvent
    from app.extensions import db
    from app.services.log_service import log_lead_event

    try:
        from app.models import ConversationState
        # Phase H4-b: NOT consistency-only, unlike the other six in this batch.
        # This route POSTs and calls log_lead_event(tenant_id=_tid), which does
        # NOT guard a falsy tenant -- it calls resolve_tenant_id(), whose
        # fallback is PRIMARY_TENANT_ID. With the legacy idiom an impersonating
        # SUPER_ADMIN would have filed the COURSE_ADMISSION event into the
        # PRIMARY tenant instead of the impersonated one: a cross-tenant write,
        # the same class as the TD-P0-1 mis-filing incident. Latent only because
        # every lead_event today already belongs to the primary tenant.
        _tid = _actor_tenant_id()
        conversation_state = tenant_query(ConversationState, _tid).filter_by(phone=phone).first()
        if conversation_state is None:
            return _not_found(phone)
            
        staff_name = conversation_state.assigned_staff if conversation_state and conversation_state.assigned_staff else ""

        # ── 1. Read already-admitted course names (lowercase set for O(1) lookup) ──
        existing_admission_events = (
            tenant_query(LeadEvent, _tid)
            .filter_by(phone=phone, event_type="COURSE_ADMISSION")
            .all()
        )
        already_admitted_lower: set = set()
        for ev in existing_admission_events:
            try:
                data = json.loads(ev.event_data or "{}")
                name = (data.get("course") or "").strip()
                if name:
                    already_admitted_lower.add(normalize_course_name(name).lower())
            except (ValueError, TypeError):
                pass

        # ── 2. Read valid enquiry courses (source of truth for checkbox values) ──
        valid_enquiry_courses_lower: set = {
            c.lower() for c in get_course_enquiries(phone)
        }

        # ── 3. Process submitted checkboxes ───────────────────────────────────────
        # request.form.getlist() returns [] if no boxes checked (all unchecked).
        submitted_courses = request.form.getlist("admitted_courses")

        newly_admitted: list = []
        for raw_course in submitted_courses:
            course = normalize_course_name(raw_course.strip())
            if not course:
                continue
            # Security: only accept courses that came from the enquiry list
            if course.lower() not in valid_enquiry_courses_lower:
                continue
            # Idempotency: skip if already recorded
            if course.lower() in already_admitted_lower:
                continue
            # Append-only: fire one new COURSE_ADMISSION event
            log_lead_event(tenant_id=_tid,
                phone=phone,
                event_type="COURSE_ADMISSION",
                event_data=json.dumps({
                    "course": course,
                    "staff": staff_name
                }),
            )
            newly_admitted.append(course)

        # ── 4. Redirect with result message ───────────────────────────────────────
        if newly_admitted:
            count = len(newly_admitted)
            names = ", ".join(newly_admitted)
            msg = f"course+admissions+saved%3A+{count}+new+course{'s' if count != 1 else ''}+admitted+%28{'+'.join(n.replace(' ', '+') for n in newly_admitted)}%29"
            return redirect(url_for(
                "admin.crm_lead_detail", phone=phone, msg=msg
            ))
        else:
            # Nothing new — all checked courses already recorded, or nothing checked
            return redirect(url_for(
                "admin.crm_lead_detail", phone=phone,
                msg="course+admissions+saved%3A+no+new+admissions+to+record"
            ))

    except Exception as exc:
        import logging
        logging.exception(f"[crm_course_admissions] Unexpected error for {phone}: {exc}")
        try:
            db.session.rollback()
        except Exception:
            pass
        return redirect(url_for(
            "admin.crm_lead_detail", phone=phone,
            err="course+admission+save+failed%3A+please+try+again"
        ))


# ── Phase 8.5: CRM Health & Data Quality Dashboard ───────────────────────────

def calculate_crm_health(tenant_id=None):
    from app.models import ConversationState, LeadEvent
    from app.bot.constants import normalize_course_name
    from datetime import datetime
    import json

    # 2 bulk queries max
    leads = tenant_query(ConversationState, tenant_id).all()
    events = tenant_query(LeadEvent, tenant_id).all()

    admitted_phones = set()
    enquiries_by_phone = {}
    latest_event_by_phone = {}

    for e in events:
        p = e.phone
        if p not in enquiries_by_phone:
            enquiries_by_phone[p] = set()

        if e.event_type == "COURSE_ADMISSION":
            admitted_phones.add(p)
        elif e.event_type in ("COURSE_ENQUIRY", "COURSE_VIEWED"):
            course = ""
            if e.event_type == "COURSE_ENQUIRY":
                try:
                    data = json.loads(e.event_data or "{}")
                    course = (data.get("course") or "").strip()
                except (ValueError, TypeError):
                    course = (e.event_data or "").strip()
            else:
                course = (e.event_data or "").strip()
            
            course = normalize_course_name(course)
            if course:
                enquiries_by_phone[p].add(course.lower())

        if e.created_at:
            if p not in latest_event_by_phone or e.created_at > latest_event_by_phone[p]:
                latest_event_by_phone[p] = e.created_at

    critical_issues = []
    warning_issues = []
    
    now = datetime.utcnow()
    total_leads = len(leads)
    unhealthy_lead_phones = set()

    # Track staff variations
    normalized_to_raw = {}

    for lead in leads:
        p = lead.phone
        # Record raw staff for duplicate detection
        raw_staff = (lead.assigned_staff or "").strip()
        if raw_staff:
            norm = normalize_staff_name(raw_staff)
            if norm not in normalized_to_raw:
                normalized_to_raw[norm] = set()
            normalized_to_raw[norm].add(raw_staff)
            
        staff = normalize_staff_name(raw_staff)
        if staff == "Unassigned":
            staff = ""
            
        score = lead.lead_score or 0
        is_admitted = lead.is_admitted
        
        has_admission_event = p in admitted_phones
        enquiry_count = len(enquiries_by_phone.get(p, set()))
        
        latest_act = latest_event_by_phone.get(p)
        if not latest_act:
            latest_act = lead.updated_at or lead.created_at
        else:
            if lead.updated_at and lead.updated_at > latest_act:
                latest_act = lead.updated_at
        
        days_inactive = (now - (latest_act or now)).days

        lead_name_display = lead.name or "Unknown"

        # CRITICAL CHECKS
        is_critical = False
        if is_admitted and not staff:
            critical_issues.append({"phone": p, "name": lead_name_display, "issue": "Admitted lead with no assigned staff"})
            is_critical = True
        
        if is_admitted and not has_admission_event:
            critical_issues.append({"phone": p, "name": lead_name_display, "issue": "Admitted lead with no COURSE_ADMISSION event"})
            is_critical = True
            
        if score >= 80 and not staff:
            critical_issues.append({"phone": p, "name": lead_name_display, "issue": "Lead score >= 80 and no assigned staff"})
            is_critical = True

        # WARNING CHECKS
        is_warning = False
        if score >= 80 and not is_admitted:
            warning_issues.append({"phone": p, "name": lead_name_display, "issue": "Lead score >= 80 and not admitted"})
            is_warning = True
            
        if enquiry_count >= 2 and not has_admission_event:
            warning_issues.append({"phone": p, "name": lead_name_display, "issue": "Multiple course enquiries and zero admissions"})
            is_warning = True
            
        if not staff:
            warning_issues.append({"phone": p, "name": lead_name_display, "issue": "Unassigned lead"})
            is_warning = True
            
        if days_inactive >= 7:
            warning_issues.append({"phone": p, "name": lead_name_display, "issue": f"No activity for {days_inactive} days"})
            is_warning = True

        if is_critical or is_warning:
            unhealthy_lead_phones.add(p)

    healthy_count = total_leads - len(unhealthy_lead_phones)

    # Duplicate Staff Naming Warning & Penalty
    for norm_name, variants in normalized_to_raw.items():
        if len(variants) > 1:
            warning_issues.insert(0, {
                "phone": "-",
                "name": "System",
                "issue": f"Duplicate Staff Naming Detected Variants: {' / '.join(variants)}"
            })
            # Penalize health score
            healthy_count -= len(variants)

    health_score = (healthy_count / total_leads * 100) if total_leads > 0 else 100.0

    return {
        "total_leads": total_leads,
        "health_score": round(health_score, 1),
        "critical_count": len(critical_issues),
        "warning_count": len(warning_issues),
        "critical_issues": critical_issues,
        "warning_issues": warning_issues
    }


@admin_bp.route("/crm/health", methods=["GET"])
def crm_health():
    """
    Phase 8.5: CRM Health & Data Quality Dashboard.
    Protected by ?key=ADMIN_KEY. Read-only.
    """
    if not check_auth():
        return _deny()
    
    data = calculate_crm_health()
    
    return render_template(
        "crm_health.html",
        key=request.args.get("key", ""),
        data=data,
    )


# ── Phase 8.6: CRM Action Center (Read-Only) ─────────────────────────────

def calculate_action_center(tenant_id=None):
    from app.models import ConversationState, LeadEvent
    from app.bot.constants import normalize_course_name
    from datetime import datetime, timedelta
    import json

    FOLLOWUP_DAYS = 3
    now = datetime.utcnow()
    followup_threshold_date = now - timedelta(days=FOLLOWUP_DAYS)

    # 1. Fetch filtered events
    events = tenant_query(LeadEvent, tenant_id).filter(LeadEvent.event_type.in_([
        "COURSE_VIEWED",
        "COURSE_ENQUIRY",
        "COURSE_ADMISSION",
        "FEES_REQUESTED",
        "DEMO_REQUESTED"
    ])).all()

    # 2. Fetch all leads
    leads = tenant_query(ConversationState, tenant_id).all()

    # Process events in a single O(E) pass
    phone_data = {}
    for e in events:
        p = e.phone
        if p not in phone_data:
            phone_data[p] = {
                "enquiries": set(),
                "admissions": set(),
                "has_demo": False,
                "has_fees": False,
                "latest_activity": None
            }

        data = phone_data[p]
        et = e.event_type

        # Track latest activity
        if e.created_at:
            if not data["latest_activity"] or e.created_at > data["latest_activity"]:
                data["latest_activity"] = e.created_at

        # Categorize event
        if et == "DEMO_REQUESTED":
            data["has_demo"] = True
        elif et == "FEES_REQUESTED":
            data["has_fees"] = True
        elif et == "COURSE_ADMISSION":
            # Just mark they have an admission, course name tracking for admission not strictly required for logic but good
            data["admissions"].add("yes")
        elif et in ("COURSE_VIEWED", "COURSE_ENQUIRY"):
            course = ""
            if et == "COURSE_ENQUIRY":
                try:
                    js = json.loads(e.event_data or "{}")
                    course = (js.get("course") or "").strip()
                except (ValueError, TypeError):
                    course = (e.event_data or "").strip()
            else:
                course = (e.event_data or "").strip()
            
            course = normalize_course_name(course)
            if course:
                data["enquiries"].add(course.lower())

    # Initialize buckets
    admission_ready = []
    hot_leads = []
    multi_course = []
    demo_pending = []
    unassigned_hot = []
    followup_required = []

    assigned_bucket = set()

    # Process leads in a single O(L) pass
    for lead in leads:
        p = lead.phone
        staff_raw = (lead.assigned_staff or "").strip()
        staff = normalize_staff_name(staff_raw)
        if staff == "Unassigned":
            staff = ""
            
        score = lead.lead_score or 0
        is_admitted = lead.is_admitted
        lead_name = lead.name or "Unknown"
        
        pd = phone_data.get(p, {})
        has_demo = pd.get("has_demo", False)
        has_fees = pd.get("has_fees", False)
        enquiries_set = pd.get("enquiries", set())
        admissions_count = len(pd.get("admissions", set()))

        # Determine latest activity for this lead
        event_latest = pd.get("latest_activity")
        latest_act = event_latest
        if not latest_act:
            latest_act = lead.updated_at or lead.created_at
        else:
            if lead.updated_at and lead.updated_at > latest_act:
                latest_act = lead.updated_at
        
        # Calculate days since activity
        days_since_activity = (now - (latest_act or now)).days

        course_interest = ", ".join(enquiries_set) if enquiries_set else "None"

        # Note: 'Unassigned Hot Leads' is tracked separately from operational workflow prioritization.
        if score >= 80 and not staff:
            unassigned_hot.append({
                "phone": p, "name": lead_name, "score": score
            })

        # 1. ADMISSION READY
        if p not in assigned_bucket and has_demo and has_fees and not is_admitted and staff:
            admission_ready.append({
                "phone": p, "name": lead_name, "staff": staff,
                "course": course_interest, "score": score
            })
            assigned_bucket.add(p)

        # 2. HOT LEADS
        if p not in assigned_bucket and score >= 80 and not is_admitted:
            hot_leads.append({
                "phone": p, "name": lead_name, "score": score, "staff": staff or "—"
            })
            assigned_bucket.add(p)

        # 3. MULTI-COURSE OPPORTUNITIES
        if p not in assigned_bucket and len(enquiries_set) >= 3:
            multi_course.append({
                "phone": p, "name": lead_name, "course_count": len(enquiries_set),
                "admissions_count": admissions_count,
                "courses": course_interest, "staff": staff or "—"
            })
            assigned_bucket.add(p)

        # 4. DEMO PENDING
        if p not in assigned_bucket and has_demo and not is_admitted:
            demo_pending.append({
                "phone": p, "name": lead_name, "staff": staff or "—", "course": course_interest
            })
            assigned_bucket.add(p)

        # 5. FOLLOW-UP REQUIRED
        if p not in assigned_bucket and staff and not is_admitted and latest_act and latest_act < followup_threshold_date:
            followup_required.append({
                "phone": p, "name": lead_name, "staff": staff, "days": days_since_activity
            })
            assigned_bucket.add(p)

    # Sort descending by score where applicable, else by days or count
    admission_ready.sort(key=lambda x: x["score"], reverse=True)
    hot_leads.sort(key=lambda x: x["score"], reverse=True)
    unassigned_hot.sort(key=lambda x: x["score"], reverse=True)
    multi_course.sort(key=lambda x: x["course_count"], reverse=True)
    followup_required.sort(key=lambda x: x["days"], reverse=True)

    return {
        "kpis": {
            "total_hot_leads": len(hot_leads),
            "admission_ready": len(admission_ready),
            "unassigned_hot": len(unassigned_hot),
            "followup_required": len(followup_required)
        },
        "admission_ready": admission_ready,
        "hot_leads": hot_leads,
        "multi_course": multi_course,
        "demo_pending": demo_pending,
        "unassigned_hot": unassigned_hot,
        "followup_required": followup_required
    }


@admin_bp.route("/crm/action-center", methods=["GET"])
def crm_action_center():
    """
    Phase 8.6: CRM Action Center
    Protected by ?key=ADMIN_KEY. Read-only operational dashboard.
    """
    if not check_auth():
        return _deny()
    
    data = calculate_action_center()
    
    return render_template(
        "crm_action_center.html",
        key=request.args.get("key", ""),
        data=data,
    )


# ── Phase 8.8: CRM Operations Command Center ─────────────────────────────

def calculate_operations(tenant_id=None, actor=None):
    from app.models import ConversationState, LeadEvent
    from app.bot.constants import normalize_course_name
    from datetime import datetime, timedelta
    import json

    now = datetime.utcnow()
    followup_threshold_date = now - timedelta(days=3)

    events = tenant_query(LeadEvent, tenant_id).all()

    # Phase RC2.3E-3C: STAFF see only leads they own.
    #
    # This helper had NO actor parameter, so it could not filter by owner even
    # in principle — not a check that failed, a check that was never written.
    # /crm/operations is guarded by check_auth(), which authenticates without
    # inspecting role, so under SESSION_ONLY every STAFF member read the whole
    # tenant. Three panels below carry customer name AND phone
    # (data_issues, admission_ready, high_value_ops); production measured 38
    # of 90 customers reaching any staff actor, 25 of them owned by a
    # colleague. The route's own docstring still claims "Protected by
    # ?key=ADMIN_KEY", which is why this was never a deliberate decision.
    #
    # Same mechanism as _build_leads_query, deliberately: one ownership rule,
    # not a second implementation. owner_filter() keys on display_label() and
    # on assigned_user_id once STAFF_IDENTITY_READ_FK is on.
    #
    # Only the LEAD SET narrows. Every downstream calculation, issue class and
    # threshold is untouched, so an ADMIN's numbers are bit-for-bit what they
    # were. `events` is deliberately NOT filtered: it is consulted only via
    # phone_data for leads already in the loop below, so filtering the leads
    # is sufficient and leaves the enquiry/admission derivation unchanged.
    lead_q = tenant_query(ConversationState, tenant_id)
    if actor and actor.get("source") == "SESSION" and actor.get("role") == "STAFF":
        lead_q = lead_q.filter(staff_identity_service.owner_filter(
            ConversationState, current_user))
    leads = lead_q.all()

    phone_data = {}
    for e in events:
        p = e.phone
        if p not in phone_data:
            phone_data[p] = {
                "enquiries": set(),
                "admissions": set(),
                "latest_activity": None,
                "has_admission_event": False
            }

        data = phone_data[p]
        et = e.event_type

        if e.created_at:
            if not data["latest_activity"] or e.created_at > data["latest_activity"]:
                data["latest_activity"] = e.created_at

        if et == "COURSE_ADMISSION":
            data["admissions"].add("yes")
            data["has_admission_event"] = True
        elif et in ("COURSE_VIEWED", "COURSE_ENQUIRY"):
            course = ""
            if et == "COURSE_ENQUIRY":
                try:
                    js = json.loads(e.event_data or "{}")
                    course = (js.get("course") or "").strip()
                except (ValueError, TypeError):
                    course = (e.event_data or "").strip()
            else:
                course = (e.event_data or "").strip()
            
            course = normalize_course_name(course)
            if course:
                data["enquiries"].add(course.lower())

    admission_ready = []
    data_issues = []
    high_value_ops = []
    staff_workload = {}
    normalized_to_raw = {}
    
    total_hot_leads = 0

    for lead in leads:
        p = lead.phone
        raw_staff = (lead.assigned_staff or "").strip()
        if raw_staff:
            norm = normalize_staff_name(raw_staff)
            if norm not in normalized_to_raw:
                normalized_to_raw[norm] = set()
            normalized_to_raw[norm].add(raw_staff)
            
        staff = normalize_staff_name(raw_staff)
        if staff == "Unassigned":
            staff = ""
            
        score = lead.lead_score or 0
        is_admitted = lead.is_admitted
        lead_name = lead.name or "Unknown"
        
        pd = phone_data.get(p, {})
        enquiries_set = pd.get("enquiries", set())
        enquiries_count = len(enquiries_set)
        admissions_count = len(pd.get("admissions", set()))
        has_admission_event = pd.get("has_admission_event", False)

        latest_act = pd.get("latest_activity")
        if not latest_act:
            latest_act = lead.updated_at or lead.created_at
        else:
            if lead.updated_at and lead.updated_at > latest_act:
                latest_act = lead.updated_at
        
        course_interest = ", ".join(enquiries_set) if enquiries_set else "None"

        # 1. ADMISSION READY
        is_adm_ready = False
        if staff and score >= 60 and enquiries_count >= 1 and not is_admitted:
            is_adm_ready = True
            admission_ready.append({
                "phone": p, "name": lead_name, "staff": staff,
                "enquiries": enquiries_count, "admissions": admissions_count,
                "score": score
            })

        # 2. DATA ISSUES
        if is_admitted and not has_admission_event:
            data_issues.append({"phone": p, "name": lead_name, "issue": "Admitted lead with no COURSE_ADMISSION event"})
        if not staff:
            data_issues.append({"phone": p, "name": lead_name, "issue": "Unassigned lead"})
        if enquiries_count >= 2 and not has_admission_event:
            data_issues.append({"phone": p, "name": lead_name, "issue": "Multiple course enquiries and zero admissions"})

        # 3. HIGH VALUE OPPORTUNITIES
        if score >= 80 and enquiries_count >= 2 and not is_admitted:
            high_value_ops.append({
                "phone": p, "name": lead_name,
                "courses": course_interest, "staff": staff or "—", "score": score
            })

        if score >= 80 and not is_admitted:
            total_hot_leads += 1

        # 4. STAFF WORKLOAD SUMMARY
        if staff:
            if staff not in staff_workload:
                staff_workload[staff] = {
                    "assigned": 0, "hot": 0, "admission_ready": 0, "followup": 0
                }
            staff_workload[staff]["assigned"] += 1
            if score >= 80 and not is_admitted:
                staff_workload[staff]["hot"] += 1
            if is_adm_ready:
                staff_workload[staff]["admission_ready"] += 1
            if not is_admitted and latest_act and latest_act < followup_threshold_date:
                staff_workload[staff]["followup"] += 1

    for norm_name, variants in normalized_to_raw.items():
        if len(variants) > 1:
            data_issues.insert(0, {
                "phone": "-",
                "name": "System",
                "issue": f"Duplicate Staff Naming Detected Variants: {' / '.join(variants)}"
            })

    admission_ready.sort(key=lambda x: x["score"], reverse=True)
    high_value_ops.sort(key=lambda x: x["score"], reverse=True)
    
    staff_workload_list = []
    for s, w in staff_workload.items():
        w["staff"] = s
        staff_workload_list.append(w)
    staff_workload_list.sort(key=lambda x: x["assigned"], reverse=True)

    total_followups = sum(w["followup"] for w in staff_workload_list)

    return {
        "kpis": {
            "total_admission_ready": len(admission_ready),
            "total_hot_leads": total_hot_leads,
            "total_data_issues": len(data_issues),
            "total_followups": total_followups
        },
        "admission_ready": admission_ready,
        "data_issues": data_issues,
        "high_value_ops": high_value_ops,
        "staff_workload": staff_workload_list
    }

@admin_bp.route("/crm/operations", methods=["GET"])
def crm_operations():
    """
    Phase 8.8: CRM Operations Command Center
    Protected by ?key=ADMIN_KEY. Read-only.
    """
    if not check_auth():
        return _deny()
    
    # Phase RC2.3E-3C: the actor decides which leads this page may show. The
    # tenant argument stays None so tenant resolution is byte-for-byte what it
    # was (tenant_query resolves it, honouring SUPER_ADMIN impersonation);
    # only the ownership filter is new.
    data = calculate_operations(actor=get_current_actor())
    # Phase RC2.3E-9: same actor, same reason — it narrows the priority queue
    # (Module 4) only. crm_staff_dashboard deliberately keeps calling this with
    # the default actor=None so its leaderboard/rank stays tenant-wide.
    intel = calculate_intelligence(actor=get_current_actor())
    
    # Phase 9.6
    from app.models import ConversationState, LeadEvent
    intel_event_types = ["FOLLOW_UP_TASK", "FOLLOW_UP_COMPLETED"]
    # Phase H4-b: consistency. _tid here feeds ONLY tenant_query(), whose
    # SUPER_ADMIN branch returns before reading the argument, so this
    # changes no behaviour -- it makes all four/seven routes resolve the
    # tenant by one rule.
    _tid = _actor_tenant_id()
    auto_events = tenant_query(LeadEvent, _tid).filter(LeadEvent.event_type.in_(intel_event_types)).all()
    leads = tenant_query(ConversationState, _tid).all()
    # Phase RC2.3E-10A: same actor, same reason — it narrows the four
    # customer-record lists only. `aging` and `productivity` are unchanged, and
    # crm_staff_dashboard deliberately keeps calling this with no actor.
    automation = calculate_automation_intelligence(leads, auto_events,
                                                   actor=get_current_actor())

    return render_template(
        "crm_operations.html",
        key=request.args.get("key", ""),
        data=data,
        intel=intel,
        automation=automation,
    )




# ── Phase 9.5: Operations Intelligence Layer ──────────────────────────

def calculate_intelligence(tenant_id=None, actor=None):
    """
    Five intelligence modules. Exactly TWO bulk queries total.
    Query 1: LeadEvent filtered to intel types only.
    Query 2: ConversationState scoped to tenant.
    O(L+E). No N+1. Read-only.

    Phase RC2.3E-9: `actor` narrows MODULE 4 ONLY (the priority queue). It is
    deliberately not applied to `leads`, which every other module aggregates
    from — see the comment at Module 4. A STAFF actor adds ONE query; ADMIN
    and the default actor=None path issue exactly the two queries above, so
    the documented cost is unchanged for every existing caller.
    """
    from app.models import ConversationState, LeadEvent
    from datetime import datetime
    import json

    now = datetime.utcnow()
    today = now.date()

    intel_event_types = [
        "FOLLOW_UP_TASK", "FOLLOW_UP_COMPLETED",
        "COURSE_ADMISSION", "LEAD_REASSIGNED", "MANUAL_MESSAGE"
    ]
    events = tenant_query(LeadEvent, tenant_id).filter(
        LeadEvent.event_type.in_(intel_event_types)
    ).order_by(LeadEvent.created_at.desc()).all()

    leads = tenant_query(ConversationState, tenant_id).all()
    lead_map = {l.phone: l for l in leads}

    # Phase RC2.2D Batch 2: candidate set is now the tenant's own active staff.
    #
    # The five intelligence modules are UNCHANGED — same normalize_staff_name()
    # applied per name, same aggregation, same downstream sorts. Only WHO is
    # counted changed.
    #
    # Ordering note: the legacy list came out in staff_master.json insertion
    # order (ANJU, KIRAN, NISHA); this one is alphabetical. Both leaderboard
    # and workload_snapshot are re-sorted downstream by their own keys, and
    # Python's sort is stable, so input order can only affect ties — and for
    # Oxford the two orders are identical anyway.
    #
    # Resolved defensively: _actor_tenant_id() reads current_user, which does
    # not exist outside a request. Every caller today is in-request, but this
    # helper previously read a FILE and so worked anywhere; turning that into
    # an AttributeError would be a worse failure than the one being fixed.
    _tid_staff = tenant_id
    if not _tid_staff:
        try:
            _tid_staff = _actor_tenant_id()
        except Exception:                                   # noqa: BLE001
            _tid_staff = None
    active_staff_names = staff_service.active_display_names(_tid_staff)

    staff_admissions = {}
    staff_task_open = {}
    staff_task_done = {}
    task_events_map = {}
    completed_ids = set()
    phone_open_tasks = {}

    # Phase 10M-E: Admissions attributed via is_admitted flag (canonical source).
    # COURSE_ADMISSION events are retained in the event fetch for the Activity Feed only.
    # They are deliberately NOT used for admission counting here.
    for lead in leads:
        s = normalize_staff_name(lead.assigned_staff or "")
        if lead.is_admitted and s and s != "Unassigned":
            staff_admissions[s] = staff_admissions.get(s, 0) + 1

    for ev in events:
        try:
            edata = json.loads(ev.event_data or "{}")
        except Exception:
            edata = {}

        if ev.event_type == "FOLLOW_UP_TASK":
            tid = edata.get("task_id")
            s = normalize_staff_name(edata.get("staff", ""))
            if tid and s and s != "Unassigned":
                staff_task_open.setdefault(s, set()).add(tid)
                if tid not in task_events_map:
                    task_events_map[tid] = {
                        "task_id": tid,
                        "phone": ev.phone,
                        "due_date": edata.get("due_date", ""),
                        "staff": s,
                        "task": edata.get("task", ""),
                    }

        elif ev.event_type == "FOLLOW_UP_COMPLETED":
            tid = edata.get("task_id")
            by = normalize_staff_name(edata.get("completed_by", edata.get("staff", "")))
            if tid:
                completed_ids.add(tid)
            if tid and by and by != "Unassigned":
                staff_task_done.setdefault(by, set()).add(tid)

    for tid, t in task_events_map.items():
        if tid not in completed_ids:
            p = t["phone"]
            phone_open_tasks[p] = phone_open_tasks.get(p, 0) + 1

    staff_assigned = {}
    for lead in leads:
        s = normalize_staff_name(lead.assigned_staff or "")
        if s and s != "Unassigned":
            staff_assigned[s] = staff_assigned.get(s, 0) + 1

    # Module 1: Leaderboard
    leaderboard = []
    for staff in active_staff_names:
        s = normalize_staff_name(staff)
        assigned = staff_assigned.get(s, 0)
        admissions = staff_admissions.get(s, 0)
        open_set = staff_task_open.get(s, set()) - staff_task_done.get(s, set())
        conversion = round((admissions / assigned * 100), 1) if assigned > 0 else 0.0
        leaderboard.append({
            "name": staff, "assigned_leads": assigned,
            "admissions": admissions, "conversion": conversion,
            "open_tasks": len(open_set),
        })
    leaderboard.sort(key=lambda x: (x["admissions"], x["conversion"], x["assigned_leads"]), reverse=True)

    # Module 2: SLA Dashboard
    sla = {"due_today": 0, "overdue_1_3": 0, "overdue_4_7": 0, "overdue_7plus": 0}
    for tid, t in task_events_map.items():
        if tid in completed_ids:
            continue
        due = t.get("due_date", "")
        if not due:
            continue
        try:
            due_dt = datetime.strptime(due, "%Y-%m-%d").date()
            diff = (today - due_dt).days
            if diff == 0:
                sla["due_today"] += 1
            elif 1 <= diff <= 3:
                sla["overdue_1_3"] += 1
            elif 4 <= diff <= 7:
                sla["overdue_4_7"] += 1
            elif diff > 7:
                sla["overdue_7plus"] += 1
        except Exception:
            pass

    # Phase RC2.3E-12: the owned-lead set, resolved ONCE for this call.
    #
    # RC2.3E-9 computed this inside Module 4. Module 3 needs the same set, so
    # it is hoisted here and both modules read it — one query, not two, and one
    # ownership rule. None means "no ownership narrowing": ADMIN, SUPER_ADMIN
    # and the default actor=None path are byte-for-byte unchanged, including
    # their query count.
    _owned_phones = None
    if actor and actor.get("source") == "SESSION" and actor.get("role") == "STAFF":
        _owned_phones = {
            row[0] for row in
            tenant_query(ConversationState, tenant_id)
            .with_entities(ConversationState.phone)
            .filter(staff_identity_service.owner_filter(
                ConversationState, current_user)).all()
        }

    # Module 3: Activity Feed (newest first, max 50)
    #
    # Phase RC2.3E-12: STAFF see activity on leads THEY OWN.
    #
    # This panel has no name/phone field — it renders a pre-formatted `label`,
    # and three of the five event types interpolate the customer:
    #   COURSE_ADMISSION  "{staff} admitted {lead_name}: {course}"
    #   LEAD_REASSIGNED   "Reassigned {lead_name}: {from} -> {to}"
    #   MANUAL_MESSAGE    "{staff} messaged {lead_name}"
    # so no field-level redaction reaches it. Production measured 45 of 50
    # entries naming a customer, 43 distinct customers, 29-45 of them not the
    # viewer's — a larger disclosure than RC2.3E-9 or RC2.3E-10A, and it also
    # revealed the tenant's reassignment history. lead_name falls back to
    # ev.phone for a nameless lead, so a raw customer number can appear in the
    # label text too.
    #
    # OWNERSHIP OF THE LEAD, NOT THE ACTOR IN THE EVENT PAYLOAD.
    # The approved definition is "activity on my leads", not "my activity".
    # edata['staff'] / 'completed_by' are display strings written at event
    # time; they are not an authorization signal and can name someone who no
    # longer owns the lead. Keying on ev.phone against the owned set uses the
    # same rule as every other filtered panel.
    #
    # The cap is applied AFTER this check, deliberately: it counts RENDERED
    # entries, so a staff member still sees their newest 50 rather than having
    # the budget consumed by colleagues' events they cannot see.
    activity_feed = []
    feed_types = {"LEAD_REASSIGNED", "COURSE_ADMISSION", "FOLLOW_UP_TASK",
                  "FOLLOW_UP_COMPLETED", "MANUAL_MESSAGE"}
    for ev in events:
        if ev.event_type not in feed_types or len(activity_feed) >= 50:
            continue
        if _owned_phones is not None and ev.phone not in _owned_phones:
            continue
        lead = lead_map.get(ev.phone)
        lead_name = (lead.name if lead and lead.name else None) or ev.phone
        try:
            edata = json.loads(ev.event_data or "{}")
        except Exception:
            edata = {}
        s = normalize_staff_name(
            edata.get("staff") or edata.get("new_staff") or
            (lead.assigned_staff if lead else "") or ""
        )
        if ev.event_type == "FOLLOW_UP_COMPLETED":
            by = normalize_staff_name(edata.get("completed_by", s))
            label = f"{by} completed task: {edata.get('task', '')[:35]}"
            icon, color = "bi-check2-circle", "var(--green)"
        elif ev.event_type == "FOLLOW_UP_TASK":
            label = f"{s} created task: {edata.get('task', '')[:35]}"
            icon, color = "bi-calendar-plus", "var(--yellow)"
        elif ev.event_type == "COURSE_ADMISSION":
            course = (ev.event_data or "")[:35]
            label = f"{s} admitted {lead_name}: {course}"
            icon, color = "bi-mortarboard", "var(--purple)"
        elif ev.event_type == "LEAD_REASSIGNED":
            from_s = normalize_staff_name(edata.get("from", "?"))
            to_s = normalize_staff_name(edata.get("to", "?"))
            label = f"Reassigned {lead_name}: {from_s} → {to_s}"
            icon, color = "bi-arrow-left-right", "var(--blue)"
        elif ev.event_type == "MANUAL_MESSAGE":
            label = f"{s} messaged {lead_name}"
            icon, color = "bi-chat-dots", "var(--text-muted)"
        else:
            continue
        ts = ev.created_at
        activity_feed.append({
            "time": ts.strftime("%I:%M %p") if ts else "—",
            "date": ts.strftime("%d %b") if ts else "",
            "label": label, "icon": icon, "color": color,
        })

    # Module 4: Priority Opportunity Queue (score >= HOT, not admitted, top 25)
    #
    # Phase RC2.3E-9: STAFF see only leads they own.
    #
    # RC2.3E-3C isolated the three calculate_operations() panels on this same
    # page and had to leave this one open: it is produced by THIS helper, which
    # had no actor parameter, and crm_staff_dashboard calls it too. The rows
    # carry customer name AND phone, plus an /crm/lead/<phone> link — so the
    # exposure was not only disclosure but a working click-through to a
    # colleague's customer, where lead update/stage/send sit behind
    # check_auth() alone. (Those route guards are a separate RBAC question and
    # are NOT touched here.)
    #
    # THE FILTER IS SCOPED TO THIS MODULE, NOT TO `leads`.
    # Filtering `leads` would change leaderboard, sla, activity_feed and
    # workload_snapshot for BOTH callers, and crm_staff_dashboard derives the
    # viewer's RANK from that leaderboard — filtered to one person, every staff
    # member would rank #1. crm_staff_dashboard never renders priority_queue,
    # so confining the filter here leaves that screen provably untouched.
    #
    # owner_filter() is the same ownership rule used by _build_leads_query, the
    # deactivation guard and calculate_operations — not a second one. It is a
    # SQL predicate and `leads` is already materialised, so ownership is
    # resolved by one extra scoped query and applied to the in-memory rows.
    # That query runs ONLY for a SESSION STAFF actor: ADMIN, SUPER_ADMIN and
    # the default actor=None path are byte-for-byte unchanged, including their
    # query count.
    # Phase RC2.3E-12: reads the set hoisted above Module 3 rather than
    # resolving it again. Same rule, same values, one query for both modules.
    _pq_leads = leads
    if _owned_phones is not None:
        _pq_leads = [l for l in leads if l.phone in _owned_phones]

    priority_queue = []
    for lead in _pq_leads:
        score = lead.lead_score or 0
        if score >= INTELLIGENCE_CONSTANTS["THRESHOLD_HOT"] and not lead.is_admitted:
            priority_queue.append({
                "phone": lead.phone,
                "name": lead.name or "Unknown",
                "staff": normalize_staff_name(lead.assigned_staff or ""),
                "score": score,
                "follow_ups": phone_open_tasks.get(lead.phone, 0),
                "status": lead.lead_status or "—",
            })
    priority_queue.sort(key=lambda x: x["score"], reverse=True)
    priority_queue = priority_queue[:25]

    # Module 5: Workload Snapshot
    workload_snapshot = []
    for staff in active_staff_names:
        s = normalize_staff_name(staff)
        open_set = staff_task_open.get(s, set()) - staff_task_done.get(s, set())
        overdue_t = 0
        for tid in open_set:
            t = task_events_map.get(tid, {})
            due = t.get("due_date", "")
            if due:
                try:
                    due_dt = datetime.strptime(due, "%Y-%m-%d").date()
                    if (today - due_dt).days > 0:
                        overdue_t += 1
                except Exception:
                    pass
        workload_snapshot.append({
            "name": staff,
            "assigned_leads": staff_assigned.get(s, 0),
            "open_tasks": len(open_set),
            "overdue_tasks": overdue_t,
            "admissions": staff_admissions.get(s, 0),
        })
    workload_snapshot.sort(key=lambda x: x["assigned_leads"], reverse=True)

    return {
        "leaderboard": leaderboard,
        "sla": sla,
        "activity_feed": activity_feed,
        "priority_queue": priority_queue,
        "workload_snapshot": workload_snapshot,
    }


# ── Phase 9.6: Automation & Lead Nurturing Engine ────────────────────────────

def get_nurture_health_score(lead, lead_events_list, today):
    """
    [LEGACY] Phase 10N-A Safety Audit: Preserved for future AI Layer work.
    Weighted scoring for relationship strength.
    Output: Excellent (80+), Good (60-79), Average (40-59), Weak (0-39).
    """
    score = 0
    if lead.updated_at:
        days_since = (today - lead.updated_at.date()).days
        if days_since <= 7:
            score += 25
        elif days_since <= 14:
            score += 15
        elif days_since <= 30:
            score += 5

    for ev in lead_events_list:
        if ev.event_type == "COURSE_VIEWED":
            score += 10
        elif ev.event_type == "DEMO_REQUESTED":
            score += 20
        elif ev.event_type == "FEES_REQUESTED":
            score += 20
        elif ev.event_type == "FOLLOW_UP_COMPLETED":
            score += 10
        elif ev.event_type == "COURSE_ADMISSION":
            score += 30

    if score >= 80:
        return "Excellent"
    elif score >= 60:
        return "Good"
    elif score >= 40:
        return "Average"
    else:
        return "Weak"

def get_admission_probability(lead, lead_events_list):
    """
    [LEGACY] Phase 10N-A Safety Audit: Preserved for future AI Layer work.
    High: lead_score >= 80 AND (DEMO_REQUESTED or FEES_REQUESTED)
    Medium: lead_score >= 50
    Low: everything else
    """
    import json
    score = lead.lead_score or 0
    has_signal = any(ev.event_type in ("DEMO_REQUESTED", "FEES_REQUESTED") for ev in lead_events_list)
    
    if score >= 80 and has_signal:
        return "High"
    elif score >= 50:
        return "Medium"
    else:
        return "Low"

def get_auto_task_suggestions(lead, lead_events_list, open_task_titles):
    """
    [LEGACY] Phase 10N-A Safety Audit: Preserved for future AI Layer work.
    Suggests tasks based on signals if not already open.
    """
    suggestions = []
    signals = {ev.event_type for ev in lead_events_list}
    
    if "DEMO_REQUESTED" in signals and not any("Demo" in t for t in open_task_titles):
        suggestions.append({"title": "Demo Follow-Up", "notes": "Follow up on requested demo session."})
    
    if "FEES_REQUESTED" in signals and not any("Fee" in t for t in open_task_titles):
        suggestions.append({"title": "Send Fee Structure", "notes": "Send latest fee structure and payment options."})
        
    if (lead.lead_score or 0) >= 80 and not any("Admission" in t for t in open_task_titles):
        suggestions.append({"title": "Admission Follow-Up", "notes": "Follow up regarding admission decision."})
        
    return suggestions

def calculate_automation_intelligence(leads, events, actor=None):
    """
    Phase 9.6: 2 Bulk Queries (via args). No N+1.
    Computes Aging, Recovery, Follow-Up Recommendations, and Staff Productivity.

    Phase RC2.3E-10A: `actor` narrows ONLY the four CUSTOMER-RECORD lists
    (unassigned_hot, stalled_admissions, recovery_queue, recommendations). It
    does NOT touch `aging`, which is counted in its own earlier loop, and it
    cannot touch `productivity`, which is derived from `events` alone. A STAFF
    actor adds ONE query; every existing caller passes no actor and is
    byte-for-byte unchanged, including its query count.
    """
    from datetime import datetime
    from app.models import LEAD_TERMINAL_STATUSES
    import json

    now = datetime.utcnow()
    today = now.date()

    # Build maps
    lead_map = {l.phone: l for l in leads}
    phones_payment_pending = {ev.phone for ev in events if ev.event_type == 'PAYMENT_PENDING'}
    
    # 1. Lead Aging Engine
    aging = {"fresh": 0, "attention": 0, "risk": 0, "dormant": 0}
    
    for lead in leads:
        if lead.is_admitted or lead.lead_status in LEAD_TERMINAL_STATUSES:
            continue
        days = (today - (lead.updated_at.date() if lead.updated_at else today)).days
        bucket = get_aging_bucket(days, mode="automation")
        aging[bucket] += 1

    # Track open tasks by phone
    phone_open_tasks = {}
    completed_task_ids = set()
    staff_productivity = {}
    
    for ev in events:
        try:
            edata = json.loads(ev.event_data or "{}")
        except Exception:
            edata = {}
            
        if ev.event_type == "FOLLOW_UP_COMPLETED":
            tid = edata.get("task_id")
            by = normalize_staff_name(edata.get("completed_by", edata.get("staff", "")))
            if tid:
                completed_task_ids.add(tid)
            if by and by != "Unassigned":
                if by not in staff_productivity:
                    staff_productivity[by] = {"created": 0, "completed": 0, "open": 0, "overdue": 0}
                staff_productivity[by]["completed"] += 1
                
        elif ev.event_type == "FOLLOW_UP_TASK":
            tid = edata.get("task_id")
            staff = normalize_staff_name(edata.get("staff", ""))
            if staff and staff != "Unassigned":
                if staff not in staff_productivity:
                    staff_productivity[staff] = {"created": 0, "completed": 0, "open": 0, "overdue": 0}
                staff_productivity[staff]["created"] += 1
                
                if tid not in completed_task_ids:
                    staff_productivity[staff]["open"] += 1
                    phone_open_tasks[ev.phone] = phone_open_tasks.get(ev.phone, 0) + 1
                    due_date = edata.get("due_date")
                    if due_date:
                        try:
                            if (today - datetime.strptime(due_date, "%Y-%m-%d").date()).days > 0:
                                staff_productivity[staff]["overdue"] += 1
                        except:
                            pass

    # 2. Recovery Queue (score >= WARM, not admitted, silent > 14 days)
    recovery_queue = []
    
    # 3. Follow-Up Recommendations
    recommendations = []
    
    # Phase 10N-D: Admin Operations Signals
    unassigned_hot = []
    stalled_admissions = []

    # Phase RC2.3E-10A: STAFF see only leads they own.
    #
    # The four lists built below carry customer NAME and PHONE, and the
    # template renders each with an /crm/lead/<phone> link — where lead
    # detail/update/stage/send sit behind check_auth() alone. So this was not
    # only disclosure but a working click-through to a colleague's customer.
    # (Those route guards are a separate RBAC defect and are NOT touched here.)
    # Production measured 20 rendered rows (all from recommendations), 17-20 of
    # them not the viewer's, for 3 of 3 STAFF members. unassigned_hot and
    # recovery_queue were empty by DATA and populate as soon as a lead meets
    # their predicates. stalled_admissions is empty by CONSTRUCTION: it keys on
    # PAYMENT_PENDING events, but both callers pass only FOLLOW_UP_TASK /
    # FOLLOW_UP_COMPLETED, so its set is always empty. That is a pre-existing
    # dead panel, not fixed here — reviving it would ADD a panel's worth of
    # customer PII and belongs in its own phase.
    #
    # SCOPED TO THIS LOOP ONLY.
    # `aging` is counted in its own earlier loop over `leads` and stays
    # tenant-wide; `productivity` is derived from `events` and cannot be
    # affected. crm_staff_dashboard consumes ONLY productivity — it never even
    # passes `automation` to its template — so it is provably untouched.
    # Filtering `leads` globally would silently turn `aging` from a tenant-wide
    # count into a per-staff one, which nobody asked for.
    #
    # owner_filter() is the same ownership rule used by _build_leads_query, the
    # deactivation guard, calculate_operations and calculate_intelligence — not
    # a second one. It is a SQL predicate and `leads` arrives already
    # materialised, so ownership is resolved by one scoped query and applied to
    # the in-memory rows.
    #
    # UNASSIGNED LEADS DISAPPEAR FOR STAFF, deliberately: they have no owner,
    # so no ownership rule can match them. Approved, and consistent with
    # RC2.3E-3C, where the same consequence was accepted for the "Unassigned
    # lead" issue class — claiming unassigned work stays an ADMIN capability.
    _cust_leads = leads
    if actor and actor.get("source") == "SESSION" and actor.get("role") == "STAFF":
        from app.models import ConversationState
        _owned_phones = {
            row[0] for row in
            tenant_query(ConversationState)
            .with_entities(ConversationState.phone)
            .filter(staff_identity_service.owner_filter(
                ConversationState, current_user)).all()
        }
        _cust_leads = [l for l in leads if l.phone in _owned_phones]

    for lead in _cust_leads:
        if lead.is_admitted or lead.lead_status in LEAD_TERMINAL_STATUSES:
            continue
            
        days = (today - (lead.updated_at.date() if lead.updated_at else today)).days
        score = lead.lead_score or 0
        assigned_staff_norm = normalize_staff_name(lead.assigned_staff or "")
        
        # Admin Signal 1: Unassigned Hot Leads
        if score >= INTELLIGENCE_CONSTANTS["THRESHOLD_HOT"] and assigned_staff_norm == "Unassigned" and not lead.is_admitted:
            unassigned_hot.append({
                "phone": lead.phone,
                "name": lead.name or "Unknown",
                "score": score,
                "days_silent": days
            })
            
        # Admin Signal 2: Stalled Admissions (Payment Pending but not admitted)
        if not lead.is_admitted and lead.phone in phones_payment_pending:
            stalled_admissions.append({
                "phone": lead.phone,
                "name": lead.name or "Unknown",
                "staff": assigned_staff_norm,
                "score": score,
                "days_silent": days
            })

        if score >= INTELLIGENCE_CONSTANTS["THRESHOLD_WARM"] and days > 14:
            recovery_queue.append({
                "phone": lead.phone,
                "name": lead.name or "Unknown",
                "staff": normalize_staff_name(lead.assigned_staff or ""),
                "course": lead.course or "—",
                "days_silent": days,
                "score": score
            })
            
        # Recommendation Logic: Activity > 24h ago AND no open task
        if days > 1 and phone_open_tasks.get(lead.phone, 0) == 0:
            recommendations.append({
                "phone": lead.phone,
                "name": lead.name or "Unknown",
                "days": days,
                "score": score
            })

    recovery_queue.sort(key=lambda x: x["score"], reverse=True)
    recommendations.sort(key=lambda x: x["days"], reverse=True)
    unassigned_hot.sort(key=lambda x: x["score"], reverse=True)
    stalled_admissions.sort(key=lambda x: x["score"], reverse=True)
    
    # Compute completion rates
    for s, data in staff_productivity.items():
        data["completion_rate"] = round((data["completed"] / data["created"] * 100), 1) if data["created"] > 0 else 0.0

    return {
        "aging": aging,
        "recovery_queue": recovery_queue[:20],
        "recommendations": recommendations[:20],
        "unassigned_hot": unassigned_hot[:20],
        "stalled_admissions": stalled_admissions[:20],
        "productivity": staff_productivity
    }



# ── Phase 9.2B Helpers & Routes ─────────────────────────────────────────────

def calculate_workload_scoring(tenant_id=None):
    """
    Returns a dictionary of staff name -> Workload Score.
    Score = (Lead * 1) + (Contacted * 2) + (Interested * 3)
    Only considers active staff.
    """
    from app.models import ConversationState
    from app.extensions import db
    
    # Phase RC2.2D Batch 1: candidate set is now the tenant's own active staff.
    #
    # THE ALGORITHM IS UNCHANGED. Same weights, same grouping, same join key:
    # normalize_staff_name() is still applied to the display name, so the
    # mapping into `scores` is byte-identical to what the JSON produced. Only
    # WHO is eligible changed. Oxford's scores must therefore come out
    # numerically identical — that equality is the batch's acceptance test.
    #
    # `tid` is resolved once and used for BOTH the staff list and the lead
    # query, so the two can never come from different tenants. Behaviour is
    # unchanged for every existing caller: all of them pass nothing, and
    # _actor_tenant_id() yields exactly what tenant_filter() already fell back
    # to (current_user.tenant_id, or the impersonated tenant for a SUPER_ADMIN).
    #
    # A SUPER_ADMIN who is NOT impersonating resolves to None, so the staff set
    # is empty and no recommendations are produced — fail closed, per the
    # approved operator decision, rather than ranking staff across tenants.
    # Resolved defensively: _actor_tenant_id() touches current_user, which does
    # not exist outside a request. Every caller today is in-request, but this
    # helper previously read a FILE and so worked anywhere — turning that into
    # an AttributeError would be a worse failure than the one being fixed.
    # Unresolvable tenant yields no candidates, which is the approved
    # fail-closed outcome.
    tid = tenant_id
    if not tid:
        try:
            tid = _actor_tenant_id()
        except Exception:                                   # noqa: BLE001
            tid = None
    active_staff = {normalize_staff_name(name): name
                    for name in staff_service.active_display_names(tid)}

    workload_query = tenant_filter(db.session.query(
        ConversationState.assigned_staff,
        ConversationState.lead_status,
        db.func.count(ConversationState.id)
    ), ConversationState, tid).group_by(ConversationState.assigned_staff, ConversationState.lead_status).all()
    
    scores = {norm_name: 0 for norm_name in active_staff.keys()}
    
    weights = {
        "Lead": 1,
        "Contacted": 2,
        "Interested": 3,
        "Enrolled": 0,  # Inactive workload
        "Dropped": 0    # Inactive workload
    }
    
    for staff_name, status, count in workload_query:
        if not staff_name: continue
        norm_name = normalize_staff_name(staff_name)
        if norm_name in scores:
            weight = weights.get(status, 1)
            scores[norm_name] += (count * weight)
            
    return scores, active_staff

def get_staff_recommendations(limit=3):
    """
    Returns a list of recommended active staff members based on lowest workload score.
    Format: [{"name": "...", "score": ...}, ...]
    """
    scores, active_staff = calculate_workload_scoring()
    
    # Sort by lowest score
    sorted_staff = sorted([{"name": display_name, "score": scores[norm_name]} 
                           for norm_name, display_name in active_staff.items()],
                          key=lambda x: x["score"])
                          
    return sorted_staff[:limit]


@admin_bp.route("/crm/staff-workload", methods=["GET"])
def crm_staff_workload():
    if not check_auth():
        return _deny()
        
    from app.models import ConversationState
    from app.extensions import db

    # ── Phase RC2.2F (H2): impersonation-aware tenant resolution ───────────
    #
    # This was `getattr(current_user, 'tenant_id', None)`, which is NULL for a
    # SUPER_ADMIN. tenant_filter() has an explicit SUPER_ADMIN branch honouring
    # session['impersonate_tenant_id'], so while impersonating this screen
    # rendered the impersonated tenant's LEADS beside an EMPTY staff roster —
    # the staff read fail-closed on a NULL tenant while the lead read did not.
    #
    # _actor_tenant_id() honours impersonation, so both halves of the page now
    # resolve to the same tenant. For every other role it returns exactly what
    # current_user.tenant_id returned, so no other behaviour changes.
    _tid = _actor_tenant_id()
    workload_query = tenant_filter(db.session.query(
        ConversationState.assigned_staff,
        ConversationState.lead_status,
        db.func.count(ConversationState.id)
    ), ConversationState, _tid).group_by(ConversationState.assigned_staff, ConversationState.lead_status).all()
    
    # Phase RC2.2D Batch 2: tenant-scoped roster.
    #
    # as_registry(), NOT active_display_names(): this screen deliberately
    # iterates EVERY staff member and renders `active` as a column, so an
    # inactive staff member's historical workload stays visible. Switching to
    # the active-only helper would silently drop those rows.
    #
    # The grouping is untouched: still keyed by normalize_staff_name(), which
    # is what joins these rows to ConversationState.assigned_staff above.
    registry = staff_service.as_registry(_tid)
    staff_data = {}

    for code, data in registry.items():
        name = data.get("display_name", "")
        norm_name = normalize_staff_name(name)
        if norm_name not in staff_data:
            staff_data[norm_name] = {
                "display_name": name,
                "active": data.get("active", False),
                "statuses": {"Lead": 0, "Contacted": 0, "Interested": 0, "Enrolled": 0, "Dropped": 0, "Other": 0},
                "total_active": 0
            }
            
    for staff_name, status, count in workload_query:
        if not staff_name: continue
        norm_name = normalize_staff_name(staff_name)
        
        if norm_name not in staff_data:
            staff_data[norm_name] = {
                "display_name": staff_name,
                "active": False,
                "statuses": {"Lead": 0, "Contacted": 0, "Interested": 0, "Enrolled": 0, "Dropped": 0, "Other": 0},
                "total_active": 0
            }
            
        status = status or "Lead"
        if status in staff_data[norm_name]["statuses"]:
            staff_data[norm_name]["statuses"][status] += count
        else:
            staff_data[norm_name]["statuses"]["Other"] += count
            
        if status in ["Lead", "Contacted", "Interested"]:
            staff_data[norm_name]["total_active"] += count
            
    # Sort by active workload
    workload_list = list(staff_data.values())
    workload_list.sort(key=lambda x: (not x["active"], -x["total_active"]))
    
    return render_template(
        "crm_staff_workload.html",
        key=request.args.get("key", ""),
        workload_list=workload_list
    )


@admin_bp.route("/crm/leads/unassigned", methods=["GET"])
@admin_required
def crm_unassigned_leads():
    if not check_auth():
        return _deny()
        
    from app.models import ConversationState
    from sqlalchemy import or_
    
    # Phase 10.3: paginated with the same paginate()/PAGE_SIZE=25 pattern as
    # /crm/leads. Previously loaded every unassigned lead unbounded. `total`
    # now comes from pagination.total (a COUNT) rather than len() of a fully
    # materialised list, so the count stays correct across pages.
    PAGE_SIZE = 25
    page = max(1, request.args.get("page", 1, type=int))
    # Phase H4-b: consistency. _tid here feeds ONLY tenant_query(), whose
    # SUPER_ADMIN branch returns before reading the argument, so this
    # changes no behaviour -- it makes all four/seven routes resolve the
    # tenant by one rule.
    _tid = _actor_tenant_id()
    pagination = tenant_query(ConversationState, _tid).filter(
        or_(ConversationState.assigned_staff.is_(None), ConversationState.assigned_staff == '')
    ).order_by(ConversationState.lead_score.desc()).paginate(
        page=page, per_page=PAGE_SIZE, error_out=False)
    unassigned = pagination.items

    recommendations = get_staff_recommendations(limit=5)

    # Phase RC2.2D Batch 1: tenant-scoped picker. get_staff_recommendations()
    # above is migrated in the same batch on purpose — a page whose dropdown
    # lists this tenant's staff while the panel beside it recommends Oxford's
    # would be worse than one that is uniformly stale.
    active_staff = staff_service.active_display_names(_actor_tenant_id())

    return render_template(
        "crm_unassigned_leads.html",
        key=request.args.get("key", ""),
        leads=unassigned,
        pagination=pagination,
        recommendations=recommendations,
        active_staff=active_staff,
        # Phase H3-1B-d: surfaces a rejected assignment. This screen had no
        # error channel at all — it is standalone (no base template) and does
        # not render flashed messages, so the err= querystring convention used
        # by crm_lead_new / crm_lead_detail is the one that works here.
        err=request.args.get("err", ""),
        total=pagination.total
    )

@admin_bp.route("/crm/leads/unassigned/assign", methods=["POST"])
@admin_required
def crm_unassigned_assign():
    if not check_auth():
        return _deny()
        
    phone = request.form.get("phone")
    target_staff = request.form.get("target_staff", "").strip()
    key = request.args.get("key", "")
    
    if not phone or not target_staff:
        return redirect(url_for("admin.crm_unassigned_leads", key=key))
        
    from app.models import ConversationState
    from app.extensions import db
    from app.services.log_service import log_lead_event
    import json

    # Phase 14B.2 (C1): scoped to the acting tenant. phone is NOT unique across
    # tenants — the same person may be a lead at two institutes — so an
    # unscoped filter_by(phone=...).first() returned whichever row the database
    # yielded first and could reassign ANOTHER tenant's lead.
    _tid = _actor_tenant_id()
    if not _tid:
        return redirect(url_for("admin.crm_unassigned_leads", key=key))

    # Phase H3-1B-d: the eighth and last assigned_staff write path to be
    # validated. REJECT, matching the other form paths (H3-1B-a) — the target
    # comes from a <select> built from active_display_names(), so a value that
    # does not resolve means a stale page or a tampered POST, not a typo. CSV
    # import warns instead (H3-1B-c); different input, different UX.
    #
    # Validated BEFORE the lookup, so no mutation can precede a rejection.
    # crm_lead_update needs a rollback here because it assigns other fields
    # first; this route touches nothing beforehand, so there is no dirty state
    # to unwind and adding one would be cargo-cult.
    #
    # This path was missed by H3-1B-a: the H3-1B discovery report listed it in
    # the write-path inventory but omitted it from the reject/warn table, so
    # the approved scope covered four form/JSON paths instead of five.
    _owner = staff_identity_service.resolve_assignment(_tid, target_staff)
    if not _owner.ok:
        return redirect(url_for(
            "admin.crm_unassigned_leads", key=key,
            err=f"'{_owner.value}' is not a current staff member of this "
                f"institute — choose from the list."))

    lead = tenant_query(ConversationState, _tid).filter_by(phone=phone).first()
    # `.value`, not `.canonical` — store the operator's own spelling, exactly
    # as before. Canonicalisation is the read layer's job (RC2.3E).
    if lead and lead.assigned_staff != _owner.value:
        old_staff = lead.assigned_staff
        lead.assigned_staff = _owner.value
        _sync_assigned_user(lead, _tid)          # Phase RC2.3D dual-write

        log_lead_event(tenant_id=_actor_tenant_id(),
            phone=lead.phone,
            event_type="LEAD_REASSIGNED",
            event_data=json.dumps({
                "from": old_staff or "Unassigned",
                "to": _owner.value,
                "by": "Admin UX Assignment"
            })
        )
        db.session.commit()
        
    return redirect(url_for("admin.crm_unassigned_leads", key=key))

@admin_bp.route("/crm/leads/unassigned/auto-assign-preview", methods=["POST"])
@admin_required
def crm_auto_assign_preview():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
        
    from app.models import ConversationState
    from sqlalchemy import or_

    # Phase 14B.2 (C1): scoped to the acting tenant. Unscoped, this returned
    # EVERY tenant's unassigned leads and disclosed their names, phone numbers
    # and scores to any tenant admin — a direct cross-customer data leak, not
    # merely a wrong assignment target.
    _tid = _actor_tenant_id()
    if not _tid:
        return jsonify({"error": "Unauthorized"}), 401

    unassigned = tenant_query(ConversationState, _tid).filter(
        or_(ConversationState.assigned_staff.is_(None), ConversationState.assigned_staff == '')
    ).order_by(ConversationState.lead_score.desc()).all()

    if not unassigned:
        return jsonify({"error": "No unassigned leads found"}), 400
        
    scores, active_staff = calculate_workload_scoring()
    
    if not active_staff:
        return jsonify({"error": "No active staff found"}), 400
        
    preview_data = []
    
    # Simulate workload distribution in memory
    for lead in unassigned:
        # Find staff with lowest score
        best_staff = min(active_staff.values(), key=lambda name: scores.get(normalize_staff_name(name), 0))
        
        preview_data.append({
            "phone": lead.phone,
            "name": lead.name,
            "score": lead.lead_score,
            "target_staff": best_staff
        })
        
        # Increment score simulating assignment (using "Lead" weight of 1)
        scores[normalize_staff_name(best_staff)] += 1
        
    return jsonify({"preview": preview_data})

@admin_bp.route("/crm/leads/unassigned/auto-assign-confirm", methods=["POST"])
@admin_required
def crm_auto_assign_confirm():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
        
    data = request.get_json(silent=True) or {}
    assignments = data.get("assignments", [])
    
    if not assignments:
        return jsonify({"error": "No assignments provided"}), 400
        
    from app.models import ConversationState
    from app.extensions import db
    from app.services.log_service import log_lead_event
    import json

    # Phase 14B.2 (C1): scoped to the acting tenant. The phones arrive in the
    # request body and are never trusted — an unscoped per-phone lookup let a
    # crafted (or merely mistaken) payload reassign another tenant's leads.
    _tid = _actor_tenant_id()
    if not _tid:
        return jsonify({"error": "Unauthorized"}), 401

    # ── Phase H3-1B-a: validate EVERY owner before writing ANY of them ────
    #
    # This endpoint takes target_staff from the request body — the client's
    # echo of the preview, not the server's computation — so it is the easiest
    # of the eight write paths to abuse, and the only one carrying a distinct
    # owner per row.
    #
    # Validated as a batch, up front: a per-row reject mid-loop would leave
    # some leads reassigned and others not, with a 400 telling the caller
    # nothing about which. All-or-nothing is the only honest contract for a
    # bulk write.
    _rejects = []
    for _a in assignments:
        _v = staff_identity_service.resolve_assignment(_tid, _a.get("target_staff"))
        if not _v.ok:
            _rejects.append({"phone": _a.get("phone"), "target_staff": _v.value,
                             "reason": _v.reason})
    if _rejects:
        return jsonify({
            "error": "One or more target staff are not current staff members "
                     "of this institute — nothing was assigned.",
            "rejected": _rejects,
        }), 400

    updated_count = 0
    for assign in assignments:
        phone = assign.get("phone")
        target_staff = assign.get("target_staff")

        if phone and target_staff:
            lead = tenant_query(ConversationState, _tid).filter_by(phone=phone).first()
            if lead and lead.assigned_staff != target_staff:
                old_staff = lead.assigned_staff
                lead.assigned_staff = target_staff
                _sync_assigned_user(lead, _tid)  # Phase RC2.3D dual-write

                log_lead_event(tenant_id=_actor_tenant_id(),
                    phone=lead.phone,
                    event_type="LEAD_REASSIGNED",
                    event_data=json.dumps({
                        "from": old_staff or "Unassigned",
                        "to": target_staff,
                        "by": "Admin Auto Assignment"
                    })
                )
                updated_count += 1
                
    db.session.commit()
    
    return jsonify({"success": True, "updated_count": updated_count})

@admin_bp.route("/crm/reassignment-center", methods=["GET"])
def crm_reassignment_center():
    if not check_auth():
        return _deny()

    # Phase RC2.2D Batch 1: tenant-scoped picker. Must render the SAME
    # recommendation set as /crm/leads/unassigned for a given tenant — they
    # share get_staff_recommendations(), and a disagreement between the two is
    # the batch's designated rollback trigger.
    active_staff = staff_service.active_display_names(_actor_tenant_id())
    
    recommendations = get_staff_recommendations(limit=5)
    
    return render_template(
        "crm_reassignment_center.html",
        active_staff=active_staff,
        recommendations=recommendations,
        msg=request.args.get("msg", ""),
        err=request.args.get("err", "")
    )

@admin_bp.route("/crm/reassignment-center/preview", methods=["POST"])
@admin_required
def crm_reassignment_preview():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
        
    data = request.get_json(silent=True) or {}
    phones = data.get("phones", [])
    target_staff = data.get("target_staff", "").strip()
    
    if not phones or not target_staff:
        return jsonify({"error": "Phones and Target Staff are required"}), 400
        
    from app.models import ConversationState

    # Phase 14B.2 (C1): scoped to the acting tenant. The phone list is supplied
    # by the caller, so an unscoped IN() returned any tenant's matching leads —
    # this endpoint RENDERS name, staff and stage, so it disclosed them.
    _tid = _actor_tenant_id()
    if not _tid:
        return jsonify({"error": "Unauthorized"}), 401

    leads = tenant_query(ConversationState, _tid).filter(
        ConversationState.phone.in_(phones)).all()

    preview_data = []
    for lead in leads:
        preview_data.append({
            "phone": lead.phone,
            "name": lead.name,
            "old_staff": lead.assigned_staff or "Unassigned",
            "new_staff": target_staff,
            "stage": lead.stage,
            "score": lead.lead_score
        })
        
    return jsonify({"preview": preview_data, "target_staff": target_staff})

@admin_bp.route("/crm/reassignment-center/confirm", methods=["POST"])
@admin_required
def crm_reassignment_confirm():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
        
    data = request.get_json(silent=True) or {}
    phones = data.get("phones", [])
    target_staff = data.get("target_staff", "").strip()
    
    if not phones or not target_staff:
        return jsonify({"error": "Phones and Target Staff are required"}), 400
        
    from app.models import ConversationState
    from app.extensions import db
    from app.services.log_service import log_lead_event
    import json

    # Phase 14B.2 (C1): scoped to the acting tenant — the write counterpart of
    # the preview above. Unscoped, a caller could reassign another institute's
    # leads simply by listing their phone numbers.
    _tid = _actor_tenant_id()
    if not _tid:
        return jsonify({"error": "Unauthorized"}), 401

    # ── Phase H3-1B-a: reject an owner who is not this tenant's staff ──────
    #
    # One target for the whole batch, so one check before the loop. Nothing is
    # written when it fails, which is what makes the 400 truthful.
    _owner = staff_identity_service.resolve_assignment(_tid, target_staff)
    if not _owner.ok:
        return jsonify({
            "error": f"'{_owner.value}' is not a current staff member of this "
                     f"institute — nothing was reassigned.",
            "target_staff": _owner.value,
            "reason": _owner.reason,
        }), 400

    leads = tenant_query(ConversationState, _tid).filter(
        ConversationState.phone.in_(phones)).all()

    updated_count = 0
    for lead in leads:
        old_staff = lead.assigned_staff
        if old_staff != target_staff:
            lead.assigned_staff = target_staff
            _sync_assigned_user(lead, _tid)      # Phase RC2.3D dual-write
            updated_count += 1
            # Add LEAD_REASSIGNED event
            log_lead_event(tenant_id=_actor_tenant_id(),
                phone=lead.phone,
                event_type="LEAD_REASSIGNED",
                event_data=json.dumps({
                    "from": old_staff or "Unassigned",
                    "to": target_staff,
                    "by": "Admin Bulk Reassignment"
                })
            )
            
    db.session.commit()
    
    return jsonify({"success": True, "updated_count": updated_count})





def get_all_tasks(tenant_id=None):
    """Unified task reader — Phase 16.5A7-B (B2).

    The `tasks` table is the System of Record. Tasks created BEFORE Phase 16.5A7
    exist only as LeadEvents (no Task row) and are replayed here so they keep
    appearing — that is the compatibility layer. A Task row always wins over a
    legacy replay of the same task_uid.

    Before 16.5A7-B this replayed LeadEvents exclusively, so the Task table had
    no reader: admin edits were invisible, deletes left zombies, and priority /
    IN_PROGRESS / staff_notes never rendered (16.5A7-A audit, B2).

    The return contract is UNCHANGED — (open_tasks, completed_tasks) with the
    same dict keys — so all four callers (dashboard KPIs, /crm/tasks/my,
    /crm/tasks/admin, staff performance) and their templates keep working. New
    keys are purely additive: id, task_status, priority, notes, staff_notes,
    is_legacy.
    """
    from app.models import LeadEvent, ConversationState, Task
    from datetime import datetime
    import json

    leads = tenant_query(ConversationState, tenant_id).all()
    lead_map = {l.phone: l for l in leads}

    def _lead_name(phone):
        lead = lead_map.get(phone)
        return (getattr(lead, "name", None) or "Unknown") if lead else "Unknown"

    tasks = {}

    # ── 1. Task table — the System of Record ────────────────────────────
    for t in tenant_query(Task, tenant_id).all():
        tasks[t.task_uid] = {
            "task_id":      t.task_uid,
            "id":           t.id,            # real PK for the 16.5A7 routes
            "phone":        t.lead_phone,
            "lead_name":    _lead_name(t.lead_phone),
            "task":         t.title,
            "due_date":     t.due_date or "",
            "staff":        t.assigned_staff or "Unassigned",
            "created_by":   t.created_by or "",
            "created_at":   t.created_at,
            # `status` keeps the legacy OPEN/COMPLETED contract the templates
            # and the open/completed split rely on; IN_PROGRESS is surfaced
            # separately via task_status so it can render without breaking them.
            "status":       "COMPLETED" if t.status == "COMPLETED" else "OPEN",
            "task_status":  t.status,
            "priority":     t.priority,
            "notes":        t.notes,
            "staff_notes":  t.staff_notes,
            "completed_by": t.completed_by,
            "completed_at": t.completed_at,
            "is_legacy":    False,
        }

    # ── 2. Compatibility layer: pre-16.5A7 tasks with no Task row ───────
    events = (tenant_query(LeadEvent, tenant_id)
              .filter(LeadEvent.event_type.in_(["FOLLOW_UP_TASK",
                                                "FOLLOW_UP_COMPLETED"]))
              .order_by(LeadEvent.id)
              .all())

    legacy = {}
    for ev in events:
        try:
            data = json.loads(ev.event_data or "{}")
        except Exception:
            data = {}
        tid = data.get("task_id")
        if not tid or tid in tasks:
            continue                      # no id, or the Task row already won
        if ev.event_type == "FOLLOW_UP_TASK":
            legacy[tid] = {
                "task_id":      tid,
                "id":           None,     # legacy tasks have no Task row
                "phone":        ev.phone,
                "lead_name":    _lead_name(ev.phone),
                "task":         data.get("task", ""),
                "due_date":     data.get("due_date", ""),
                "staff":        data.get("staff", "Unassigned"),
                "created_by":   data.get("created_by", ""),
                "created_at":   ev.created_at,
                "status":       "OPEN",
                "task_status":  "OPEN",
                "priority":     "NORMAL",   # legacy payload has no priority
                "notes":        data.get("notes"),
                "staff_notes":  None,
                "completed_by": None,
                "completed_at": None,
                "is_legacy":    True,
            }

    for ev in events:
        if ev.event_type != "FOLLOW_UP_COMPLETED":
            continue
        try:
            data = json.loads(ev.event_data or "{}")
        except Exception:
            continue
        tid = data.get("task_id")
        if tid in legacy:
            legacy[tid]["status"] = "COMPLETED"
            legacy[tid]["task_status"] = "COMPLETED"
            legacy[tid]["completed_by"] = data.get("completed_by", "")
            legacy[tid]["completed_at"] = ev.created_at

    tasks.update(legacy)

    open_tasks = []
    completed_tasks = []
    
    today_dt = datetime.now()
    
    for t in tasks.values():
        if t["status"] == "OPEN":
            due = t.get("due_date", "")
            if due:
                try:
                    due_dt = datetime.strptime(due, "%Y-%m-%d")
                    diff = (today_dt.date() - due_dt.date()).days
                    if diff > 0:
                        if diff >= 7:
                            t["severity"] = "7+ Days Overdue"
                        elif diff >= 4:
                            t["severity"] = "4-7 Days Overdue"
                        else:
                            t["severity"] = "1-3 Days Overdue"
                        t["is_overdue"] = True
                        t["is_today"] = False
                        t["days_diff"] = diff
                    elif diff == 0:
                        t["severity"] = "Due Today"
                        t["is_overdue"] = False
                        t["is_today"] = True
                        t["days_diff"] = 0
                    else:
                        t["severity"] = "Upcoming"
                        t["is_overdue"] = False
                        t["is_today"] = False
                        t["days_diff"] = diff
                except:
                    t["severity"] = "Unknown"
                    t["is_overdue"] = False
                    t["is_today"] = False
                    t["days_diff"] = 0
            else:
                t["severity"] = "No Due Date"
                t["is_overdue"] = False
                t["is_today"] = False
                t["days_diff"] = 0
                
            open_tasks.append(t)
        else:
            completed_tasks.append(t)
            
    # Sort: overdue first (highest diff), then today, then upcoming
    open_tasks.sort(key=lambda x: x.get("days_diff", 0), reverse=True)
    completed_tasks.sort(key=lambda x: x.get("completed_at", datetime.min), reverse=True)
            
    return open_tasks, completed_tasks

def _actor_tenant_id():
    """Resolve the acting user's tenant_id.

    Phase 16.5A7 (ADR-021): NEVER falls back to _get_default_tenant_id()
    (Tenant.query.first()), which resolves to an arbitrary unrelated tenant and
    had already mis-filed 18 production lead_event rows. Returns None when the
    tenant cannot be resolved; callers must refuse to write.

    Phase 16.5A7-B: honours session['impersonate_tenant_id'] for SUPER_ADMIN,
    matching tenant_query() (admin.py:73). A SUPER_ADMIN has tenant_id = NULL,
    so without this it could neither create tasks nor read notifications even
    while impersonating a tenant.
    """
    if not current_user.is_authenticated:
        return None
    if getattr(current_user, 'role', None) == 'SUPER_ADMIN':
        impersonated = session.get('impersonate_tenant_id')
        if impersonated:
            return impersonated
        # Not impersonating: no tenant context. Callers refuse to write, which
        # is correct — a platform-level task has no tenant to belong to.
        return None
    return getattr(current_user, 'tenant_id', None)


def _actor_name():
    """Display name of the acting user, normalized like staff names."""
    actor = get_current_actor()
    return normalize_staff_name(actor.get("username") or "Admin")


def _actor_is_admin():
    """True for ADMIN / SUPER_ADMIN, or legacy ADMIN_KEY auth.

    Phase 16.5A7-B (B1): drives task mutation authority. STAFF may only touch
    their own tasks; admins may touch any task in their tenant.
    """
    actor = get_current_actor()
    return actor.get("role") in ("ADMIN", "SUPER_ADMIN")


def _actor_user_id():
    """The acting user's PRIMARY KEY, or None when there isn't one.

    Phase RC2.3E-1 Batch 2: task authorization compares this integer instead
    of two name strings that were never the same field — task.assigned_staff
    holds a DISPLAY LABEL, _actor_name() supplies a USERNAME.

    None for ADMIN_KEY auth, which has no User row. That is safe: the only
    caller treats a missing id as "fall back to the name comparison", and
    ADMIN_KEY resolves to is_admin=True and never reaches the comparison.
    """
    try:
        if current_user.is_authenticated:
            return getattr(current_user, "id", None)
    except Exception:                                       # noqa: BLE001
        pass
    return None


@admin_bp.route("/crm/tasks/create", methods=["POST"])
@admin_required
def crm_tasks_create():
    """Admin creates and assigns a task.

    Phase 16.5A7: @admin_required added — STAFF must not create tasks (ADR-021).
    Previously this route only called check_auth(), so any authenticated staff
    member could create tasks.
    """
    phone = request.form.get("phone") or None
    task_title = request.form.get("task", "").strip()
    notes = request.form.get("notes", "").strip()
    due_date = request.form.get("due_date", "").strip()
    staff = request.form.get("staff", "").strip()
    priority = request.form.get("priority", "NORMAL").strip()

    if not task_title or not due_date:
        return redirect(url_for("admin.crm_lead_detail", phone=phone))

    tenant_id = _actor_tenant_id()
    if not tenant_id:
        logging.error("task create refused: unresolved tenant_id")
        return _deny()

    from app.services import task_service
    try:
        task_service.create_task(
            tenant_id=tenant_id,
            title=task_title,
            created_by=_actor_name(),
            lead_phone=phone,
            notes=notes or None,
            due_date=due_date,
            priority=priority,
            assigned_staff=normalize_staff_name(staff) if staff else None,
        )
    except task_service.TaskError as e:
        # Phase H3-1B-b: SURFACE the refusal instead of only logging it.
        #
        # This handler previously logged and fell through to the same redirect
        # as success, so a rejected create looked to the operator like a task
        # that simply never appeared — no message, nothing to act on. Harmless
        # while the only refusals were "title required" (which the form's own
        # `required` attribute already prevents), but H3-1B-b adds a refusal a
        # real operator can trigger: assigning to someone who is no longer
        # staff. A silent failure there is worse than the bug being fixed.
        #
        # crm_lead_detail renders `err`, and its task form is the ONLY template
        # posting to this route — every UI-reachable create carries a phone, so
        # the operator sees the message. The no-phone branch is reachable only
        # by a crafted POST; it still carries err= (and the log line), but
        # crm_admin_tasks does not render it, and adding that rendering for an
        # unreachable path would be scope this phase has not earned.
        logging.warning("task create rejected tenant=%s: %s", tenant_id, e)
        if phone:
            return redirect(url_for("admin.crm_lead_detail", phone=phone,
                                    err=str(e)))
        return redirect(url_for("admin.crm_admin_tasks", err=str(e)))

    if phone:
        return redirect(url_for("admin.crm_lead_detail", phone=phone))
    return redirect(url_for("admin.crm_admin_tasks"))


@admin_bp.route("/crm/tasks/<int:task_id>/edit", methods=["POST"])
@admin_required
def crm_tasks_edit(task_id):
    """Admin edits a task: title, notes, due date, priority, assignee."""
    tenant_id = _actor_tenant_id()
    if not tenant_id:
        return _deny()

    from app.services import task_service

    def field(name):
        # Only fields actually present in the form are changed.
        return request.form.get(name) if name in request.form else None

    staff = field("staff")
    try:
        task = task_service.update_task(
            tenant_id=tenant_id, task_id=task_id, actor=_actor_name(),
            title=field("task"), notes=field("notes"),
            due_date=field("due_date"), priority=field("priority"),
            assigned_staff=(normalize_staff_name(staff)
                            if staff not in (None, "") else staff),
        )
    except task_service.TaskError as e:
        logging.warning("task edit rejected tenant=%s task=%s: %s",
                        tenant_id, task_id, e)
        return jsonify({"error": str(e)}), 400

    if request.form.get("phone"):
        return redirect(url_for("admin.crm_lead_detail",
                                phone=request.form.get("phone")))
    return redirect(url_for("admin.crm_admin_tasks"))


@admin_bp.route("/crm/tasks/<int:task_id>/delete", methods=["POST"])
@admin_required
def crm_tasks_delete(task_id):
    """Admin deletes a task. Notifications are detached, not cascaded."""
    tenant_id = _actor_tenant_id()
    if not tenant_id:
        return _deny()

    from app.services import task_service
    try:
        task_service.delete_task(tenant_id, task_id, _actor_name())
    except task_service.TaskError as e:
        return jsonify({"error": str(e)}), 404

    if request.is_json:
        return jsonify({"success": True})
    return redirect(url_for("admin.crm_admin_tasks"))


@admin_bp.route("/crm/tasks/<int:task_id>/update", methods=["POST"])
def crm_tasks_staff_update(task_id):
    """Staff updates progress: status and/or notes. No reassign, no retitle."""
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    tenant_id = _actor_tenant_id()
    if not tenant_id:
        return jsonify({"error": "Tenant unresolved"}), 400

    payload = request.get_json(silent=True) if request.is_json else request.form
    payload = payload or {}

    from app.services import task_service
    try:
        task = task_service.staff_update(
            tenant_id=tenant_id, task_id=task_id, actor=_actor_name(),
            status=payload.get("status"), staff_notes=payload.get("staff_notes"),
            is_admin=_actor_is_admin(),
            actor_user_id=_actor_user_id(),
        )
    except task_service.TaskForbidden as e:
        # B1: staff attempting to modify another staff member's task.
        logging.warning("task mutation denied tenant=%s task=%s actor=%s: %s",
                        tenant_id, task_id, _actor_name(), e)
        if request.is_json:
            return jsonify({"error": str(e)}), 403
        return _deny()
    except task_service.TaskError as e:
        return jsonify({"error": str(e)}), 400

    if request.is_json:
        return jsonify({"success": True, "task": task.to_dict()})
    return redirect(request.referrer or url_for("admin.crm_my_tasks"))
@admin_bp.route("/crm/tasks/complete", methods=["POST"])
def crm_tasks_complete():
    # Supports both Form (from lead detail) and JSON (from dashboards)
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
        
    is_json = request.is_json
    
    if is_json:
        data = request.get_json(silent=True) or {}
        task_id = data.get("task_id")
        phone = data.get("phone")
        completed_by = data.get("completed_by", "Admin")
    else:
        task_id = request.form.get("task_id")
        phone = request.form.get("phone")
        completed_by = "Admin"
        
    # Phase 16.5A7-B (B2): `phone` is no longer required up-front. A standalone
    # Task has no lead, and demanding a phone made it impossible to complete
    # from the UI. The legacy event path below still requires one.
    if not task_id:
        if is_json:
            return jsonify({"error": "Missing parameters"}), 400
        else:
            return redirect(url_for("admin.crm_lead_detail", phone=phone))

    from app.models import LeadEvent, Task
    from app.services.log_service import log_lead_event
    import json

    # Phase 16.5A7: the acting user, not a hardcoded "Admin" (ADR-021).
    completed_by = _actor_name()
    _tid = _actor_tenant_id()
    if not _tid:
        if is_json:
            return jsonify({"error": "Tenant unresolved"}), 400
        return _deny()

    # Phase 16.5A7 bridge: task_id here is the legacy uuid-hex payload key,
    # which Task mirrors as task_uid. A 16.5A7 task routes through the service
    # (legacy event + TASK_COMPLETED notification). A pre-16.5A7 task has no
    # Task row and still completes via the legacy event path below.
    from app.services import task_service
    task = Task.query.filter_by(tenant_id=_tid, task_uid=task_id).first()
    if task is not None:
        try:
            task_service.complete_task(_tid, task.id, completed_by,
                                       is_admin=_actor_is_admin(),
                                       actor_user_id=_actor_user_id())
        except task_service.TaskForbidden as e:
            # B1: only the assignee (or an admin) may complete — completed_by is
            # the credit record that feeds staff_productivity.
            logging.warning("task completion denied tenant=%s task=%s actor=%s: %s",
                            _tid, task_id, completed_by, e)
            if is_json:
                return jsonify({"error": str(e)}), 403
            return _deny()
        except task_service.TaskError as e:
            if is_json:
                return jsonify({"error": str(e)}), 400
        if is_json:
            return jsonify({"success": True})
        if phone:
            return redirect(url_for("admin.crm_lead_detail", phone=phone))
        return redirect(request.referrer or url_for("admin.crm_my_tasks"))

    # ── Legacy path: event-sourced task with no Task row ──────────────────
    # Requires a phone: legacy tasks are lead-scoped by construction.
    if not phone:
        if is_json:
            return jsonify({"error": "Missing parameters"}), 400
        return redirect(url_for("admin.crm_my_tasks"))

    # ── Phase 16.5A7-D (B1-R): authorize BEFORE writing ───────────────────
    # 16.5A7-C proved this branch had NO authorization: any staff member could
    # complete a colleague's pre-16.5A7 task and have completed_by set to
    # themselves (HTTP 200), stealing credit and corrupting staff_productivity,
    # which keys on completed_by. The Task path already returned 403; this
    # reaches the SAME decision via task_service.authorize_assignee().
    #
    # The assignee lives in the FOLLOW_UP_TASK payload's `staff` field — there
    # is no Task row to read it from.
    _legacy_assignee = None
    _found_legacy = False
    for _ev in (tenant_query(LeadEvent, _tid)
                .filter_by(phone=phone, event_type="FOLLOW_UP_TASK").all()):
        try:
            _p = json.loads(_ev.event_data or "{}")
        except (ValueError, TypeError):
            continue
        if _p.get("task_id") == task_id:
            _legacy_assignee = _p.get("staff")
            _found_legacy = True
            break

    if not _found_legacy:
        # No Task row and no legacy event: nothing to complete. Refuse rather
        # than log a completion for a task that does not exist in this tenant.
        if is_json:
            return jsonify({"error": "Task not found"}), 404
        return redirect(url_for("admin.crm_lead_detail", phone=phone))

    try:
        # Phase RC2.3E-1 Batch 2: tenant_id makes this FAIL CLOSED when the
        # legacy payload's name does not identify exactly one current staff
        # member, and actor_user_id turns the check into an integer compare
        # so this path reaches the same standard as the Task path.
        task_service.authorize_assignee(_legacy_assignee, completed_by,
                                        _actor_is_admin(),
                                        tenant_id=_tid,
                                        actor_user_id=_actor_user_id())
    except task_service.TaskForbidden as e:
        logging.warning("legacy task completion denied tenant=%s task=%s "
                        "actor=%s: %s", _tid, task_id, completed_by, e)
        if is_json:
            return jsonify({"error": str(e)}), 403
        return _deny()

    # Duplicate completion protection
    existing = tenant_query(LeadEvent, _tid).filter_by(phone=phone, event_type="FOLLOW_UP_COMPLETED").all()
    already_completed = False
    for ev in existing:
        try:
            d = json.loads(ev.event_data or "{}")
            if d.get("task_id") == task_id:
                already_completed = True
                break
        except:
            pass

    if not already_completed:
        # tenant_id is the ACTOR's tenant. Previously _get_default_tenant_id()
        # (Tenant.query.first()) mis-filed these rows under an unrelated
        # tenant, making completions invisible (ADR-021).
        log_lead_event(tenant_id=_tid,
            phone=phone,
            event_type="FOLLOW_UP_COMPLETED",
            event_data=json.dumps({
                "task_id": task_id,
                "completed_by": completed_by
            })
        )

    if is_json:
        return jsonify({"success": True})
    else:
        return redirect(url_for("admin.crm_lead_detail", phone=phone))


# ══ Phase 16.5A7 — Notification Centre (ADR-021) ═══════════════════════════

@admin_bp.route("/crm/notifications/unread-count", methods=["GET"])
def crm_notifications_unread_count():
    """Bell badge count. Polled by the sidebar on every CRM page."""
    if not check_auth():
        return jsonify({"count": 0}), 401
    tenant_id = _actor_tenant_id()
    if not tenant_id:
        return jsonify({"count": 0})
    from app.services import notification_service
    return jsonify({"count": notification_service.unread_count(
        tenant_id, _actor_name())})


@admin_bp.route("/crm/notifications/recent", methods=["GET"])
def crm_notifications_recent():
    """Bell dropdown payload: newest notifications + unread count."""
    if not check_auth():
        return jsonify({"items": [], "count": 0}), 401
    tenant_id = _actor_tenant_id()
    if not tenant_id:
        return jsonify({"items": [], "count": 0})
    from app.services import notification_service
    me = _actor_name()
    rows = notification_service.recent(tenant_id, me, limit=10)
    return jsonify({
        "items": [r.to_dict() for r in rows],
        "count": notification_service.unread_count(tenant_id, me),
    })


@admin_bp.route("/crm/notifications", methods=["GET"])
def crm_notifications():
    """Full notification centre page."""
    if not check_auth():
        return _deny()
    tenant_id = _actor_tenant_id()
    from app.services import notification_service
    me = _actor_name()
    rows = (notification_service.recent(tenant_id, me, limit=100)
            if tenant_id else [])
    return render_template(
        "crm_notifications.html",
        key=request.args.get("key", ""),
        actor=get_current_actor(),
        notifications=rows,
        unread=(notification_service.unread_count(tenant_id, me)
                if tenant_id else 0),
    )


@admin_bp.route("/crm/notifications/<int:notification_id>/read",
                methods=["POST"])
def crm_notifications_read(notification_id):
    """Mark one notification read. Scoped to tenant AND recipient."""
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    tenant_id = _actor_tenant_id()
    if not tenant_id:
        return jsonify({"error": "Tenant unresolved"}), 400
    from app.services import notification_service
    ok = notification_service.mark_read(tenant_id, _actor_name(),
                                        notification_id)
    if request.is_json:
        return jsonify({"success": ok})
    return redirect(request.referrer or url_for("admin.crm_notifications"))


@admin_bp.route("/crm/notifications/read-all", methods=["POST"])
def crm_notifications_read_all():
    """Mark every unread notification for the current user read."""
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    tenant_id = _actor_tenant_id()
    if not tenant_id:
        return jsonify({"error": "Tenant unresolved"}), 400
    from app.services import notification_service
    n = notification_service.mark_all_read(tenant_id, _actor_name())
    if request.is_json:
        return jsonify({"success": True, "marked": n})
    return redirect(request.referrer or url_for("admin.crm_notifications"))

@admin_bp.route("/crm/tasks/my", methods=["GET"])
def crm_my_tasks():
    if not check_auth():
        return _deny()
        
    actor = get_current_actor()
    is_staff = (actor.get("source") == "SESSION" and actor.get("role") == "STAFF")
    
    if is_staff:
        staff_name = normalize_staff_name(actor.get("username", ""))
    else:
        staff_name = normalize_staff_name(request.args.get("staff", ""))
    open_tasks, completed_tasks = get_all_tasks()
    
    if staff_name:
        staff_name_normalized = staff_name.strip().lower()
        open_tasks = [t for t in open_tasks if (t.get("staff") or "").strip().lower() == staff_name_normalized]
        completed_tasks = [t for t in completed_tasks if (t.get("staff") or "").strip().lower() == staff_name_normalized]
        
    overdue = [t for t in open_tasks if t.get("is_overdue")]
    today = [t for t in open_tasks if t.get("is_today")]
    upcoming = [t for t in open_tasks if not t.get("is_overdue") and not t.get("is_today")]
    
    # Filter completed this week (simple implementation: last 7 days)
    from datetime import datetime, timedelta
    week_ago = datetime.now() - timedelta(days=7)
    recent_completed = [t for t in completed_tasks if t.get("completed_at") and t.get("completed_at") > week_ago]
    
    # Phase RC2.2D Batch 3: tenant-scoped task picker. Same sorted list of
    # active display names; only the source changed. This is the consumer that
    # shares its three-line idiom with the Batch 2 pickers and was deliberately
    # left behind then — it is in scope now.
    active_staff = staff_service.active_display_names(_actor_tenant_id())

    return render_template(
        "crm_my_tasks.html",
        key=request.args.get("key", ""),
        staff_name=staff_name,
        active_staff=active_staff,
        overdue=overdue,
        today=today,
        upcoming=upcoming,
        completed=recent_completed
    )

@admin_bp.route("/crm/tasks/admin", methods=["GET"])
def crm_admin_tasks():
    if not check_auth():
        return _deny()
        
    open_tasks, completed_tasks = get_all_tasks()
    
    staff_summary = {}
    # Phase RC2.2D Batch 3: seed the summary from the TENANT's active staff.
    #
    # active_display_names(), not as_registry(): this loop deliberately seeds
    # ACTIVE staff only, and the loop immediately below re-adds anyone who
    # still holds tasks but is no longer active. That two-step is what keeps a
    # deactivated staff member's outstanding tasks visible, and it is
    # preserved exactly — only the seed source changed.
    for _name in staff_service.active_display_names(_actor_tenant_id()):
        staff_summary[_name] = {"pending": 0, "overdue": 0, "completed": 0}

    # Add staff dynamically if they have tasks but are no longer active
    for t in open_tasks + completed_tasks:
        s = t.get("staff", "Unassigned")
        if s not in staff_summary:
            staff_summary[s] = {"pending": 0, "overdue": 0, "completed": 0}
            
    for t in open_tasks:
        s = t.get("staff", "Unassigned")
        staff_summary[s]["pending"] += 1
        if t.get("is_overdue"):
            staff_summary[s]["overdue"] += 1
            
    # filter completed this week
    from datetime import datetime, timedelta
    week_ago = datetime.now() - timedelta(days=7)
    recent_completed = [t for t in completed_tasks if t.get("completed_at") and t.get("completed_at") > week_ago]
    
    for t in recent_completed:
        s = t.get("staff", "Unassigned")
        staff_summary[s]["completed"] += 1
        
    summary_list = [{"name": k, **v} for k, v in staff_summary.items()]
    summary_list.sort(key=lambda x: x["pending"], reverse=True)
    
    kpis = {
        "open": len(open_tasks),
        "today": len([t for t in open_tasks if t.get("is_today")]),
        "overdue": len([t for t in open_tasks if t.get("is_overdue")]),
        "completed_week": len(recent_completed)
    }
    
    return render_template(
        "crm_admin_tasks.html",
        kpis=kpis,
        staff_summary=summary_list
    )


# ── Phase 9.4: Staff Workspace ──────────────────────────────────────────────

@admin_bp.route("/crm/staff-dashboard", methods=["GET"])
def crm_staff_dashboard():
    actor = get_current_actor()
    if not check_auth():
        logging.warning(f"AUTH_FAILURE username={actor['username']} role={actor['role']} source={actor['source']} route=/crm/staff-dashboard")
        return _deny()
    logging.info(f"AUTH_SUCCESS username={actor['username']} role={actor['role']} source={actor['source']} route=/crm/staff-dashboard")
    is_staff = (actor.get("source") == "SESSION" and actor.get("role") == "STAFF")
    
    if is_staff:
        staff_name = normalize_staff_name(actor.get("username", ""))
    else:
        staff_name = normalize_staff_name(request.args.get("staff", ""))
    # Phase RC2.2D Batch 2: tenant-scoped staff picker. Same sorted list of
    # active display names; only the source changed.
    active_staff = staff_service.active_display_names(_actor_tenant_id())
    
    if not staff_name:
        if active_staff:
            staff_name = active_staff[0]
            return redirect(url_for("admin.crm_staff_dashboard", key=request.args.get("key", ""), staff=staff_name))
            
    from app.models import ConversationState, LEAD_TERMINAL_STATUSES
    from sqlalchemy.sql import func

    _tid = _actor_tenant_id()

    # Phase RC2.3E-1 Batch 1b. This screen reads ownership in FIVE places and
    # they must agree, so they are all derived from ONE resolved identity.
    #
    # A STAFF actor is current_user itself; an admin is browsing someone else
    # via ?staff=, so that name is resolved. Unresolvable fails CLOSED —
    # owner_filter(model, None) is false().
    _owner_user = (current_user if is_staff
                   else staff_service.resolve(_tid, staff_name))

    # WHY NOT KEY EVERYTHING BY FK
    # ----------------------------
    # Only the two lead queries below CAN be FK-keyed. The other three read
    # dicts built elsewhere from data that has no FK to key on:
    #   * automation["productivity"] is keyed off LeadEvent JSON payloads
    #     (completed_by / staff) — LeadEvent has no assigned_user_id column
    #     at all, so this can never be FK-keyed without migrating the event
    #     log, which is far outside this batch.
    #   * intel["leaderboard"] entries are keyed by
    #     normalize_staff_name(assigned_staff), a Batch 4 producer.
    #   * get_all_tasks() returns the raw assigned_staff string.
    #
    # So this route BRIDGES the two key-spaces instead of pretending one
    # exists: owner_filter() for the queries, and a canonical display key —
    # derived from the SAME user — for the name-keyed lookups. Both sides then
    # agree whether the flag is on or off, which is what makes this screen
    # safe to flip. Deriving the key from the user rather than from the
    # ?staff= string is also what makes it correct for a staff member whose
    # display_name differs from their username: the producers key on the
    # display label, and until now this route looked itself up by username.
    _display_key = (normalize_staff_name(_owner_user.display_label())
                    if _owner_user is not None else staff_name)
    staff_name_normalized = _display_key.strip().lower()
    # The heading and the picker must show the same person the KPIs describe.
    staff_name = _display_key

    _owned = staff_identity_service.owner_filter(ConversationState, _owner_user)

    leads = tenant_query(ConversationState, _tid).filter(
        _owned,
        ConversationState.lead_status.notin_(tuple(LEAD_TERMINAL_STATUSES))
    ).all()

    my_leads_count = len(leads)
    hot_leads_count = sum(1 for lead in leads if (lead.lead_score or 0) >= 80)

    admissions_count = tenant_query(ConversationState, _tid).filter(
        _owned,
        ConversationState.is_admitted == True
    ).count()

    open_tasks, _ = get_all_tasks(_tid)
    active_tasks_count = sum(1 for t in open_tasks if (t.get("staff") or "").strip().lower() == staff_name_normalized)

    # Phase 9.5: intelligence summary for this staff member
    intel = calculate_intelligence()
    # Find this staff's rank in leaderboard
    staff_rank = None
    staff_lb = None
    for i, entry in enumerate(intel["leaderboard"]):
        # _display_key, not the raw ?staff= string: leaderboard rows describe
        # display labels, so a username would never match for a renamed staff
        # member. Both sides are normalized because calculate_intelligence()
        # stores the RAW active_staff string in "name" while keying its own
        # tallies on normalize_staff_name() — so an all-caps display label
        # would fail a bare == against the normalized key.
        if normalize_staff_name(entry["name"]) == _display_key:
            staff_rank = i + 1
            staff_lb = entry
            break

    kpis = {
        "my_leads": my_leads_count,
        "hot_leads": hot_leads_count,
        "active_tasks": active_tasks_count,
        "admissions": admissions_count
    }
    
    # Phase 9.6
    from app.models import ConversationState, LeadEvent
    intel_event_types = ["FOLLOW_UP_TASK", "FOLLOW_UP_COMPLETED"]
    auto_events = tenant_query(LeadEvent, _tid).filter(LeadEvent.event_type.in_(intel_event_types)).all()
    leads = tenant_query(ConversationState, _tid).all()
    automation = calculate_automation_intelligence(leads, auto_events)
    # Same key-space as the leaderboard above: LeadEvent payload names,
    # normalized. See the note by _display_key for why this cannot be a FK.
    my_productivity = automation["productivity"].get(_display_key, {"created": 0, "completed": 0, "open": 0, "overdue": 0, "completion_rate": 0.0})


    return render_template(
        "crm_staff_dashboard.html",
        key=request.args.get("key", ""),
        staff_name=staff_name,
        active_staff=active_staff,
        kpis=kpis,
        intel=intel,
        staff_rank=staff_rank,
        staff_lb=staff_lb,
        my_productivity=my_productivity,
    )

@admin_bp.route("/crm/my-leads", methods=["GET"])
def crm_my_leads():
    if not check_auth():
        return _deny()
        
    actor = get_current_actor()
    is_staff = (actor.get("source") == "SESSION" and actor.get("role") == "STAFF")
    
    if is_staff:
        staff_name = normalize_staff_name(actor.get("username", ""))
    else:
        staff_name = normalize_staff_name(request.args.get("staff", ""))
    # Phase RC2.2D Batch 2: tenant-scoped staff picker. Same sorted list of
    # active display names; only the source changed.
    active_staff = staff_service.active_display_names(_actor_tenant_id())
    
    from app.models import ConversationState, LEAD_TERMINAL_STATUSES
    
    # Phase 10.3: paginated using the same paginate()/PAGE_SIZE=25 pattern as
    # /crm/leads. This route previously rendered every non-terminal lead for a
    # staff member with no limit. `leads` stays the template's variable name so
    # the existing table loop is untouched.
    PAGE_SIZE = 25
    page = max(1, request.args.get("page", 1, type=int))
    pagination = None
    if staff_name:
        # Phase RC2.3E-1 Batch 1a + H4.
        #
        # H4: _actor_tenant_id() replaces getattr(current_user,'tenant_id'),
        # which is NULL for a SUPER_ADMIN and does NOT honour
        # session['impersonate_tenant_id'] — so an impersonating SUPER_ADMIN
        # resolved None, tenant_query() failed closed, and the screen came up
        # empty. Unlike the Batch 3 case, this one is a real fix.
        _tid = _actor_tenant_id()
        # A STAFF actor is current_user itself (source == "SESSION"). An admin
        # is browsing someone else via ?staff=, so that name is resolved —
        # against BOTH username and display_name, and refusing to guess when
        # ambiguous.
        _owner_user = (current_user if is_staff
                       else staff_service.resolve(_tid, staff_name))
        # Fail closed, per the approved policy: an unresolvable ?staff= shows
        # nothing rather than falling back to a name match that would ignore
        # the FK regime entirely. owner_filter(model, None) is false().
        pagination = tenant_query(ConversationState, _tid).filter(
            staff_identity_service.owner_filter(ConversationState, _owner_user),
            ConversationState.lead_status.notin_(tuple(LEAD_TERMINAL_STATUSES))
        ).order_by(ConversationState.updated_at.desc()).paginate(
            page=page, per_page=PAGE_SIZE, error_out=False)
        leads = pagination.items
    else:
        leads = []
        
    return render_template(
        "crm_my_leads.html",
        key=request.args.get("key", ""),
        staff_name=staff_name,
        active_staff=active_staff,
        pagination=pagination,
        leads=leads
    )

@admin_bp.route("/crm/staff-performance-detail", methods=["GET"])
def crm_staff_performance_detail():
    if not check_auth():
        return _deny()
    actor = get_current_actor()
    is_staff = (actor.get("source") == "SESSION" and actor.get("role") == "STAFF")
    
    if is_staff:
        staff_name = normalize_staff_name(actor.get("username", ""))
    else:
        staff_name = normalize_staff_name(request.args.get("staff", ""))
    # Phase RC2.2D Batch 2: tenant-scoped staff picker. Same sorted list of
    # active display names; only the source changed.
    active_staff = staff_service.active_display_names(_actor_tenant_id())
    
    from app.models import ConversationState, LEAD_TERMINAL_STATUSES
    
    # Phase H4-a: CONSISTENCY, not a behavioural fix — stated plainly because
    # the H4 discovery classified this route as H4-a on the grounds that it
    # passes _tid to get_all_tasks(). It does, but get_all_tasks() hands that
    # tenant straight to tenant_query(), whose SUPER_ADMIN branch returns
    # BEFORE reading the argument — so an impersonating SUPER_ADMIN already
    # got the right tasks under the old idiom. Reverting this one line alone
    # changes no behaviour; it is aligned with the other three so the four
    # routes resolve their tenant by one rule.
    _tid = _actor_tenant_id()
    leads = tenant_query(ConversationState, _tid).all()
    
    staff_metrics = {}
    for staff in active_staff:
        staff_metrics[staff] = {
            "assigned_leads": 0,
            "active_leads": 0,
            "admissions": 0,
            "hot_leads": 0,
            "total_score": 0,
            "leads_with_score": 0,
            "open_tasks": 0,
            "completed_tasks": 0
        }
        
    # Phase RC2.3E-1 Batch 4: match on the NORMALIZED name.
    #
    # This was `s = lead.assigned_staff; if s not in staff_metrics: continue`
    # — an EXACT string match between the raw column and the raw display
    # label, so any lead stored in another spelling was silently skipped. In
    # production that hid 4 lead-rows on this screen: Kiran read 24 against a
    # true 27, Anju 26 against 27.
    #
    # staff_metrics stays keyed by the RAW display label because the template
    # renders those keys; only the LOOKUP is normalized.
    _by_norm = {normalize_staff_name(s): s for s in active_staff}

    for lead in leads:
        s = _by_norm.get(normalize_staff_name(lead.assigned_staff or ""))
        if not s:
            continue

        staff_metrics[s]["assigned_leads"] += 1
        
        if lead.lead_status not in LEAD_TERMINAL_STATUSES:
            staff_metrics[s]["active_leads"] += 1
            
        if lead.is_admitted:
            staff_metrics[s]["admissions"] += 1
            
        score = lead.lead_score or 0
        if score >= 80 and lead.lead_status not in LEAD_TERMINAL_STATUSES:
            staff_metrics[s]["hot_leads"] += 1
            
        if lead.lead_status not in LEAD_TERMINAL_STATUSES:
            staff_metrics[s]["total_score"] += score
            staff_metrics[s]["leads_with_score"] += 1

    # _tid passed explicitly: get_all_tasks() with no tenant is the call shape
    # that produced the "tenant_query could not resolve a tenant" warnings in
    # RC2.2E. The task counts on this screen are part of the same numbers the
    # lead match above fixes, so they are scoped the same way.
    open_tasks, completed_tasks = get_all_tasks(_tid)

    for s in active_staff:
        # Same normalized match as the leads above — get_all_tasks() returns
        # the raw assigned_staff string, so an exact == dropped case variants
        # here too.
        _n = normalize_staff_name(s)
        staff_metrics[s]["open_tasks"] = sum(
            1 for t in open_tasks
            if normalize_staff_name(t.get("staff") or "") == _n)
        staff_metrics[s]["completed_tasks"] = sum(
            1 for t in completed_tasks
            if normalize_staff_name(t.get("staff") or "") == _n)

    for s, m in staff_metrics.items():
        if m["assigned_leads"] > 0:
            m["conversion"] = round((m["admissions"] / m["assigned_leads"]) * 100, 1)
        else:
            m["conversion"] = 0.0
            
        if m["leads_with_score"] > 0:
            m["avg_score"] = round(m["total_score"] / m["leads_with_score"], 1)
        else:
            m["avg_score"] = 0.0

    return render_template(
        "crm_staff_performance_detail.html",
        staff_name=staff_name,
        active_staff=active_staff,
        metrics=staff_metrics
    )

# ── Phase 9.8A: Staff Allocation Center ──────────────────────────────────

@admin_bp.route("/crm/staff-allocation", methods=["GET"])
def crm_staff_allocation():
    # future_role = ADMIN
    # future_permission = STAFF_REALLOCATION
    # future_tenant = tenant_id
    if not check_auth():
        return _deny()
    
    from app.extensions import db
    from sqlalchemy import func, case
    from app.models import ConversationState, LeadEvent
    import json
    
    # 1. Total & HOT Leads & Admissions
    # Phase H4-b: consistency. _tid here feeds ONLY tenant_query(), whose
    # SUPER_ADMIN branch returns before reading the argument, so this
    # changes no behaviour -- it makes all four/seven routes resolve the
    # tenant by one rule.
    _tid = _actor_tenant_id()
    lead_stats = tenant_filter(db.session.query(
        ConversationState.assigned_staff,
        func.count(ConversationState.phone).label('total_leads'),
        func.sum(case((ConversationState.lead_score >= INTELLIGENCE_CONSTANTS["THRESHOLD_HOT"], 1), else_=0)).label('hot_leads'),
        func.sum(case((ConversationState.is_admitted == True, 1), else_=0)).label('admissions')
    ).group_by(ConversationState.assigned_staff), ConversationState, _tid).all()
    
    total_crm_leads = sum(row.total_leads for row in lead_stats)
    
    # Phase RC2.2D Batch 2: tenant-scoped roster.
    #
    # as_registry(), NOT active_display_names(): registry_map is a
    # lowercase->canonical display-name lookup used to fold raw
    # assigned_staff strings onto a canonical spelling. It must contain
    # INACTIVE staff too, or leads owned by a deactivated staff member would
    # stop folding and appear under a separate raw-cased heading.
    registry = staff_service.as_registry(_actor_tenant_id())
    registry_map = {}
    for code, details in registry.items():
        disp = details.get("display_name", "").strip()
        if disp:
            registry_map[disp.lower()] = disp

    aggregated = {}
    for row in lead_stats:
        raw_name = (row.assigned_staff or "").strip()
        if not raw_name:
            s_name = "Unassigned"
        else:
            s_name = registry_map.get(raw_name.lower(), raw_name.title())
            
        if s_name not in aggregated:
            aggregated[s_name] = {"total_leads": 0, "hot_leads": 0, "admissions": 0}
        aggregated[s_name]["total_leads"] += row.total_leads
        aggregated[s_name]["hot_leads"] += row.hot_leads or 0
        aggregated[s_name]["admissions"] += row.admissions or 0
    
    # 2. Task/Admissions mapping from Event logs
    events = tenant_query(LeadEvent, _tid).filter(
        LeadEvent.event_type.in_(["FOLLOW_UP_TASK", "FOLLOW_UP_COMPLETED"])
    ).all()
    
    task_map = {}
    completed_task_ids = set()
    open_tasks = {}
    completed_tasks = {}
    
    for ev in events:
        try:
            data = json.loads(ev.event_data or "{}")
        except:
            data = {}
            
        if ev.event_type == "FOLLOW_UP_COMPLETED":
            tid = data.get("task_id")
            if tid:
                completed_task_ids.add(tid)
                raw_name = (data.get("staff") or "").strip()
                s = registry_map.get(raw_name.lower(), raw_name.title()) if raw_name else "Unassigned"
                completed_tasks[s] = completed_tasks.get(s, 0) + 1
        elif ev.event_type == "FOLLOW_UP_TASK":
            raw_name = (data.get("staff") or "").strip()
            s = registry_map.get(raw_name.lower(), raw_name.title()) if raw_name else "Unassigned"
            tid = data.get("task_id")
            if tid:
                task_map[tid] = s

    # Calculate Open Tasks per staff
    for tid, s in task_map.items():
        if tid not in completed_task_ids:
            open_tasks[s] = open_tasks.get(s, 0) + 1

    # Format output
    staff_data = []
    
    for s_name, counts in aggregated.items():
        pct = round((counts["total_leads"] / total_crm_leads * 100) if total_crm_leads else 0, 1)
        
        # UI Thresholds
        if counts["total_leads"] > 100:
            status = "Overloaded"
        elif counts["total_leads"] > 50:
            status = "Heavy Load"
        else:
            status = "Balanced"
            
        is_active = True
        if s_name != "Unassigned":
            norm_name = normalize_staff_name(s_name)
            found_active = False
            for code, details in registry.items():
                if normalize_staff_name(details.get("display_name", "")) == norm_name:
                    is_active = details.get("active", False)
                    found_active = True
                    break
            if not found_active:
                is_active = False # Staff deleted/legacy
                
        if not is_active and s_name != "Unassigned":
            status = "Inactive"
            
        staff_data.append({
            "name": s_name,
            "total_leads": counts["total_leads"],
            "hot_leads": counts["hot_leads"],
            "admissions": counts["admissions"],
            "open_tasks": open_tasks.get(s_name, 0),
            "completed_tasks": completed_tasks.get(s_name, 0),
            "ownership_pct": pct,
            "status": status,
            "active": is_active
        })
        
    # Also add staff who have 0 leads but are in registry or have open tasks
    existing_staff_names = set(s["name"] for s in staff_data)
    for code, details in registry.items():
        s_name = details.get("display_name", "").strip()
        if s_name and s_name not in existing_staff_names:
            staff_data.append({
                "name": s_name,
                "total_leads": 0, "hot_leads": 0, "admissions": 0,
                "open_tasks": open_tasks.get(s_name, 0),
                "completed_tasks": completed_tasks.get(s_name, 0),
                "ownership_pct": 0,
                "status": "Balanced" if details.get("active", False) else "Inactive",
                "active": details.get("active", False)
            })
            existing_staff_names.add(s_name)
            
    # Sort: Unassigned first, then active, then alphabetical
    staff_data.sort(key=lambda x: (0 if x["name"] == "Unassigned" else (1 if x["active"] else 2), x["name"]))
    
    return render_template(
        "crm_staff_allocation.html",
        key=request.args.get("key", ""),
        staff_data=staff_data,
        total_crm_leads=total_crm_leads
    )


@admin_bp.route("/crm/staff-allocation/<staff_name>", methods=["GET"])
def crm_staff_allocation_detail(staff_name):
    # future_role = ADMIN
    # future_permission = STAFF_REALLOCATION
    if not check_auth():
        return _deny()
        
    from app.models import ConversationState
    
    actual_name = "" if staff_name == "Unassigned" else staff_name
    
    # Phase H4-b: consistency. _tid here feeds ONLY tenant_query(), whose
    # SUPER_ADMIN branch returns before reading the argument, so this
    # changes no behaviour -- it makes all four/seven routes resolve the
    # tenant by one rule.
    _tid = _actor_tenant_id()
    if actual_name == "":
        leads = tenant_query(ConversationState, _tid).filter(
            (ConversationState.assigned_staff == None) | (ConversationState.assigned_staff == "")
        ).all()
    else:
        from sqlalchemy import func
        leads = tenant_query(ConversationState, _tid).filter(
            func.lower(func.trim(ConversationState.assigned_staff)) == actual_name.lower()
        ).all()
        
    # Phase RC2.2D Batch 2: tenant-scoped staff picker. Same sorted list of
    # active display names; only the source changed.
    active_staff = staff_service.active_display_names(_actor_tenant_id())
    
    return render_template(
        "crm_staff_allocation_detail.html",
        staff_name=staff_name,
        leads=leads,
        active_staff=active_staff
    )


@admin_bp.route("/crm/staff-allocation/check-deactivation/<staff_name>", methods=["GET"])
def crm_staff_allocation_check(staff_name):
    # future_role = ADMIN
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
        
    from app.models import ConversationState, LeadEvent
    import json
    
    if staff_name == "Unassigned":
        return jsonify({"safe": False, "reason": "Cannot deactivate Unassigned"})
        
    # 1. Check Leads
    from sqlalchemy import func
    # Phase H4-b: consistency. _tid here feeds ONLY tenant_query(), whose
    # SUPER_ADMIN branch returns before reading the argument, so this
    # changes no behaviour -- it makes all four/seven routes resolve the
    # tenant by one rule.
    _tid = _actor_tenant_id()
    lead_count = tenant_query(ConversationState, _tid).filter(func.lower(func.trim(ConversationState.assigned_staff)) == staff_name.lower()).count()
    admission_count = tenant_query(ConversationState, _tid).filter(func.lower(func.trim(ConversationState.assigned_staff)) == staff_name.lower(), ConversationState.is_admitted == True).count()
    
    # 2. Check Open Tasks
    events = tenant_query(LeadEvent, _tid).filter(
        LeadEvent.event_type.in_(["FOLLOW_UP_TASK", "FOLLOW_UP_COMPLETED"])
    ).all()
    
    task_map = {}
    completed_task_ids = set()
    
    for ev in events:
        try:
            data = json.loads(ev.event_data or "{}")
        except:
            data = {}
            
        if ev.event_type == "FOLLOW_UP_COMPLETED":
            tid = data.get("task_id")
            if tid:
                completed_task_ids.add(tid)
        elif ev.event_type == "FOLLOW_UP_TASK":
            s = (data.get("staff") or "").strip() or "Unassigned"
            tid = data.get("task_id")
            if tid and s == staff_name:
                task_map[tid] = True

    open_tasks_count = sum(1 for tid in task_map if tid not in completed_task_ids)
    
    # Check if safe
    safe = lead_count == 0 and open_tasks_count == 0
    
    return jsonify({
        "safe": safe,
        "active_leads": lead_count,
        "admissions": admission_count,
        "open_tasks": open_tasks_count,
        "pending_follow_ups": open_tasks_count
    })

from flask_login import current_user, login_user, logout_user, login_required
from werkzeug.security import check_password_hash
from datetime import datetime

def check_auth():
    """
    Returns True when request is authenticated.

    AUTH_MODE = ADMIN_KEY_ONLY
        -> require legacy key only

    AUTH_MODE = DUAL
        -> allow valid session OR ADMIN_KEY

    AUTH_MODE = SESSION_ONLY
        -> allow session only
    """
    mode = current_app.config.get("AUTH_MODE", "ADMIN_KEY_ONLY")
    key_valid = request.args.get("key", "") == ADMIN_KEY
    
    if mode == "ADMIN_KEY_ONLY":
        return key_valid
        
    elif mode == "DUAL":
        return key_valid or current_user.is_authenticated
            
    elif mode == "SESSION_ONLY":
        return current_user.is_authenticated
            
    return False

def get_current_actor():
    """
    Returns the current actor dictionary:
    {
        "authenticated": True/False,
        "username": "...",
        "role": "...",
        "source": "SESSION" or "ADMIN_KEY"
    }
    """
    is_session = current_user.is_authenticated
    is_key = request.args.get("key", "") == ADMIN_KEY
    mode = current_app.config.get("AUTH_MODE", "ADMIN_KEY_ONLY")
    
    # Priority: If mode is SESSION_ONLY, ignore ADMIN_KEY
    if mode == "SESSION_ONLY" and is_session:
        return {
            "authenticated": True,
            "username": current_user.username,
            "role": current_user.role,
            "source": "SESSION"
        }
        
    if mode in ["ADMIN_KEY_ONLY", "DUAL"] and is_key:
        return {
            "authenticated": True,
            "username": "Admin",
            "role": "ADMIN",
            "source": "ADMIN_KEY"
        }
        
    if mode == "DUAL" and is_session:
        return {
            "authenticated": True,
            "username": current_user.username,
            "role": current_user.role,
            "source": "SESSION"
        }
        
    return {
        "authenticated": False,
        "username": None,
        "role": None,
        "source": None
    }


@admin_bp.route("/crm/auth-debug", methods=["GET"])
@login_required
def auth_debug():
    actor = get_current_actor()
    logging.info(f"AUTH source={actor['source']} user={actor['username']}")
    return jsonify(actor)



@admin_bp.route("/crm/login", methods=["GET", "POST"])
def crm_login():
    if current_user.is_authenticated:
        next_page = request.args.get('next')
        if next_page and next_page.startswith('/'):
            return redirect(next_page)
        return redirect(url_for("admin.crm_home"))
        
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        
        from app.models import User, Tenant
        from app.extensions import db
        user = User.query.filter_by(email=email).first()

        # Phase 0 Sprint 3: sovereign audit log (Constitution I.7)
        from app.services.audit_service import log_audit, request_ip

        # Fail closed on non-existent user or invalid role
        if not user or user.role not in ('ADMIN', 'STAFF'):
            log_audit("LOGIN_FAILURE", actor=email, target="/crm/login",
                      detail={"reason": "unknown user or invalid role"},
                      ip=request_ip())
            flash("Invalid credentials or inactive account.", "danger")
            return redirect(url_for("admin.crm_login"))
            
        if user.is_active and check_password_hash(user.password_hash, password):
            # Resolve tenant safely
            if not user.tenant_id:
                flash("Invalid credentials or inactive account.", "danger")
                return redirect(url_for("admin.crm_login"))
                
            tenant = db.session.get(Tenant, user.tenant_id)
            if not tenant or tenant.status != 'ACTIVE':
                flash("Account awaiting approval. Please contact support.", "warning")
                return redirect(url_for("admin.crm_login"))
                
            # Phase 15C.5-B: Enforce Email Verification for tenant users
            if user.email_verified_at is None:
                flash("Please verify your email address to log in. Check your inbox.", "warning")
                return redirect(url_for("admin.crm_login"))
                
            user.last_login = datetime.utcnow()
            db.session.commit()
            login_user(user)
            log_audit("LOGIN_SUCCESS", actor=email, tenant_id=user.tenant_id,
                      target="/crm/login", detail={"role": user.role},
                      ip=request_ip())
            next_page = request.args.get('next')
            if next_page and next_page.startswith('/'):
                return redirect(next_page)
            return redirect(url_for("admin.crm_home"))

        log_audit("LOGIN_FAILURE", actor=email,
                  tenant_id=(user.tenant_id if user else None),
                  target="/crm/login", detail={"reason": "bad password or inactive"},
                  ip=request_ip())
        flash("Invalid credentials or inactive account.", "danger")

    return render_template("crm_login.html")


@admin_bp.route("/crm/logout", methods=["GET"])
@login_required
def crm_logout():
    logout_user()
    session.clear()
    return redirect(url_for("admin.crm_login"))


@admin_bp.route("/crm/setup-password", methods=["GET", "POST"])
@login_required
def crm_setup_password():
    if not getattr(current_user, 'require_password_change', False):
        return redirect(url_for("admin.crm_home"))
        
    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        if len(new_password) < 6:
            flash("Password must be at least 6 characters.", "danger")
            return redirect(url_for("admin.crm_setup_password"))
            
        from werkzeug.security import generate_password_hash
        from app.extensions import db
        current_user.password_hash = generate_password_hash(new_password)
        current_user.require_password_change = False
        db.session.commit()
        flash("Password updated successfully. Welcome!", "success")
        return redirect(url_for("admin.crm_home"))
        
    return render_template("crm_setup_password.html")


@admin_bp.after_request
def add_cache_control_headers(response):
    """
    Phase 10F.1: Session Hardening
    Add no-cache headers to CRM routes to prevent back-button access after logout.
    """
    if request.path.startswith('/crm/') and not request.path.startswith('/crm/login'):
        # Do not apply headers to static assets
        if not request.path.endswith(('.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.woff', '.woff2', '.ttf', '.eot')):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
    return response


# ── Phase 13-A3C: Super Admin Control Center ─────────────────────────────────

@admin_bp.route("/crm/super/login", methods=["GET", "POST"])
def crm_super_login():
    if current_user.is_authenticated and getattr(current_user, 'role', None) == 'SUPER_ADMIN':
        next_page = request.args.get('next')
        if next_page and next_page.startswith('/'):
            return redirect(next_page)
        return redirect(url_for("admin.crm_super_dashboard"))
        
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        
        from app.models import User
        from app.extensions import db
        from app.services.audit_service import log_audit, request_ip
        user = User.query.filter_by(email=email, role='SUPER_ADMIN').first()
        if user and user.is_active and check_password_hash(user.password_hash, password):
            user.last_login = datetime.utcnow()
            db.session.commit()
            login_user(user)
            log_audit("LOGIN_SUCCESS", actor=email, target="/crm/super/login",
                      detail={"role": "SUPER_ADMIN"}, ip=request_ip())
            next_page = request.args.get('next')
            if next_page and next_page.startswith('/'):
                return redirect(next_page)
            return redirect(url_for("admin.crm_super_dashboard"))

        log_audit("LOGIN_FAILURE", actor=email, target="/crm/super/login",
                  detail={"reason": "bad credentials or not super admin"},
                  ip=request_ip())
        flash("Invalid credentials or unauthorized.", "danger")
        
    return render_template("crm_super_login.html")

@admin_bp.route("/crm/super/dashboard", methods=["GET"])
@login_required
@super_admin_required
def crm_super_dashboard():
    from app.models import Tenant, User
    tenants = Tenant.query.order_by(Tenant.created_at.desc()).all()
    
    admins = User.query.filter_by(role="ADMIN").all()
    tenant_admins = {
        admin.tenant_id: admin
        for admin in admins
    }

    # Phase RC2.3E-6: a tenant is ONE row with a count, never one row per
    # admin. tenant_admins above is keyed by tenant_id, so with three admins
    # two are silently discarded and which one survives depends on query
    # order — Oxford's 3 rendered as 1. It is retained (the detail block reads
    # its email and verification badge) but is no longer the only signal that
    # admins exist.
    #
    # Counted from the same rows already loaded rather than a second query:
    # one pass, and the count cannot disagree with the object shown beside it.
    # ACTIVE only, matching staff_service.active_admin_count() — an admin who
    # cannot log in should not make a tenant look covered.
    admin_counts = {}
    for admin in admins:
        if admin.is_active:
            admin_counts[admin.tenant_id] = admin_counts.get(admin.tenant_id, 0) + 1

    return render_template("crm_super_dashboard.html", tenants=tenants,
                           tenant_admins=tenant_admins,
                           admin_counts=admin_counts)

@admin_bp.route("/admin/tenant/<tenant_id>/resend-verification", methods=["POST"])
@login_required
@super_admin_required
def crm_super_resend_verification(tenant_id):
    from app.models import User
    import logging
    from app.services.email_service import email_service
    
    admin_user = User.query.filter_by(tenant_id=tenant_id, role="ADMIN").first()
    if not admin_user:
        flash("No Tenant Admin found for this tenant.", "danger")
        return redirect(url_for('admin.crm_super_dashboard'))
        
    if not admin_user.email:
        flash("Tenant Admin does not have a registered email.", "warning")
        return redirect(url_for('admin.crm_super_dashboard'))
        
    if admin_user.email_verified_at is not None:
        flash("Tenant Admin is already verified.", "info")
        return redirect(url_for('admin.crm_super_dashboard'))
        
    logging.info(f"SUPER_ADMIN_RESEND_VERIFICATION_REQUESTED: User {admin_user.id}")
    success = email_service.send_verification_email(admin_user.email, admin_user.username)
    
    if success:
        logging.info(f"SUPER_ADMIN_RESEND_VERIFICATION_SENT: User {admin_user.id}")
        flash("Verification email successfully resent.", "success")
    else:
        logging.error(f"SUPER_ADMIN_RESEND_VERIFICATION_FAILED: User {admin_user.id}")
        flash("Failed to resend verification email.", "danger")
        
    return redirect(url_for('admin.crm_super_dashboard'))

@admin_bp.route("/crm/super/tenant/<tenant_id>/suspend", methods=["POST"])
@login_required
@super_admin_required
def crm_super_suspend_tenant(tenant_id):
    from app.models import Tenant
    from app.extensions import db
    tenant = Tenant.query.get_or_404(tenant_id)
    if tenant.status != 'SUSPENDED':
        tenant.status = 'SUSPENDED'
        db.session.commit()
        flash(f"Tenant '{tenant.name}' has been suspended.", "warning")
    return redirect(url_for('admin.crm_super_dashboard'))

@admin_bp.route("/crm/super/tenant/<tenant_id>/reactivate", methods=["POST"])
@login_required
@super_admin_required
def crm_super_reactivate_tenant(tenant_id):
    from app.models import Tenant
    from app.extensions import db
    tenant = Tenant.query.get_or_404(tenant_id)
    if tenant.status != 'ACTIVE':
        tenant.status = 'ACTIVE'
        db.session.commit()
        flash(f"Tenant '{tenant.name}' has been reactivated.", "success")
    return redirect(url_for('admin.crm_super_dashboard'))

# ── Phase 13-B3D: Impersonation ──────────────────────────────────────────────
@admin_bp.route("/crm/super/impersonate/<tenant_id>", methods=["POST"])
@login_required
@super_admin_required
def crm_super_impersonate(tenant_id):
    from app.models import Tenant
    tenant = Tenant.query.get_or_404(tenant_id)
    session['impersonate_tenant_id'] = tenant.id
    session['impersonate_tenant_name'] = tenant.name

    # Phase 8.2E.6A (ADR-023 D3): a platform operator entering a customer
    # tenant must never be indistinguishable from that tenant's own staff.
    # tenant_id is the IMPERSONATED tenant so the entry is discoverable from
    # the tenant's own audit view, not only the platform's.
    from app.services.audit_service import log_audit, request_ip
    log_audit("IMPERSONATION_START",
              actor=getattr(current_user, "email", None) or getattr(current_user, "username", None),
              tenant_id=tenant.id,
              target=f"/crm/super/impersonate/{tenant.id}",
              detail={"tenant_name": tenant.name, "actor_role": "SUPER_ADMIN"},
              ip=request_ip())

    flash(f"Now impersonating tenant: {tenant.name}", "success")
    return redirect(url_for("admin.crm_home"))

@admin_bp.route("/crm/super/impersonate/exit", methods=["POST"])
@login_required
@super_admin_required
def crm_super_impersonate_exit():
    # Capture the impersonated tenant BEFORE clearing the session — popping
    # first would leave the audit entry with no subject.
    _prev_tenant_id   = session.get('impersonate_tenant_id')
    _prev_tenant_name = session.get('impersonate_tenant_name')

    session.pop('impersonate_tenant_id', None)
    session.pop('impersonate_tenant_name', None)

    # Only record a real transition. Hitting exit while not impersonating is
    # a no-op, not a security event, and must not write a subject-less row.
    if _prev_tenant_id:
        from app.services.audit_service import log_audit, request_ip
        log_audit("IMPERSONATION_END",
                  actor=getattr(current_user, "email", None) or getattr(current_user, "username", None),
                  tenant_id=_prev_tenant_id,
                  target="/crm/super/impersonate/exit",
                  detail={"tenant_name": _prev_tenant_name, "actor_role": "SUPER_ADMIN"},
                  ip=request_ip())

    flash("Exited impersonation mode.", "info")
    return redirect(url_for("admin.crm_super_dashboard"))

# ── Phase 13-B2B: Tenant Approval ────────────────────────────────────────────
@admin_bp.route("/crm/super/tenant/<tenant_id>/approve", methods=["POST"])
@login_required
@super_admin_required
def crm_super_approve_tenant(tenant_id):
    from app.models import Tenant
    from app.extensions import db
    tenant = Tenant.query.get_or_404(tenant_id)
    if tenant.status == 'PENDING':
        tenant.status = 'ACTIVE'
        db.session.commit()
        flash(f"Tenant '{tenant.name}' has been approved and is now ACTIVE.", "success")
    else:
        flash(f"Tenant '{tenant.name}' is already {tenant.status}. No change made.", "info")
    return redirect(url_for('admin.crm_super_dashboard'))

