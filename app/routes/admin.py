from flask import Blueprint, request, jsonify, render_template
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
