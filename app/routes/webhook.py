import threading
from flask import Blueprint, request, jsonify, current_app
from app.config import VERIFY_TOKEN
from app.state import phone_exists
from app.bot.router import smart_reply
from app.services.whatsapp_service import send_reply
from app.services.crm_service import save_lead_to_sheets
from app.services.followup_service import schedule_followups
from app.services.log_service import log_message_in_thread, save_conversation_message_in_thread

webhook_bp = Blueprint("webhook", __name__)

@webhook_bp.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ Webhook verified")
        return challenge, 200
    return "Forbidden", 403


@webhook_bp.route("/webhook", methods=["POST"])
def receive_message():
    data = request.get_json(silent=True) or {}
    try:
        entry   = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value   = changes.get("value", {})

        # Ignore delivery / read receipts
        if "statuses" in value:
            return jsonify({"status": "ok"}), 200

        messages = value.get("messages", [])
        contacts = value.get("contacts", [])
        if not messages:
            return jsonify({"status": "ok"}), 200

        message      = messages[0]
        from_number  = message.get("from", "")
        msg_type     = message.get("type", "")
        contact_name = contacts[0].get("profile", {}).get("name", "Student") if contacts else "Student"

        # Parse message text based on type
        if msg_type == "text":
            msg_text = message["text"]["body"].strip()
        elif msg_type == "interactive":
            itype = message["interactive"]["type"]
            if itype == "button_reply":
                msg_text = message["interactive"]["button_reply"]["id"]
            elif itype == "list_reply":
                msg_text = message["interactive"]["list_reply"]["id"]
            else:
                msg_text = f"[interactive_{itype}]"
        elif msg_type == "button":
            msg_text = message["button"]["text"]
        else:
            # Unsupported type — ignore silently
            return jsonify({"status": "ok"}), 200

        print(f"📱 {contact_name} ({from_number}): {msg_text}")

        is_new_lead = not phone_exists(from_number)

        # Capture app ref once in request context — safe to pass to daemon threads
        _app = current_app._get_current_object()

        # ── Log inbound user message (MessageLog daemon thread) ──
        threading.Thread(
            target=log_message_in_thread,
            kwargs=dict(
                app=_app,
                phone=from_number,
                direction="inbound",
                message_type="user",
                message_text=msg_text,
            ),
            daemon=True,
        ).start()

        # ── Persist inbound to ConversationMessage (daemon thread, app-context-safe) ──
        threading.Thread(
            target=save_conversation_message_in_thread,
            kwargs=dict(
                app=_app,
                phone=from_number,
                direction="incoming",
                message=msg_text,
                message_type=msg_type,
                source="user",
            ),
            daemon=True,
        ).start()

        # ── CRM save (background) ──
        threading.Thread(
            target=save_lead_to_sheets,
            args=(from_number, contact_name, msg_text, is_new_lead),
        ).start()

        # ── Generate reply ──
        reply_text, preset = smart_reply(msg_text, contact_name, from_number, is_new_lead)
        send_reply(from_number, reply_text, preset)

        # ── Log outbound AI reply (MessageLog daemon thread) ──
        threading.Thread(
            target=log_message_in_thread,
            kwargs=dict(
                app=_app,
                phone=from_number,
                direction="outbound",
                message_type="ai",
                message_text=reply_text,
            ),
            daemon=True,
        ).start()

        # ── Persist AI reply to ConversationMessage (daemon thread, app-context-safe) ──
        threading.Thread(
            target=save_conversation_message_in_thread,
            kwargs=dict(
                app=_app,
                phone=from_number,
                direction="outgoing",
                message=reply_text,
                message_type="text",
                source="ai",
            ),
            daemon=True,
        ).start()

        # ── Schedule follow-ups for new leads ──
        if is_new_lead:
            schedule_followups(from_number, contact_name)

    except Exception as e:
        print(f"❌ Webhook error: {e}")

    return jsonify({"status": "ok"}), 200
