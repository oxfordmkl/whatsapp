import logging
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
            db.or_(
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

    from app.models import ConversationState, MessageLog

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

    return render_template(
        "crm_lead_detail.html",
        lead=lead,
        logs=logs,
        key=request.args.get("key", ""),
        msg=request.args.get("msg", ""),
        err=request.args.get("err", ""),
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

    try:
        lead.lead_status    = request.form.get("lead_status",    "").strip() or lead.lead_status
        lead.assigned_staff = request.form.get("assigned_staff", "").strip() or None
        lead.notes          = request.form.get("notes",          "").strip() or None

        score_raw = request.form.get("lead_score", "").strip()
        if score_raw.isdigit():
            lead.lead_score = max(0, min(100, int(score_raw)))

        lead.is_admitted = request.form.get("is_admitted") == "1"

        db.session.commit()
        return redirect(f"/crm/lead/{phone}?key={key}&msg=CRM+updated+successfully")

    except Exception:
        db.session.rollback()
        logging.exception(f"CRM update failed for {phone}")
        return redirect(f"/crm/lead/{phone}?key={key}&err=Unexpected+server+error")


# ── POST /crm/lead/<phone>/send ────────────────────────────────────────────

@admin_bp.route("/crm/lead/<phone>/send", methods=["POST"])
def crm_lead_send(phone):
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()

    key     = request.args.get("key", "")
    message = request.form.get("manual_message", "").strip()

    if not message:
        return redirect(f"/crm/lead/{phone}?key={key}&err=Message+cannot+be+empty")

    try:
        r = send_text(phone, message)
        if r.status_code == 200:
            # ── Log manual outbound message ──
            from app.services.log_service import log_message
            log_message(
                phone=phone,
                direction="outbound",
                message_type="manual",
                message_text=message,
            )
            return redirect(f"/crm/lead/{phone}?key={key}&msg=Message+sent+successfully")
        else:
            return redirect(f"/crm/lead/{phone}?key={key}&err=WhatsApp+API+returned+an+error")
    except Exception:
        logging.exception(f"Manual WhatsApp send failed for {phone}")
        return redirect(f"/crm/lead/{phone}?key={key}&err=Unexpected+server+error")
