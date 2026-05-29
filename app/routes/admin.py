import logging
from sqlalchemy import or_
from flask import Blueprint, request, jsonify, render_template, redirect
from app.config import ADMIN_KEY
from app.state import count_states, count_pending_followups, get_all_states, get_stage_breakdown
from app.services.whatsapp_service import send_text

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

    # \u2500\u2500 Dashboard metrics \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    total_leads    = ConversationState.query.count()
    admitted_count = ConversationState.query.filter_by(is_admitted=True).count()
    pending_fu     = count_pending_followups()

    # \u2500\u2500 All distinct stages for filter dropdown \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    stages = [r[0] for r in db.session.query(ConversationState.stage).distinct().all() if r[0]]

    return render_template(
        "crm_leads.html",
        pagination=pagination,
        leads=pagination.items,
        total_leads=total_leads,
        admitted_count=admitted_count,
        pending_fu=pending_fu,
        stages=stages,
        search=search,
        stage_filter=stage_filter,
        admitted_filter=admitted_filter,
        key=key,
        page=page,
    )


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

    return render_template(
        "crm_lead_detail.html",
        lead=lead,
        logs=logs,
        timeline=timeline,
        metrics=metrics,
        search_q=search_q,
        source_q=source_q,
        range_q=range_q,
        key=request.args.get("key", ""),
        msg=request.args.get("msg", ""),
        err=request.args.get("err", ""),
        events=events,
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

        lead.is_admitted = request.form.get("is_admitted") == "1"

        db.session.commit()
        return redirect(f"/crm/lead/{phone}?key={key}&msg=CRM+updated+successfully{qs}")

    except Exception:
        db.session.rollback()
        logging.exception(f"CRM update failed for {phone}")
        return redirect(f"/crm/lead/{phone}?key={key}&err=Unexpected+server+error{qs}")


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
