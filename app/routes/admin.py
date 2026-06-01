import logging
from sqlalchemy import or_
from flask import Blueprint, request, jsonify, render_template, redirect, flash, url_for
from app.config import ADMIN_KEY
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


@admin_bp.route("/crm/leads", methods=["GET"])
def crm_leads():
    if request.args.get("key", "") != ADMIN_KEY:
        return (
            "<html><body style='font-family:sans-serif;text-align:center;"
            "padding:50px;background:#0a0f0d;color:#25D366'>"
            "<h2>\U0001f512 Access Denied</h2>"
            "<p style='color:#888'>URL-il ?key=YOUR_ADMIN_KEY add cheyyuka</p>"
            "</body></html>"
        ), 403

    from app.models import ConversationState
    from app.extensions import db

    PAGE_SIZE = 25

    # \u2500\u2500 Query params \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    page            = max(1, request.args.get("page", 1, type=int))
    search          = request.args.get("search", "").strip()
    stage_filter    = request.args.get("stage", "").strip()
    admitted_filter = request.args.get("admitted", "").strip()
    key             = request.args.get("key", "")

    # \u2500\u2500 Build query safely \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    q = ConversationState.query

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
    for ev in events:
        if ev.event_type in ("COURSE_ENQUIRY", "COURSE_ADMISSION"):
            try:
                parsed = _json.loads(ev.event_data or "{}")
                name   = (parsed.get("course") or "").strip()
                if name:
                    event_course_map[ev.id] = normalize_course_name(name)
            except Exception:
                pass

    # ── Phase 8.4: Lead Portfolio — zero new queries ─────────────────────────
    # Passes already-loaded lead ORM row, events list, and course_journey dict.
    # All aggregation happens in Python memory inside calculate_lead_portfolio().
    portfolio = calculate_lead_portfolio(lead, events, course_journey)

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
        lead.lead_status    = request.form.get("lead_status",    "").strip() or lead.lead_status
        lead.assigned_staff = request.form.get("assigned_staff", "").strip() or None
        lead.notes          = request.form.get("notes",          "").strip() or None

        score_raw = request.form.get("lead_score", "").strip()
        if score_raw.isdigit():
            lead.lead_score = max(0, min(100, int(score_raw)))

        # ── Snapshot values before commit for post-commit event firing ──
        new_course    = (lead.course or "").strip()
        new_admitted  = request.form.get("is_admitted") == "1"

        # ── Phase 8.2 Gap 2: Hard block — admission requires assigned staff ──────
        # lead.assigned_staff was already written from the form at L665 above.
        # If marking as admitted and no staff is assigned, abort before any commit.
        # Uses the same err= redirect pattern as crm_lead_send().
        if new_admitted and not (lead.assigned_staff or "").strip():
            db.session.rollback()
            return redirect(url_for(
                "admin.crm_lead_detail", phone=phone, key=ADMIN_KEY,
                err="Admission+blocked%3A+please+assign+a+staff+member+before+marking+this+lead+as+admitted."
            ))

        # ── Phase 8.2 Gap 3: Auto-promote lead_status → Enrolled on admission ────
        # If the lead is being admitted and their status is still at an early
        # pipeline stage, silently promote to Enrolled.
        # Mirrors the JS auto-promote in crm_lead_detail.html.
        # No change if lead_status is already Enrolled/Lost/etc.
        _PROMOTE_STATUSES = {"Lead", "Contacted", "Interested"}
        if new_admitted and (lead.lead_status or "").strip() in _PROMOTE_STATUSES:
            lead.lead_status = "Enrolled"

        lead.is_admitted = new_admitted

        db.session.commit()

        # ── Phase 7E: Fire course events AFTER successful commit ──────────
        # Uses existing log_lead_event() which is already safe / never-raises.
        import json
        from app.services.log_service import log_lead_event
        from app.models import LeadEvent

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
                    event_data=json.dumps({"course": new_course}),
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
            from app.services.log_service import save_conversation_message
            save_conversation_message(
                phone=phone,
                direction="outgoing",
                message=message,
                message_type="text",
                source="manual",
                staff_name=None,    # extend when staff auth added (Phase 6+)
                wa_message_id=None, # extend when WA API response parsed (Phase 6+)
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
        if not assigned_staff:
            continue
            
        staff = assigned_staff
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
        if not assigned_staff:
            continue
            
        staff = assigned_staff
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
        staff_key = (staff or "").strip() or "Unassigned"
        if staff_key not in staff_stats:
            staff_stats[staff_key] = {"leads": 0, "admissions": 0}
        staff_stats[staff_key]["leads"] += 1
        if admitted:
            staff_stats[staff_key]["admissions"] += 1

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
                event_data=json.dumps({"course": course}),
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

    for lead in leads:
        p = lead.phone
        staff = (lead.assigned_staff or "").strip()
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

    # Process leads in a single O(L) pass
    for lead in leads:
        p = lead.phone
        staff = (lead.assigned_staff or "").strip()
        score = lead.lead_score or 0
        is_admitted = lead.is_admitted
        lead_name = lead.name or "Unknown"
        
        pd = phone_data.get(p, {})
        has_demo = pd.get("has_demo", False)
        has_fees = pd.get("has_fees", False)
        enquiries_set = pd.get("enquiries", set())
        has_admissions = len(pd.get("admissions", set())) > 0

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

        # 1. ADMISSION READY
        if has_demo and has_fees and not is_admitted and staff:
            admission_ready.append({
                "phone": p, "name": lead_name, "staff": staff,
                "course": course_interest, "score": score
            })

        # 2. HOT LEADS
        if score >= 80 and not is_admitted:
            hot_leads.append({
                "phone": p, "name": lead_name, "score": score, "staff": staff or "—"
            })

        # 3. MULTI-COURSE OPPORTUNITIES
        if len(enquiries_set) >= 3 and not has_admissions:
            multi_course.append({
                "phone": p, "name": lead_name, "course_count": len(enquiries_set),
                "courses": course_interest, "staff": staff or "—"
            })

        # 4. DEMO PENDING
        if has_demo and not is_admitted:
            demo_pending.append({
                "phone": p, "name": lead_name, "staff": staff or "—", "course": course_interest
            })

        # 5. UNASSIGNED HOT LEADS
        if score >= 80 and not staff:
            unassigned_hot.append({
                "phone": p, "name": lead_name, "score": score
            })

        # 6. FOLLOW-UP REQUIRED
        if staff and not is_admitted and latest_act and latest_act < followup_threshold_date:
            followup_required.append({
                "phone": p, "name": lead_name, "staff": staff, "days": days_since_activity
            })

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

