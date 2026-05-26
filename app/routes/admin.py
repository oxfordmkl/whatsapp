from flask import Blueprint, request, jsonify
from app.config import ADMIN_KEY
from app.state import conversation_state, follow_up_queue
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
        "total_leads":       len(conversation_state),
        "pending_followups": sum(1 for f in follow_up_queue if not f["done"]),
        "stage_breakdown": {
            s: sum(1 for v in conversation_state.values() if v.get("stage") == s)
            for s in {"new", "goal_selection", "course_recommendation", "course_viewed",
                      "demo_time_ask", "demo_date_ask", "demo_booked",
                      "offer_menu", "payment_pending", "enrolled", "not_sure", "done"}
        },
        "active_conversations": [
            {
                "name":        v.get("name", ""),
                "stage":       v.get("stage", ""),
                "last_text":   v.get("last_text", ""),
                "last_active": v.get("last_msg", ""),
                "course":      v.get("course", ""),
            }
            for v in conversation_state.values()
        ],
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
