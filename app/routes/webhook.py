import threading
from flask import Blueprint, request, jsonify, current_app
from app.config import VERIFY_TOKEN
from app.state import phone_exists
from app.bot.router import smart_reply
from app.services.whatsapp_service import send_reply
from app.services.crm_service import save_lead_to_sheets
from app.services.followup_service import schedule_followups
from app.services.log_service import log_message_in_thread, save_conversation_message_in_thread, log_lead_event_in_thread
from app.models import ConversationMessage, ConversationState
from app.extensions import db

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
        wamid        = message.get("id", "")
        contact_name = contacts[0].get("profile", {}).get("name", "Student") if contacts else "Student"

        # Phase 12-D2B: Extract tenant context early
        from app.services.log_service import _get_default_tenant_id
        tenant_id = _get_default_tenant_id()

        # Phase 11-D1 Task C: Deduplication Protection
        if wamid:
            existing = ConversationMessage.query.filter_by(wa_message_id=wamid).first()
            if existing:
                print(f"♻️ Webhook deduplicated: {wamid}")
                return jsonify({"status": "ok", "reason": "duplicate"}), 200

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

        # Phase 11-D1 Task D & Phase 11-D2A: Opt-Out & Opt-In Infrastructure
        low_text = msg_text.lower()
        if low_text in {"stop", "unsubscribe", "cancel"}:
            state = ConversationState.query.filter_by(phone=from_number, tenant_id=tenant_id).first()
            if state:
                state.is_opted_out = True
                db.session.commit()
                print(f"🚫 Opt-out triggered for {from_number}")
                # We can optionally send an opt-out confirmation here, but we just halt workflows
                return jsonify({"status": "ok"}), 200
        elif low_text in {"start", "resume", "unstop"}:
            state = ConversationState.query.filter_by(phone=from_number, tenant_id=tenant_id).first()
            if state and getattr(state, 'is_opted_out', False):
                state.is_opted_out = False
                db.session.commit()
                print(f"✅ Opt-in recovery triggered for {from_number}")
                # Allow the message to continue processing so AI can reply or workflows can resume

        is_new_lead = not phone_exists(from_number, tenant_id=tenant_id)

        # Capture app ref once in request context — safe to pass to daemon threads
        _app = current_app._get_current_object()

        if is_new_lead:
            threading.Thread(
                target=log_lead_event_in_thread,
                kwargs=dict(app=_app, phone=from_number, event_type="LEAD_CREATED", tenant_id=tenant_id),
                daemon=True,
            ).start()
            threading.Thread(
                target=log_lead_event_in_thread,
                kwargs=dict(app=_app, phone=from_number, event_type="FIRST_MESSAGE_RECEIVED", tenant_id=tenant_id),
                daemon=True,
            ).start()

        # ── Log inbound user message (MessageLog daemon thread) ──
        threading.Thread(
            target=log_message_in_thread,
            kwargs=dict(
                app=_app,
                phone=from_number,
                direction="inbound",
                message_type="user",
                message_text=msg_text,
                tenant_id=tenant_id,
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
                wa_message_id=wamid,
                tenant_id=tenant_id,
            ),
            daemon=True,
        ).start()

        # ── CRM save (background) ──
        threading.Thread(
            target=save_lead_to_sheets,
            args=(from_number, contact_name, msg_text, is_new_lead),
        ).start()

        # Phase 11-D3B2: Deliver Pending Messages (Interceptor Fallback)
        from app.models import PendingMessage
        from app.services.whatsapp_service import send_text
        pending_msgs = PendingMessage.query.filter_by(phone=from_number, tenant_id=tenant_id).order_by(PendingMessage.created_at.asc()).all()
        if pending_msgs:
            print(f"📦 Delivering {len(pending_msgs)} pending messages to {from_number}")
            for pm in pending_msgs:
                send_text(from_number, pm.text)
                db.session.delete(pm)
            db.session.commit()
            
            # If the user just replied "Yes" to our re-engagement template, 
            # suppress the AI to avoid confusing double-replies
            if msg_text.lower().strip() in {"yes", "y", "ok", "okay"}:
                return jsonify({"status": "ok"}), 200

        # ── Generate reply ──
        reply_text, preset = smart_reply(msg_text, contact_name, from_number, is_new_lead, tenant_id=tenant_id)
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
                tenant_id=tenant_id,
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
                tenant_id=tenant_id,
            ),
            daemon=True,
        ).start()

        if is_new_lead:
            threading.Thread(
                target=log_lead_event_in_thread,
                kwargs=dict(app=_app, phone=from_number, event_type="AI_RESPONSE_SENT", tenant_id=tenant_id),
                daemon=True,
            ).start()

        # ── Schedule follow-ups for new leads ──
        if is_new_lead:
            schedule_followups(from_number, contact_name, tenant_id=tenant_id)

    except Exception as e:
        print(f"❌ Webhook error: {e}")

    return jsonify({"status": "ok"}), 200
