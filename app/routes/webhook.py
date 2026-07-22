import logging
import threading
from flask import Blueprint, request, jsonify, current_app
from app.config import VERIFY_TOKEN
from app.state import phone_exists, resolve_is_new_lead
from app.bot.router import smart_reply
from app.services.whatsapp_service import send_reply
from app.services.crm_service import save_lead_to_sheets
from app.services.followup_service import schedule_followups
from app.services.log_service import log_message_in_thread, save_conversation_message_in_thread, log_lead_event_in_thread
from app.models import ConversationMessage, ConversationState
from app.extensions import db

logger = logging.getLogger(__name__)

webhook_bp = Blueprint("webhook", __name__)

@webhook_bp.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("✅ Webhook verified")
        return challenge, 200
    return "Forbidden", 403


@webhook_bp.route("/webhook", methods=["POST"])
def receive_message():
    data = request.get_json(silent=True) or {}
    from app import perf
    try:
        perf.start()
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

        # Phase 13-B4D2: Dynamic Webhook Tenant Routing
        phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
        tenant_id = None
        
        from app.models import Tenant
        tenant = Tenant.query.filter_by(waba_phone_number_id=phone_number_id).first()
        if tenant:
            if tenant.status not in ["ACTIVE", "TRIAL"]:
                logger.warning(f"⚠️ Webhook dropped: Tenant {tenant.id} is {tenant.status}")
                return jsonify({"status": "ok"}), 200
            tenant_id = tenant.id
        else:
            # Grace-period fallback to primary Oxford tenant
            if phone_number_id == current_app.config.get("PHONE_NUMBER_ID"):
                env_phone_id = str(
                current_app.config.get("PHONE_NUMBER_ID", "")
                ).strip()
                incoming_phone_id = str(phone_number_id).strip()    
                if env_phone_id and incoming_phone_id == env_phone_id:
                    tenant_id = current_app.config.get("PRIMARY_TENANT_ID")
                    logger.warning(f"⚠️ Webhook warning: Unregistered WABA Phone ID {phone_number_id}, but matched primary tenant fallback")
            else:
                logger.warning(f"⚠️ Webhook dropped: Unknown WABA Phone ID {phone_number_id}")
                return jsonify({"status": "ok"}), 200

        # Phase 11-D1 Task C: Deduplication Protection
        if wamid:
            existing = ConversationMessage.query.filter_by(wa_message_id=wamid).first()
            if existing:
                logger.info(f"♻️ Webhook deduplicated: {wamid}")
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

        # Truncated: full message bodies are lead PII and stay out of logs
        # (same rationale as the Sprint 1 [DIAG] removal).
        logger.info(f"📱 {contact_name} ({from_number}) tenant={tenant_id}: {msg_text[:40]!r}...")

        # Phase 11-D1 Task D & Phase 11-D2A: Opt-Out & Opt-In Infrastructure
        low_text = msg_text.lower()
        if low_text in {"stop", "unsubscribe", "cancel"}:
            state = ConversationState.query.filter_by(phone=from_number, tenant_id=tenant_id).first()
            if state:
                state.is_opted_out = True
                db.session.commit()
                logger.warning(f"🚫 Opt-out triggered for {from_number}")
                # We can optionally send an opt-out confirmation here, but we just halt workflows
                return jsonify({"status": "ok"}), 200
        elif low_text in {"start", "resume", "unstop"}:
            state = ConversationState.query.filter_by(phone=from_number, tenant_id=tenant_id).first()
            if state and getattr(state, 'is_opted_out', False):
                state.is_opted_out = False
                db.session.commit()
                logger.info(f"✅ Opt-in recovery triggered for {from_number}")
                # Allow the message to continue processing so AI can reply or workflows can resume

        # Phase 1.5.5E: gated by STATE_MERGE_LOOKUP. Flag OFF → identical to
        # `not phone_exists(...)`. Flag ON → derives is_new_lead from a single
        # load-or-create that the later smart_reply() reuses (one fewer SELECT).
        is_new_lead = resolve_is_new_lead(from_number, contact_name, tenant_id=tenant_id)

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
            args=(from_number, contact_name, msg_text, is_new_lead, tenant_id),
        ).start()

        # Phase 11-D3B2: Deliver Pending Messages (Interceptor Fallback)
        from app.models import PendingMessage
        from app.services.whatsapp_service import send_text
        pending_msgs = PendingMessage.query.filter_by(phone=from_number, tenant_id=tenant_id).order_by(PendingMessage.created_at.asc()).all()
        if pending_msgs:
            logger.info(f"📦 Delivering {len(pending_msgs)} pending messages to {from_number}")
            for pm in pending_msgs:
                send_text(from_number, pm.text, tenant_id=tenant_id)
                db.session.delete(pm)
            db.session.commit()
            
            # If the user just replied "Yes" to our re-engagement template, 
            # suppress the AI to avoid confusing double-replies
            if msg_text.lower().strip() in {"yes", "y", "ok", "okay"}:
                return jsonify({"status": "ok"}), 200

        # ── Generate reply ──
        # Phase 1.5.5D: gated by STATE_UOW_CONTEXT. Flag OFF → no-op scope,
        # behavior identical to before. Flag ON → state writes made during
        # smart_reply are deferred and committed once when the scope exits,
        # which is AFTER send_reply — keeping the commit off the reply path.
        # Business logic and routing are unchanged; only the transaction
        # boundary moves.
        from app.persistence.scope import state_unit_of_work, flush_state_writes
        perf.mark("router_start")
        with state_unit_of_work():
            reply_text, preset = smart_reply(msg_text, contact_name, from_number, is_new_lead, tenant_id=tenant_id, wa_message_id=wamid)
            send_reply(
            from_number,
            reply_text,
            preset,
            tenant_id=tenant_id
            )
            # Explicit flush after send_reply; the durable commit is the scope exit.
            flush_state_writes()
        # Phase 1.3A-2: Conversation Memory observe mode (metrics only).
        # Gated by MEMORY_OBSERVE_MODE (default OFF). Runs AFTER the reply is
        # sent, only for AI-eligible requests. Result is discarded — memory is
        # NEVER injected into Gemini in this phase.
        from app.memory.observer import observe_memory
        observe_memory(tenant_id, from_number, exclude_message_id=wamid)
        # Emit a correlated [PERF] block only for Gemini-powered replies.
        perf.report(only_if_stage="gemini_start")

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
        logger.error(f"❌ Webhook error: {e}")

    return jsonify({"status": "ok"}), 200
