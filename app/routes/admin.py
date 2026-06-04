import logging
from sqlalchemy import or_
from flask import Blueprint, request, jsonify, render_template, redirect, flash, url_for, current_app, session
from app.config import ADMIN_KEY

import os
import json

def get_staff_json_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "staff_master.json")

def load_staff_registry():
    path = get_staff_json_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logging.error(f"Error loading staff_master.json: {e}")
    return {}

def save_staff_registry(data):
    path = get_staff_json_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logging.error(f"Error saving staff_master.json: {e}")


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


# ── Phase 7E: Course Journey helpers ───────────────────────────────────────
from app.bot.constants import normalize_course_name

def get_course_enquiries(phone: str) -> list:
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
            LeadEvent.query
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


def get_course_admissions(phone: str) -> list:
    """
    Return deduplicated list of course names for which a COURSE_ADMISSION
    event exists for this phone. Returns [] on any error or empty table.

    event_data is a JSON string: '{"course": "Python Programming"}'
    """
    import json
    try:
        from app.models import LeadEvent
        events = (
            LeadEvent.query
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
    
    if final_score >= 80:
        temperature = "HOT"
    elif final_score >= 50:
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
    elif final_score >= 80:
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

def calculate_lead_health(state_updated_at, state_created_at, latest_msg_time, latest_event_time, intelligence, needs_reply):
    latest_activity = latest_msg_time or latest_event_time or state_updated_at or state_created_at
    if not latest_activity:
        from datetime import datetime
        latest_activity = datetime.now()
        
    from datetime import datetime
    days_inactive = (datetime.now() - latest_activity).days
    if days_inactive < 0:
        days_inactive = 0
        
    if days_inactive <= 2:
        aging_status = "Fresh"
    elif days_inactive <= 6:
        aging_status = "Attention"
    elif days_inactive <= 13:
        aging_status = "Aging"
    else:
        aging_status = "Critical"
        
    escalation = None
    events_set = intelligence.get("_events", [])
    if intelligence.get("temperature") == "HOT" and aging_status == "Critical":
        escalation = "🚨 HOT Lead Ignored"
    elif needs_reply and days_inactive >= 1:
        escalation = "💬 Waiting For Reply"
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

    return jsonify({
        "total_leads":          count_states(),
        "pending_followups":    count_pending_followups(),
        "stage_breakdown":      get_stage_breakdown(),
        "active_conversations": get_all_states(),
    })


@admin_bp.route("/panel", methods=["GET"])
def admin_panel():
    if request.args.get("key", "") != ADMIN_KEY:
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

def calculate_home_kpis():
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

    # Future Tenant Scope: total_leads = ConversationState.query.filter_by(tenant_id=tid).count()
    total_leads = ConversationState.query.count()
    admissions  = ConversationState.query.filter_by(is_admitted=True).count()

    # HOT leads: score >= 80 (consistent with EVENT_SCORE_MAP logic in calculate_lead_intelligence)
    # Using lead_score column as lightweight proxy — full intelligence calc runs on leads page
    # Future Tenant Scope: .filter_by(tenant_id=tid)
    hot_leads = ConversationState.query.filter(
        ConversationState.lead_score >= 80
    ).count()

    # Needs reply: last message for each phone was incoming
    # Future Tenant Scope: join tenant_id filter here
    subq = db.session.query(
        ConversationMessage.phone,
        func.max(ConversationMessage.id).label('max_id')
    ).group_by(ConversationMessage.phone).subquery()
    needs_reply_count = db.session.query(ConversationMessage).join(
        subq, ConversationMessage.id == subq.c.max_id
    ).filter(ConversationMessage.direction == 'incoming').count()

    # Task KPIs — reuse existing get_all_tasks() helper
    try:
        open_tasks, _ = get_all_tasks()
        now = datetime.utcnow()
        open_task_count = len(open_tasks)
        overdue_count = sum(
            1 for t in open_tasks
            if t.get("due_dt") and t["due_dt"] < now
        )
    except Exception:
        open_task_count = 0
        overdue_count = 0

    # Staff active count from registry JSON
    registry = load_staff_registry()
    staff_active = sum(1 for v in registry.values() if v.get("active"))

    # Recent leads (last 5 by created_at)
    # Future Tenant Scope: .filter_by(tenant_id=tid)
    recent_leads = ConversationState.query.order_by(
        ConversationState.created_at.desc()
    ).limit(5).all()

    # Recent events (last 10 LeadEvents for activity feed)
    # Future Tenant Scope: .filter_by(tenant_id=tid)
    recent_events = LeadEvent.query.order_by(
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
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()

    return render_template(
        "crm_marketing.html",
        key=request.args.get("key", ""),
    )


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

    # \u2500\u2500 Build query safely \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    from sqlalchemy.sql import func
    
    q = ConversationState.query
    actor = get_current_actor()
    is_staff = (actor.get("source") == "SESSION" and actor.get("role") == "STAFF")
    
    if is_staff:
        actor_username_normalized = (actor.get("username") or "").strip().lower()
        q = q.filter(
            func.lower(func.trim(ConversationState.assigned_staff)) == actor_username_normalized
        )

    if search:
        q = q.filter(
        or_(
            ConversationState.phone.ilike(f"%{search}%"),
            ConversationState.name.ilike(f"%{search}%"),
    )
)
    if stage_filter:
        q = q.filter(ConversationState.stage == stage_filter)
    if admitted_filter == "yes":
        q = q.filter(ConversationState.is_admitted == True)  # noqa: E712
    elif admitted_filter == "no":
        q = q.filter(ConversationState.is_admitted != True)  # noqa: E712

    q = q.order_by(ConversationState.updated_at.desc())
    pagination = q.paginate(page=page, per_page=PAGE_SIZE, error_out=False)

    # ── Dashboard metrics & Intelligence Cache (Phase 6C) ──────────────────
    from app.models import LeadEvent, ConversationMessage, FollowUpJob
    from sqlalchemy.sql import func
    
    total_leads = ConversationState.query.count()
    pending_fu = count_pending_followups()
    
    # 1. Fetch all states and events for intelligence caching
    all_states = db.session.query(
        ConversationState.phone, 
        ConversationState.lead_score,
        ConversationState.updated_at,
        ConversationState.created_at
    ).all()
    all_events = db.session.query(LeadEvent.phone, LeadEvent.event_type, LeadEvent.created_at).all()
    
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
    subq = db.session.query(
        ConversationMessage.phone,
        func.max(ConversationMessage.id).label('max_id'),
        func.max(ConversationMessage.created_at).label('max_created')
    ).group_by(ConversationMessage.phone).subquery()
    
    latest_msgs = db.session.query(
        ConversationMessage.phone, 
        ConversationMessage.direction,
        subq.c.max_created
    ).join(
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
            phone in needs_reply_phones
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
    pending_jobs = db.session.query(FollowUpJob.phone).filter_by(done=False).all()
    pending_fu_phones = {j.phone for j in pending_jobs}

    for lead in pagination.items:
        lead.intelligence = intelligence_cache.get(lead.phone)
        lead.needs_reply = lead.phone in needs_reply_phones
        lead.has_pending_fu = lead.phone in pending_fu_phones

    # ── All distinct stages for filter dropdown ──────────────────────────────────
    stages = [r[0] for r in db.session.query(ConversationState.stage).distinct().all() if r[0]]

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


# ── Phase 6G: Audience Calculation Helper ──
def _calculate_audiences():
    from app.extensions import db
    from app.models import ConversationState, LeadEvent, ConversationMessage
    from sqlalchemy.sql import func
    
    all_states = db.session.query(
        ConversationState.phone, 
        ConversationState.lead_score,
        ConversationState.updated_at,
        ConversationState.created_at
    ).all()
    all_events = db.session.query(LeadEvent.phone, LeadEvent.event_type, LeadEvent.created_at).all()
    
    events_by_phone = {}
    latest_event_time = {}
    for e in all_events:
        events_by_phone.setdefault(e.phone, []).append(e)
        if e.phone not in latest_event_time or (e.created_at and e.created_at > latest_event_time[e.phone]):
            latest_event_time[e.phone] = e.created_at
            
    subq = db.session.query(
        ConversationMessage.phone,
        func.max(ConversationMessage.id).label('max_id'),
        func.max(ConversationMessage.created_at).label('max_created')
    ).group_by(ConversationMessage.phone).subquery()
    
    latest_msgs = db.session.query(
        ConversationMessage.phone, 
        ConversationMessage.direction,
        subq.c.max_created
    ).join(
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
            intel, phone in needs_reply_phones
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
def crm_staff_management():
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()
        
    key = request.args.get("key", "")
    registry = load_staff_registry()
    
    if request.method == "POST":
        action = request.form.get("action")
        
        if action == "add":
            code = request.form.get("staff_code", "").strip().upper()
            display_name = request.form.get("display_name", "").strip()
            role = request.form.get("role", "STAFF").strip()
            active = request.form.get("active") == "on"
            
            if not code or not display_name:
                return redirect(url_for("admin.crm_staff_management", key=key, err="Code and Name required"))
            if code in registry:
                return redirect(url_for("admin.crm_staff_management", key=key, err="Staff code already exists"))
                
            registry[code] = {
                "display_name": display_name,
                "role": role,
                "active": active
            }
            save_staff_registry(registry)
            return redirect(url_for("admin.crm_staff_management", key=key, msg="Staff added"))
            
        elif action == "edit":
            code = request.form.get("staff_code", "").strip().upper()
            if code not in registry:
                return redirect(url_for("admin.crm_staff_management", key=key, err="Staff not found"))
                
            new_active = request.form.get("active") == "on"
            if not new_active and registry[code].get("active", False):
                from app.models import ConversationState
                staff_name = registry[code].get("display_name", "")
                norm_name = normalize_staff_name(staff_name)
                leads_count = ConversationState.query.filter(ConversationState.assigned_staff == norm_name).count()
                if leads_count > 0:
                    err_msg = f"BLOCK_DEACTIVATION:{leads_count}:{norm_name}"
                    return redirect(url_for("admin.crm_staff_management", key=key, err=err_msg))
                
            registry[code]["display_name"] = request.form.get("display_name", "").strip() or registry[code]["display_name"]
            registry[code]["role"] = request.form.get("role", "").strip() or registry[code]["role"]
            registry[code]["active"] = new_active
            
            save_staff_registry(registry)
            return redirect(url_for("admin.crm_staff_management", key=key, msg="Staff updated"))
            
        elif action == "toggle":
            code = request.form.get("staff_code", "").strip().upper()
            if code in registry:
                new_active = not registry[code]["active"]
                if not new_active:
                    from app.models import ConversationState
                    staff_name = registry[code].get("display_name", "")
                    norm_name = normalize_staff_name(staff_name)
                    leads_count = ConversationState.query.filter(ConversationState.assigned_staff == norm_name).count()
                    if leads_count > 0:
                        err_msg = f"BLOCK_DEACTIVATION:{leads_count}:{norm_name}"
                        return redirect(url_for("admin.crm_staff_management", key=key, err=err_msg))

                registry[code]["active"] = new_active
                save_staff_registry(registry)
                return redirect(url_for("admin.crm_staff_management", key=key, msg="Staff status toggled"))
    
    # Calculate statistics based on existing analytics logic
    analytics_data = calculate_admission_analytics()
    # analytics_data["staff_rows"] contains {"name": ..., "leads": ..., "admissions": ...}
    stats_map = {row["name"]: {"leads": row["leads"], "admissions": row["admissions"]} for row in analytics_data["staff_rows"]}
    
    staff_list = []
    for code, data in registry.items():
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
        key=key,
        staff_list=staff_list,
        msg=request.args.get("msg", ""),
        err=request.args.get("err", "")
    )


# ── GET /crm/leads ─────────────────────────────────────────────────────────

@admin_bp.route("/crm/leads", methods=["GET"])

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
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()

    from app.models import ConversationState, MessageLog, ConversationMessage, LeadEvent
    from datetime import datetime, timedelta

    lead = ConversationState.query.filter_by(phone=phone).first()
    if lead is None:
        return _not_found(phone)

    # ── Fetch message timeline (newest first, capped at 100) ──
    logs = (
        MessageLog.query
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
    query = ConversationMessage.query.filter_by(phone=phone)

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
            LeadEvent.query
            .filter_by(phone=phone)
            .order_by(LeadEvent.created_at.asc())
            .all()
        )
    except Exception:
        events = []

    # ── Phase 6B & 6F: Intelligence and Health ──
    intelligence = calculate_lead_intelligence(lead.lead_score, events)
    
    latest_msg = ConversationMessage.query.filter_by(phone=phone).order_by(ConversationMessage.created_at.desc()).first()
    latest_msg_time = latest_msg.created_at if latest_msg else None
    latest_event_time = events[-1].created_at if events else None
    needs_reply = (latest_msg.direction == 'incoming') if latest_msg else False
    
    health = calculate_lead_health(
        lead.updated_at,
        lead.created_at,
        latest_msg_time,
        latest_event_time,
        intelligence,
        needs_reply
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
    registry = load_staff_registry()
    active_staff = [data["display_name"] for code, data in registry.items() if data.get("active")]
    active_staff.sort()

    # ── Phase 9.3A: Task Summary ─────────────────────────────────────────────
    task_summary = {"open": 0, "overdue": 0, "completed": 0}
    task_map = {}
    
    for ev in events:
        if ev.event_type == "FOLLOW_UP_TASK":
            payload = event_payload_map.get(ev.id, {})
            tid = payload.get("task_id")
            if tid:
                task_map[tid] = payload
        elif ev.event_type == "FOLLOW_UP_COMPLETED":
            payload = event_payload_map.get(ev.id, {})
            tid = payload.get("task_id")
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

    return render_template(
        "crm_lead_detail.html",
        lead=lead,
        logs=logs,
        timeline=timeline,
        unified_timeline=unified_timeline,
        metrics=metrics,
        search_q=search_q,
        source_q=source_q,
        range_q=range_q,
        key=request.args.get("key", ""),
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
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()

    from app.models import ConversationState
    from app.extensions import db

    key  = request.args.get("key", "")
    lead = ConversationState.query.filter_by(phone=phone).first()
    if lead is None:
        return _not_found(phone)

    import urllib.parse
    qs = ""
    if request.args.get("search"): qs += f"&search={urllib.parse.quote(request.args.get('search'))}"
    if request.args.get("source"): qs += f"&source={urllib.parse.quote(request.args.get('source'))}"
    if request.args.get("range"):  qs += f"&range={urllib.parse.quote(request.args.get('range'))}"

    try:
        old_staff = lead.assigned_staff
        
        lead.lead_status    = request.form.get("lead_status",    "").strip() or lead.lead_status
        lead.assigned_staff = request.form.get("assigned_staff", "").strip() or None
        lead.notes          = request.form.get("notes",          "").strip() or None

        score_raw = request.form.get("lead_score", "").strip()
        if score_raw.isdigit():
            lead.lead_score = max(0, min(100, int(score_raw)))

        # ── Snapshot values before commit for post-commit event firing ──
        new_course    = (lead.course or "").strip()
        new_admitted  = request.form.get("is_admitted") == "1"
        new_staff     = lead.assigned_staff

        # ── Phase 8.2 Gap 2: Hard block — admission requires assigned staff ──────
        if new_admitted and not (lead.assigned_staff or "").strip():
            db.session.rollback()
            return redirect(url_for(
                "admin.crm_lead_detail", phone=phone, key=ADMIN_KEY,
                err="Admission+blocked%3A+please+assign+a+staff+member+before+marking+this+lead+as+admitted."
            ))

        # ── Phase 8.2 Gap 3: Auto-promote lead_status → Enrolled on admission ────
        _PROMOTE_STATUSES = {"Lead", "Contacted", "Interested"}
        if new_admitted and (lead.lead_status or "").strip() in _PROMOTE_STATUSES:
            lead.lead_status = "Enrolled"

        lead.is_admitted = new_admitted

        db.session.commit()

        # ── Phase 7E & 9.1: Fire events AFTER successful commit ──────────
        import json
        from app.services.log_service import log_lead_event
        from app.models import LeadEvent

        # Phase 9.1: LEAD_REASSIGNED accountability audit
        if old_staff != new_staff:
            log_lead_event(
                phone=phone,
                event_type="LEAD_REASSIGNED",
                event_data=json.dumps({
                    "from": old_staff or "",
                    "to": new_staff or "",
                    "by": "Admin"
                })
            )

        # COURSE_ENQUIRY — fire once per unique course name.
        if new_course:
            existing_enquiry = LeadEvent.query.filter_by(
                phone=phone, event_type="COURSE_ENQUIRY"
            ).all()
            already_logged = {
                (json.loads(e.event_data or "{}").get("course") or "").strip().lower()
                for e in existing_enquiry
                if e.event_data
            }
            if new_course.lower() not in already_logged:
                log_lead_event(
                    phone=phone,
                    event_type="COURSE_ENQUIRY",
                    event_data=json.dumps({"course": new_course}),
                )

        # COURSE_ADMISSION — fire once per unique admitted course name.
        if new_admitted and new_course:
            existing_admission = LeadEvent.query.filter_by(
                phone=phone, event_type="COURSE_ADMISSION"
            ).all()
            already_admitted = {
                (json.loads(e.event_data or "{}").get("course") or "").strip().lower()
                for e in existing_admission
                if e.event_data
            }
            if new_course.lower() not in already_admitted:
                log_lead_event(
                    phone=phone,
                    event_type="COURSE_ADMISSION",
                    event_data=json.dumps({
                        "course": new_course,
                        "staff": new_staff or ""
                    }),
                )

    except Exception as e:
        db.session.rollback()

    return redirect(url_for("admin.crm_lead_detail", phone=phone, key=ADMIN_KEY))

# ── Phase 6G: Campaigns ──
@admin_bp.route("/crm/campaigns", methods=["GET"])
def campaigns():
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()
    
    from datetime import date
    from app.models import ConversationMessage
    from app.extensions import db
    
    today = date.today()
    campaign_msgs = ConversationMessage.query.filter(
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
    
    return render_template("campaigns.html", dashboard=dashboard, key=ADMIN_KEY)

@admin_bp.route("/crm/campaigns/preview", methods=["POST"])
def campaign_preview():
    if request.args.get("key", "") != ADMIN_KEY:
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
def campaign_send():
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()
        
    name = request.form.get("campaign_name", "").strip()
    audience_type = request.form.get("audience", "").strip()
    message = request.form.get("message", "").strip()
    
    if not name or not message:
        flash("Campaign name and message are required.", "danger")
        return redirect(url_for("admin.campaigns", key=ADMIN_KEY))
        
    audiences = _calculate_audiences()
    phones = list(audiences.get(audience_type, set()))
    
    if len(phones) == 0:
        flash(f"Audience '{audience_type}' has 0 leads. Campaign aborted.", "warning")
        return redirect(url_for("admin.campaigns", key=ADMIN_KEY))
        
    if len(phones) > 100:
        flash("Campaigns are limited to 100 recipients max. Please split large batches.", "danger")
        return redirect(url_for("admin.campaigns", key=ADMIN_KEY))
        
    from app.services.campaign_service import start_campaign
    try:
        start_campaign(phones, message, name)
        flash(f"Campaign '{name}' started successfully. Sending to {len(phones)} leads. Check dashboard later for results.", "success")
    except Exception as e:
        flash(f"Failed to start campaign: {str(e)}", "danger")
        
    return redirect(url_for("admin.campaigns", key=ADMIN_KEY))


# ── POST /crm/lead/<phone>/send ────────────────────────────────────────────

@admin_bp.route("/crm/lead/<phone>/send", methods=["POST"])
def crm_lead_send(phone):
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()

    key     = request.args.get("key", "")
    message = request.form.get("manual_message", "").strip()

    if not message:
        return redirect(f"/crm/lead/{phone}?key={key}&err=Message+cannot+be+empty")

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
            log_message(
                phone=phone,
                direction="outbound",
                message_type="manual",
                message_text=message,
            )
            # ── Persist manual send to ConversationMessage (CRM timeline) ──
            from app.services.log_service import save_conversation_message, log_lead_event
            from app.models import ConversationState
            import json
            
            lead = ConversationState.query.filter_by(phone=phone).first()
            current_staff = lead.assigned_staff if lead else None
            
            save_conversation_message(
                phone=phone,
                direction="outgoing",
                message=message,
                message_type="text",
                source="manual",
                staff_name=current_staff or "Admin",
                wa_message_id=None,
            )
            
            # Phase 9.1: MESSAGE_OWNER audit using LeadEvent
            log_lead_event(
                phone=phone,
                event_type="MANUAL_MESSAGE",
                event_data=json.dumps({"staff": current_staff or "Admin"})
            )
            return redirect(f"/crm/lead/{phone}?key={key}&msg=Message+sent+successfully{qs}")

        else:
            return redirect(f"/crm/lead/{phone}?key={key}&err=WhatsApp+API+returned+an+error{qs}")
    except Exception:
        logging.exception(f"Manual WhatsApp send failed for {phone}")
        return redirect(f"/crm/lead/{phone}?key={key}&err=Unexpected+server+error{qs}")



# ── Phase 7A: Funnel Analytics ──

def calculate_funnel_metrics():
    from app.models import LeadEvent, ConversationState
    from app.extensions import db

    # Bulk queries (Max 1 LeadEvent, 1 ConversationState)
    events = db.session.query(LeadEvent.phone, LeadEvent.event_type).all()
    states = db.session.query(ConversationState.phone, ConversationState.is_admitted).all()

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
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()

    data = calculate_funnel_metrics()
    
    return render_template(
        "crm_analytics.html",
        key=request.args.get("key", ""),
        data=data
    )




# ── Phase 7B: Staff Performance ──

def calculate_staff_performance():
    from app.models import ConversationState, LeadEvent, ConversationMessage
    try:
        from app.models import FollowUpJob
    except ImportError:
        FollowUpJob = None
    from app.extensions import db

    # Bulk queries (Max 1 each)
    states = db.session.query(
        ConversationState.phone,
        ConversationState.assigned_staff,
        ConversationState.is_admitted,
        ConversationState.lead_score
    ).all()
    
    events = db.session.query(
        LeadEvent.phone, 
        LeadEvent.event_type
    ).all()
    
    msgs = db.session.query(
        ConversationMessage.phone,
        ConversationMessage.direction,
        ConversationMessage.created_at
    ).all()

    # FollowUpJob query for "Follow-up due count"
    pending_fu_phones = set()
    if FollowUpJob:
        pending_jobs = db.session.query(FollowUpJob.phone).filter_by(done=False).all()
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

def calculate_staff_performance_fixed():
    from app.models import ConversationState, LeadEvent, ConversationMessage
    try:
        from app.models import FollowUpJob
    except ImportError:
        FollowUpJob = None
    from app.extensions import db

    # Bulk queries
    states = db.session.query(
        ConversationState.phone,
        ConversationState.assigned_staff,
        ConversationState.is_admitted,
        ConversationState.lead_score
    ).all()
    
    events = db.session.query(LeadEvent.phone, LeadEvent.event_type).all()
    
    msgs = db.session.query(
        ConversationMessage.phone,
        ConversationMessage.direction,
        ConversationMessage.created_at
    ).all()

    pending_fu_phones = set()
    if FollowUpJob:
        pending_jobs = db.session.query(FollowUpJob.phone).filter_by(done=False).all()
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
    if request.args.get("key", "") != ADMIN_KEY:
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

def calculate_source_analytics():
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
    states = db.session.query(
        ConversationState.phone,
        ConversationState.is_admitted,
    ).all()

    # ── Bulk Query 2: all messages (phone, source, message, created_at) ──
    messages = db.session.query(
        ConversationMessage.phone,
        ConversationMessage.source,
        ConversationMessage.message,
        ConversationMessage.created_at,
    ).all()

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
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()

    data = calculate_source_analytics()

    return render_template(
        "crm_source_analytics.html",
        key=request.args.get("key", ""),
        data=data,
    )


# ── Phase 7D: Admission Analytics ──────────────────────────────────────────

def calculate_admission_analytics():
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
    rows = db.session.query(
        ConversationState.phone,
        ConversationState.is_admitted,
        ConversationState.assigned_staff,
        ConversationState.course,
        ConversationState.offer_course,
    ).all()

    # ── Bulk query 2: Fetch ADMISSION_OWNER locks (Phase 9.1) ───────────
    from app.models import LeadEvent
    import json
    admission_events = db.session.query(LeadEvent.phone, LeadEvent.event_data).filter_by(event_type="COURSE_ADMISSION").all()
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
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()

    data = calculate_admission_analytics()

    return render_template(
        "crm_admission_analytics.html",
        key=request.args.get("key", ""),
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

def calculate_revenue_analytics():
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
    states = db.session.query(
        ConversationState.phone,
        ConversationState.is_admitted,
        ConversationState.assigned_staff,
        ConversationState.course,
        ConversationState.offer_course,
        ConversationState.lead_score,
    ).all()

    # ── Bulk Query 2: LeadEvent (admission + course events only) ─────────
    events = db.session.query(
        LeadEvent.phone,
        LeadEvent.event_type,
        LeadEvent.event_data,
    ).filter(
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
        staff_key = (staff or "").strip() or "Unassigned"
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
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()

    data = calculate_revenue_analytics()

    return render_template(
        "crm_revenue_analytics.html",
        key=request.args.get("key", ""),
        data=data,
    )


# ── Phase 8.3A: Multi-Course Admission Selection ────────────────────────────

@admin_bp.route("/crm/course-admissions/<phone>", methods=["POST"])
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
           - If NOT already in admitted-event history → fire log_lead_event()
           - If ALREADY in admitted-event history → skip silently
      4. NEVER delete or modify existing COURSE_ADMISSION events.
      5. Redirect back to lead detail with msg= on success or err= on failure.

    Query count: 2 reads + N writes (one per newly admitted course, typically 0-3).
    Schema changes: none. Model changes: none. Analytics: unchanged.
    """
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()

    import json
    from app.models import LeadEvent
    from app.extensions import db
    from app.services.log_service import log_lead_event

    try:
        from app.models import ConversationState
        conversation_state = ConversationState.query.filter_by(phone=phone).first()
        staff_name = conversation_state.assigned_staff if conversation_state and conversation_state.assigned_staff else ""

        # ── 1. Read already-admitted course names (lowercase set for O(1) lookup) ──
        existing_admission_events = (
            LeadEvent.query
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
            log_lead_event(
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
                "admin.crm_lead_detail", phone=phone, key=ADMIN_KEY, msg=msg
            ))
        else:
            # Nothing new — all checked courses already recorded, or nothing checked
            return redirect(url_for(
                "admin.crm_lead_detail", phone=phone, key=ADMIN_KEY,
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
            "admin.crm_lead_detail", phone=phone, key=ADMIN_KEY,
            err="course+admission+save+failed%3A+please+try+again"
        ))


# ── Phase 8.5: CRM Health & Data Quality Dashboard ───────────────────────────

def calculate_crm_health():
    from app.models import ConversationState, LeadEvent
    from app.bot.constants import normalize_course_name
    from datetime import datetime
    import json

    # 2 bulk queries max
    leads = ConversationState.query.all()
    events = LeadEvent.query.all()

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
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()
    
    data = calculate_crm_health()
    
    return render_template(
        "crm_health.html",
        key=request.args.get("key", ""),
        data=data,
    )


# ── Phase 8.6: CRM Action Center (Read-Only) ─────────────────────────────

def calculate_action_center():
    from app.models import ConversationState, LeadEvent
    from app.bot.constants import normalize_course_name
    from datetime import datetime, timedelta
    import json

    FOLLOWUP_DAYS = 3
    now = datetime.utcnow()
    followup_threshold_date = now - timedelta(days=FOLLOWUP_DAYS)

    # 1. Fetch filtered events
    events = LeadEvent.query.filter(LeadEvent.event_type.in_([
        "COURSE_VIEWED",
        "COURSE_ENQUIRY",
        "COURSE_ADMISSION",
        "FEES_REQUESTED",
        "DEMO_REQUESTED"
    ])).all()

    # 2. Fetch all leads
    leads = ConversationState.query.all()

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
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()
    
    data = calculate_action_center()
    
    return render_template(
        "crm_action_center.html",
        key=request.args.get("key", ""),
        data=data,
    )


# ── Phase 8.8: CRM Operations Command Center ─────────────────────────────

def calculate_operations():
    from app.models import ConversationState, LeadEvent
    from app.bot.constants import normalize_course_name
    from datetime import datetime, timedelta
    import json

    now = datetime.utcnow()
    followup_threshold_date = now - timedelta(days=3)

    events = LeadEvent.query.all()
    leads = ConversationState.query.all()

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
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()
    
    data = calculate_operations()
    intel = calculate_intelligence()
    
    # Phase 9.6
    from app.models import ConversationState, LeadEvent
    intel_event_types = ["FOLLOW_UP_TASK", "FOLLOW_UP_COMPLETED"]
    auto_events = LeadEvent.query.filter(LeadEvent.event_type.in_(intel_event_types)).all()
    leads = ConversationState.query.all()
    automation = calculate_automation_intelligence(leads, auto_events)

    return render_template(
        "crm_operations.html",
        key=request.args.get("key", ""),
        data=data,
        intel=intel,
        automation=automation,
    )




# ── Phase 9.5: Operations Intelligence Layer ──────────────────────────

def calculate_intelligence():
    """
    Five intelligence modules. Exactly TWO bulk queries total.
    Query 1: LeadEvent filtered to intel types only.
    Query 2: ConversationState.query.all()
    O(L+E). No N+1. Read-only.
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
    events = LeadEvent.query.filter(
        LeadEvent.event_type.in_(intel_event_types)
    ).order_by(LeadEvent.created_at.desc()).all()

    leads = ConversationState.query.all()
    lead_map = {l.phone: l for l in leads}

    registry = load_staff_registry()
    active_staff_names = [d["display_name"] for d in registry.values() if d.get("active")]

    staff_admissions = {}
    staff_task_open = {}
    staff_task_done = {}
    task_events_map = {}
    completed_ids = set()
    phone_open_tasks = {}

    for ev in events:
        try:
            edata = json.loads(ev.event_data or "{}")
        except Exception:
            edata = {}

        if ev.event_type == "COURSE_ADMISSION":
            lead = lead_map.get(ev.phone)
            s = normalize_staff_name((lead.assigned_staff or "") if lead else "")
            if s and s != "Unassigned":
                staff_admissions[s] = staff_admissions.get(s, 0) + 1

        elif ev.event_type == "FOLLOW_UP_TASK":
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

    # Module 3: Activity Feed (newest first, max 50)
    activity_feed = []
    feed_types = {"LEAD_REASSIGNED", "COURSE_ADMISSION", "FOLLOW_UP_TASK",
                  "FOLLOW_UP_COMPLETED", "MANUAL_MESSAGE"}
    for ev in events:
        if ev.event_type not in feed_types or len(activity_feed) >= 50:
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
            from_s = normalize_staff_name(edata.get("from_staff", "?"))
            to_s = normalize_staff_name(edata.get("to_staff", "?"))
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

    # Module 4: Priority Opportunity Queue (score >= 70, not admitted, top 25)
    priority_queue = []
    for lead in leads:
        score = lead.lead_score or 0
        if score >= 70 and not lead.is_admitted:
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

def calculate_automation_intelligence(leads, events):
    """
    Phase 9.6: 2 Bulk Queries (via args). No N+1.
    Computes Aging, Recovery, Follow-Up Recommendations, and Staff Productivity.
    """
    from datetime import datetime
    import json

    now = datetime.utcnow()
    today = now.date()

    # Build maps
    lead_map = {l.phone: l for l in leads}
    
    # 1. Lead Aging Engine
    aging = {"fresh": 0, "attention": 0, "risk": 0, "dormant": 0}
    
    for lead in leads:
        if lead.is_admitted or lead.lead_status in ("Enrolled", "Dropped", "Lost"):
            continue
        days = (today - (lead.updated_at.date() if lead.updated_at else today)).days
        if days <= 3:
            aging["fresh"] += 1
        elif days <= 7:
            aging["attention"] += 1
        elif days <= 15:
            aging["risk"] += 1
        else:
            aging["dormant"] += 1

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

    # 2. Recovery Queue (score >= 50, not admitted, silent > 14 days)
    recovery_queue = []
    
    # 3. Follow-Up Recommendations
    recommendations = []
    
    for lead in leads:
        if lead.is_admitted or lead.lead_status in ("Enrolled", "Dropped", "Lost"):
            continue
            
        days = (today - (lead.updated_at.date() if lead.updated_at else today)).days
        score = lead.lead_score or 0
        
        if score >= 50 and days > 14:
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
    
    # Compute completion rates
    for s, data in staff_productivity.items():
        data["completion_rate"] = round((data["completed"] / data["created"] * 100), 1) if data["created"] > 0 else 0.0

    return {
        "aging": aging,
        "recovery_queue": recovery_queue[:20],
        "recommendations": recommendations[:20],
        "productivity": staff_productivity
    }



# ── Phase 9.2B Helpers & Routes ─────────────────────────────────────────────

def calculate_workload_scoring():
    """
    Returns a dictionary of staff name -> Workload Score.
    Score = (Lead * 1) + (Contacted * 2) + (Interested * 3)
    Only considers active staff.
    """
    from app.models import ConversationState
    from app.extensions import db
    
    registry = load_staff_registry()
    active_staff = {normalize_staff_name(data["display_name"]): data["display_name"] 
                    for code, data in registry.items() if data.get("active")}
    
    workload_query = db.session.query(
        ConversationState.assigned_staff,
        ConversationState.lead_status,
        db.func.count(ConversationState.id)
    ).group_by(ConversationState.assigned_staff, ConversationState.lead_status).all()
    
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
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()
        
    from app.models import ConversationState
    from app.extensions import db
    
    workload_query = db.session.query(
        ConversationState.assigned_staff,
        ConversationState.lead_status,
        db.func.count(ConversationState.id)
    ).group_by(ConversationState.assigned_staff, ConversationState.lead_status).all()
    
    registry = load_staff_registry()
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
def crm_unassigned_leads():
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()
        
    from app.models import ConversationState
    from sqlalchemy import or_
    
    unassigned = ConversationState.query.filter(
        or_(ConversationState.assigned_staff.is_(None), ConversationState.assigned_staff == '')
    ).order_by(ConversationState.lead_score.desc()).all()
    
    recommendations = get_staff_recommendations(limit=5)
    
    registry = load_staff_registry()
    active_staff = [data["display_name"] for code, data in registry.items() if data.get("active")]
    active_staff.sort()
    
    return render_template(
        "crm_unassigned_leads.html",
        key=request.args.get("key", ""),
        leads=unassigned,
        recommendations=recommendations,
        active_staff=active_staff,
        total=len(unassigned)
    )

@admin_bp.route("/crm/leads/unassigned/assign", methods=["POST"])
def crm_unassigned_assign():
    if request.args.get("key", "") != ADMIN_KEY:
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
    
    lead = ConversationState.query.filter_by(phone=phone).first()
    if lead and lead.assigned_staff != target_staff:
        old_staff = lead.assigned_staff
        lead.assigned_staff = target_staff
        
        log_lead_event(
            phone=lead.phone,
            event_type="LEAD_REASSIGNED",
            event_data=json.dumps({
                "from": old_staff or "Unassigned",
                "to": target_staff,
                "by": "Admin UX Assignment"
            })
        )
        db.session.commit()
        
    return redirect(url_for("admin.crm_unassigned_leads", key=key))

@admin_bp.route("/crm/leads/unassigned/auto-assign-preview", methods=["POST"])
def crm_auto_assign_preview():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return jsonify({"error": "Unauthorized"}), 401
        
    from app.models import ConversationState
    from sqlalchemy import or_
    
    unassigned = ConversationState.query.filter(
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
def crm_auto_assign_confirm():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return jsonify({"error": "Unauthorized"}), 401
        
    data = request.get_json(silent=True) or {}
    assignments = data.get("assignments", [])
    
    if not assignments:
        return jsonify({"error": "No assignments provided"}), 400
        
    from app.models import ConversationState
    from app.extensions import db
    from app.services.log_service import log_lead_event
    import json
    
    updated_count = 0
    for assign in assignments:
        phone = assign.get("phone")
        target_staff = assign.get("target_staff")
        
        if phone and target_staff:
            lead = ConversationState.query.filter_by(phone=phone).first()
            if lead and lead.assigned_staff != target_staff:
                old_staff = lead.assigned_staff
                lead.assigned_staff = target_staff
                
                log_lead_event(
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
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()
        
    registry = load_staff_registry()
    active_staff = [data["display_name"] for code, data in registry.items() if data.get("active")]
    active_staff.sort()
    
    recommendations = get_staff_recommendations(limit=5)
    
    return render_template(
        "crm_reassignment_center.html",
        key=request.args.get("key", ""),
        active_staff=active_staff,
        recommendations=recommendations,
        msg=request.args.get("msg", ""),
        err=request.args.get("err", "")
    )

@admin_bp.route("/crm/reassignment-center/preview", methods=["POST"])
def crm_reassignment_preview():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return jsonify({"error": "Unauthorized"}), 401
        
    data = request.get_json(silent=True) or {}
    phones = data.get("phones", [])
    target_staff = data.get("target_staff", "").strip()
    
    if not phones or not target_staff:
        return jsonify({"error": "Phones and Target Staff are required"}), 400
        
    from app.models import ConversationState
    leads = ConversationState.query.filter(ConversationState.phone.in_(phones)).all()
    
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
def crm_reassignment_confirm():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
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
    
    leads = ConversationState.query.filter(ConversationState.phone.in_(phones)).all()
    
    updated_count = 0
    for lead in leads:
        old_staff = lead.assigned_staff
        if old_staff != target_staff:
            lead.assigned_staff = target_staff
            updated_count += 1
            # Add LEAD_REASSIGNED event
            log_lead_event(
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





def get_all_tasks():
    from app.models import LeadEvent, ConversationState
    from datetime import datetime
    import json
    
    events = LeadEvent.query.filter(LeadEvent.event_type.in_(["FOLLOW_UP_TASK", "FOLLOW_UP_COMPLETED"])).all()
    leads = ConversationState.query.all()
    
    lead_map = {l.phone: l for l in leads}
    
    tasks = {}
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
            tid = data.get("task_id")
            if tid:
                tasks[tid] = {
                    "task_id": tid,
                    "phone": ev.phone,
                    "lead_name": lead_map.get(ev.phone, type("obj", (object,), {"name": "Unknown"})).name or "Unknown",
                    "task": data.get("task", ""),
                    "due_date": data.get("due_date", ""),
                    "staff": data.get("staff", "Unassigned"),
                    "created_by": data.get("created_by", ""),
                    "created_at": ev.created_at,
                    "status": "OPEN",
                    "completed_by": None,
                    "completed_at": None
                }

    for ev in events:
        if ev.event_type == "FOLLOW_UP_COMPLETED":
            try:
                data = json.loads(ev.event_data or "{}")
            except:
                continue
            tid = data.get("task_id")
            if tid in tasks:
                tasks[tid]["status"] = "COMPLETED"
                tasks[tid]["completed_by"] = data.get("completed_by", "")
                tasks[tid]["completed_at"] = ev.created_at

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

@admin_bp.route("/crm/tasks/create", methods=["POST"])
def crm_tasks_create():
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()
        
    phone = request.form.get("phone")
    task_title = request.form.get("task", "").strip()
    notes = request.form.get("notes", "").strip()
    due_date = request.form.get("due_date", "").strip()
    staff = request.form.get("staff", "").strip()
    key = request.args.get("key", "")
    
    if not phone or not task_title or not due_date:
        return redirect(url_for("admin.crm_lead_detail", phone=phone, key=key))
        
    from app.services.log_service import log_lead_event
    import uuid
    import json
    
    task_id = uuid.uuid4().hex
    
    payload = {
        "task_id": task_id,
        "lead_phone": phone,
        "task": task_title,
        "due_date": due_date,
        "staff": staff,
        "created_by": "Admin UX"
    }
    if notes:
        payload["notes"] = notes
        
    log_lead_event(
        phone=phone,
        event_type="FOLLOW_UP_TASK",
        event_data=json.dumps(payload)
    )
    
    return redirect(url_for("admin.crm_lead_detail", phone=phone, key=key))

@admin_bp.route("/crm/tasks/complete", methods=["POST"])
def crm_tasks_complete():
    # Supports both Form (from lead detail) and JSON (from dashboards)
    if request.args.get("key", "") != ADMIN_KEY and request.headers.get("X-Admin-Key") != ADMIN_KEY:
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
        
    if not task_id or not phone:
        if is_json:
            return jsonify({"error": "Missing parameters"}), 400
        else:
            return redirect(url_for("admin.crm_lead_detail", phone=phone, key=request.args.get("key", "")))
            
    from app.models import LeadEvent
    from app.services.log_service import log_lead_event
    import json
    
    # Duplicate completion protection
    existing = LeadEvent.query.filter_by(phone=phone, event_type="FOLLOW_UP_COMPLETED").all()
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
        log_lead_event(
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
        return redirect(url_for("admin.crm_lead_detail", phone=phone, key=request.args.get("key", "")))

@admin_bp.route("/crm/tasks/my", methods=["GET"])
def crm_my_tasks():
    if not check_auth():
        return _deny()
        
    actor = get_current_actor()
    is_staff = (actor.get("source") == "SESSION" and actor.get("role") == "STAFF")
    
    if is_staff:
        staff_name = actor.get("username")
    else:
        staff_name = request.args.get("staff", "").strip()
    open_tasks, completed_tasks = get_all_tasks()
    
    if staff_name:
        open_tasks = [t for t in open_tasks if t.get("staff") == staff_name]
        completed_tasks = [t for t in completed_tasks if t.get("staff") == staff_name]
        
    overdue = [t for t in open_tasks if t.get("is_overdue")]
    today = [t for t in open_tasks if t.get("is_today")]
    upcoming = [t for t in open_tasks if not t.get("is_overdue") and not t.get("is_today")]
    
    # Filter completed this week (simple implementation: last 7 days)
    from datetime import datetime, timedelta
    week_ago = datetime.now() - timedelta(days=7)
    recent_completed = [t for t in completed_tasks if t.get("completed_at") and t.get("completed_at") > week_ago]
    
    registry = load_staff_registry()
    active_staff = [data["display_name"] for code, data in registry.items() if data.get("active")]
    active_staff.sort()
    
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
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()
        
    open_tasks, completed_tasks = get_all_tasks()
    
    staff_summary = {}
    registry = load_staff_registry()
    for code, data in registry.items():
        if data.get("active"):
            staff_summary[data["display_name"]] = {"pending": 0, "overdue": 0, "completed": 0}
            
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
        key=request.args.get("key", ""),
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
    
    staff_name = request.args.get("staff", "").strip()
    registry = load_staff_registry()
    active_staff = [data["display_name"] for code, data in registry.items() if data.get("active")]
    active_staff.sort()
    
    if not staff_name:
        if active_staff:
            staff_name = active_staff[0]
            return redirect(url_for("admin.crm_staff_dashboard", key=request.args.get("key", ""), staff=staff_name))
            
    from app.models import ConversationState
    
    leads = ConversationState.query.filter(
        ConversationState.assigned_staff == staff_name,
        ConversationState.lead_status.notin_(["Enrolled", "Dropped", "Lost"])
    ).all()
    
    my_leads_count = len(leads)
    hot_leads_count = sum(1 for lead in leads if (lead.lead_score or 0) >= 80)
    
    admissions_count = ConversationState.query.filter(
        ConversationState.assigned_staff == staff_name,
        ConversationState.is_admitted == True
    ).count()
    
    open_tasks, _ = get_all_tasks()
    follow_ups_due = sum(1 for t in open_tasks if t.get("staff") == staff_name)

    # Phase 9.5: intelligence summary for this staff member
    intel = calculate_intelligence()
    # Find this staff's rank in leaderboard
    staff_rank = None
    staff_lb = None
    for i, entry in enumerate(intel["leaderboard"]):
        if entry["name"] == staff_name:
            staff_rank = i + 1
            staff_lb = entry
            break

    kpis = {
        "my_leads": my_leads_count,
        "hot_leads": hot_leads_count,
        "follow_ups": follow_ups_due,
        "admissions": admissions_count
    }
    
    # Phase 9.6
    from app.models import ConversationState, LeadEvent
    intel_event_types = ["FOLLOW_UP_TASK", "FOLLOW_UP_COMPLETED"]
    auto_events = LeadEvent.query.filter(LeadEvent.event_type.in_(intel_event_types)).all()
    leads = ConversationState.query.all()
    automation = calculate_automation_intelligence(leads, auto_events)
    my_productivity = automation["productivity"].get(staff_name, {"created": 0, "completed": 0, "open": 0, "overdue": 0, "completion_rate": 0.0})


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
        staff_name = actor.get("username")
    else:
        staff_name = request.args.get("staff", "").strip()
    registry = load_staff_registry()
    active_staff = [data["display_name"] for code, data in registry.items() if data.get("active")]
    active_staff.sort()
    
    from app.models import ConversationState
    
    if staff_name:
        leads = ConversationState.query.filter(
            ConversationState.assigned_staff == staff_name,
            ConversationState.lead_status.notin_(["Enrolled", "Dropped", "Lost"])
        ).order_by(ConversationState.updated_at.desc()).all()
    else:
        leads = []
        
    return render_template(
        "crm_my_leads.html",
        key=request.args.get("key", ""),
        staff_name=staff_name,
        active_staff=active_staff,
        leads=leads
    )

@admin_bp.route("/crm/staff-performance-detail", methods=["GET"])
def crm_staff_performance_detail():
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()
        
    staff_name = request.args.get("staff", "").strip()
    registry = load_staff_registry()
    active_staff = [data["display_name"] for code, data in registry.items() if data.get("active")]
    active_staff.sort()
    
    from app.models import ConversationState
    
    leads = ConversationState.query.all()
    
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
        
    for lead in leads:
        s = lead.assigned_staff
        if not s or s not in staff_metrics:
            continue
            
        staff_metrics[s]["assigned_leads"] += 1
        
        if lead.lead_status not in ["Enrolled", "Dropped", "Lost"]:
            staff_metrics[s]["active_leads"] += 1
            
        if lead.is_admitted:
            staff_metrics[s]["admissions"] += 1
            
        score = lead.lead_score or 0
        if score >= 80 and lead.lead_status not in ["Enrolled", "Dropped", "Lost"]:
            staff_metrics[s]["hot_leads"] += 1
            
        if lead.lead_status not in ["Enrolled", "Dropped", "Lost"]:
            staff_metrics[s]["total_score"] += score
            staff_metrics[s]["leads_with_score"] += 1

    open_tasks, completed_tasks = get_all_tasks()

    for s in active_staff:
        staff_metrics[s]["open_tasks"] = sum(1 for t in open_tasks if t.get("staff") == s)
        staff_metrics[s]["completed_tasks"] = sum(1 for t in completed_tasks if t.get("staff") == s)

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
        key=request.args.get("key", ""),
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
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()
    
    key = request.args.get("key", "")
    from app.extensions import db
    from sqlalchemy import func, case
    from app.models import ConversationState, LeadEvent
    import json
    
    # 1. Total & HOT Leads & Admissions
    lead_stats = db.session.query(
        ConversationState.assigned_staff,
        func.count(ConversationState.phone).label('total_leads'),
        func.sum(case((ConversationState.lead_score >= 80, 1), else_=0)).label('hot_leads'),
        func.sum(case((ConversationState.is_admitted == True, 1), else_=0)).label('admissions')
    ).group_by(ConversationState.assigned_staff).all()
    
    total_crm_leads = sum(row.total_leads for row in lead_stats)
    
    registry = load_staff_registry()
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
    events = LeadEvent.query.filter(
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
        key=key,
        staff_data=staff_data,
        total_crm_leads=total_crm_leads
    )


@admin_bp.route("/crm/staff-allocation/<staff_name>", methods=["GET"])
def crm_staff_allocation_detail(staff_name):
    # future_role = ADMIN
    # future_permission = STAFF_REALLOCATION
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()
        
    key = request.args.get("key", "")
    from app.models import ConversationState
    
    actual_name = "" if staff_name == "Unassigned" else staff_name
    
    if actual_name == "":
        leads = ConversationState.query.filter(
            (ConversationState.assigned_staff == None) | (ConversationState.assigned_staff == "")
        ).all()
    else:
        from sqlalchemy import func
        leads = ConversationState.query.filter(
            func.lower(func.trim(ConversationState.assigned_staff)) == actual_name.lower()
        ).all()
        
    registry = load_staff_registry()
    active_staff = [data["display_name"] for code, data in registry.items() if data.get("active")]
    active_staff.sort()
    
    return render_template(
        "crm_staff_allocation_detail.html",
        key=key,
        staff_name=staff_name,
        leads=leads,
        active_staff=active_staff
    )


@admin_bp.route("/crm/staff-allocation/check-deactivation/<staff_name>", methods=["GET"])
def crm_staff_allocation_check(staff_name):
    # future_role = ADMIN
    if request.args.get("key", "") != ADMIN_KEY:
        return jsonify({"error": "Unauthorized"}), 401
        
    from app.models import ConversationState, LeadEvent
    import json
    
    if staff_name == "Unassigned":
        return jsonify({"safe": False, "reason": "Cannot deactivate Unassigned"})
        
    # 1. Check Leads
    from sqlalchemy import func
    lead_count = ConversationState.query.filter(func.lower(func.trim(ConversationState.assigned_staff)) == staff_name.lower()).count()
    admission_count = ConversationState.query.filter(func.lower(func.trim(ConversationState.assigned_staff)) == staff_name.lower(), ConversationState.is_admitted == True).count()
    
    # 2. Check Open Tasks
    events = LeadEvent.query.filter(
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
def auth_debug():
    actor = get_current_actor()
    logging.info(f"AUTH source={actor['source']} user={actor['username']}")
    return jsonify(actor)



@admin_bp.route("/crm/login", methods=["GET", "POST"])
def crm_login():
    if current_user.is_authenticated:
        return redirect(url_for("admin.crm_home"))
        
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        
        from app.models import User
        from app.extensions import db
        user = User.query.filter_by(username=username).first()
        if user and user.is_active and check_password_hash(user.password_hash, password):
            user.last_login = datetime.utcnow()
            db.session.commit()
            login_user(user)
            return redirect(url_for("admin.crm_home"))
            
        flash("Invalid credentials or inactive account.", "danger")
        
    return render_template("crm_login.html")


@admin_bp.route("/crm/logout", methods=["GET"])
@login_required
def crm_logout():
    logout_user()
    session.clear()
    return redirect(url_for("admin.crm_login"))


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

