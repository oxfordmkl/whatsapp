import hashlib
import hmac
import logging
import threading
from flask import Blueprint, request, jsonify, current_app
from app.config import VERIFY_TOKEN, META_APP_SECRET
from app.state import phone_exists, resolve_is_new_lead
from app.bot.router import smart_reply
from app.services.whatsapp_service import send_reply
from app.services.crm_service import save_lead_to_sheets
from app.services.followup_service import schedule_followups
from app.services.log_service import log_message_in_thread, save_conversation_message_in_thread, log_lead_event_in_thread
from app.services.phone_service import mask_destination as _mask
from app.models import ConversationMessage, ConversationState
from app.extensions import db

logger = logging.getLogger(__name__)

webhook_bp = Blueprint("webhook", __name__)

@webhook_bp.route("/webhook", methods=["GET"])
def verify_webhook():
    """Meta's subscription handshake.

    Phase RC2.5.19-D: FAIL-CLOSED and constant-time. VERIFY_TOKEN no longer has
    a committed default -- the old default was published in the repository and
    its docs -- so an unset token now refuses every handshake instead of
    accepting a publicly known one. The token is never logged.
    """
    expected  = current_app.config.get("VERIFY_TOKEN") or VERIFY_TOKEN
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token") or ""
    challenge = request.args.get("hub.challenge")
    if not expected:
        logger.error("❌ Webhook verification refused: VERIFY_TOKEN is not "
                     "configured (fail-closed)")
        return "Forbidden", 403
    if mode == "subscribe" and hmac.compare_digest(
            token.encode("utf-8"), expected.encode("utf-8")):
        logger.info("✅ Webhook verified")
        return challenge, 200
    return "Forbidden", 403


def verify_meta_signature() -> bool:
    """True when the inbound webhook POST may be trusted.

    Phase 14C. VERIFY_TOKEN authenticates only the GET subscription handshake;
    it is never sent on delivered messages. Without this check the endpoint
    accepted any payload from anyone who knew the URL — allowing forged inbound
    messages attributed to any tenant, which create leads, drive AI replies and
    consume that tenant's WhatsApp quota.

    Meta signs every POST with HMAC-SHA256 of the RAW body under the app
    secret, in X-Hub-Signature-256.

    Phase RC2.5.5a. This check is now FAIL-CLOSED. Previously a missing
    META_APP_SECRET returned True, so the endpoint silently accepted
    unauthenticated payloads and only logged a WARNING. Production already had
    the secret configured, so enforcement was live — but it rested on a
    configuration value rather than on the code. A blank rotation, a dropped
    variable or a fresh environment would have re-opened the endpoint with no
    failure signal. That is what this closes: the guarantee is now structural.

    Consequence, accepted deliberately: with no secret configured, inbound
    WhatsApp processing STOPS rather than proceeding unauthenticated.
    """
    secret = current_app.config.get("META_APP_SECRET") or META_APP_SECRET
    if not secret:
        logger.error(
            "❌ Webhook rejected: META_APP_SECRET is not configured — inbound "
            "payloads cannot be authenticated and are refused (fail-closed)")
        return False

    header = request.headers.get("X-Hub-Signature-256", "")
    if not header.startswith("sha256="):
        logger.warning("⚠️ Webhook rejected: missing X-Hub-Signature-256")
        return False

    expected = hmac.new(secret.encode("utf-8"),
                        request.get_data(), hashlib.sha256).hexdigest()
    # compare_digest, not ==, so a mismatch cannot be found by timing.
    if not hmac.compare_digest(expected, header[len("sha256="):]):
        logger.warning("⚠️ Webhook rejected: invalid signature")
        return False
    return True


@webhook_bp.route("/webhook", methods=["POST"])
def receive_message():
    # Phase 14C: authenticate BEFORE parsing or touching the database.
    if not verify_meta_signature():
        return jsonify({"status": "forbidden"}), 403

    data = request.get_json(silent=True)
    from app import perf
    perf.start()

    # Phase RC2.5.19-D: process EVERY entry, EVERY change and EVERY message.
    # Before this phase only entry[0].changes[0].messages[0] was read, and a
    # `statuses` key in that first change acknowledged the whole delivery.
    # Once one Meta app serves many tenants, one delivery can carry several
    # tenants' events; each change is now resolved and gated on its own, and
    # one failing item cannot drop its siblings.
    #
    # The acknowledgement is ALWAYS 200 once the signature is valid, exactly as
    # before: Meta retries non-2xx and can disable the subscription after
    # sustained failures, so one bad event must not degrade every tenant.
    for value in _iter_change_values(data):
        try:
            _process_change(value)
        except Exception as e:                                  # noqa: BLE001
            _rollback_quietly()
            # Class only: an exception message can echo payload content.
            logger.error("❌ Webhook change failed: %s", type(e).__name__)

    return jsonify({"status": "ok"}), 200


def _as_list(v):
    return v if isinstance(v, list) else []


def _iter_change_values(data):
    """Every `value` object in the delivery, in delivery order. Anything that
    is not the documented shape is skipped, never guessed at."""
    if not isinstance(data, dict):
        return
    for entry in _as_list(data.get("entry")):
        if not isinstance(entry, dict):
            continue
        for change in _as_list(entry.get("changes")):
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if isinstance(value, dict):
                yield value


def _rollback_quietly():
    try:
        db.session.rollback()
    except Exception:                                           # noqa: BLE001
        logger.error("❌ Webhook rollback failed")


def _resolve_accepting_tenant(value):
    """The tenant id this change belongs to, or None to drop it.

    Resolution is by metadata.phone_number_id ONLY (unique since RC2.4.2).
    There is no fallback tenant: unknown, missing or malformed metadata drops
    the change (Phase RC2.5.5a removed the PRIMARY_TENANT_ID grace fallback),
    and a tenant whose status does not accept inbound WhatsApp is dropped
    before any write. Each change is resolved on its own.
    """
    metadata = value.get("metadata")
    phone_number_id = metadata.get("phone_number_id") if isinstance(metadata, dict) else None
    if not isinstance(phone_number_id, str) or not phone_number_id:
        logger.warning("⚠️ Webhook change dropped: missing phone_number_id")
        return None

    from app.models import Tenant
    tenant = Tenant.query.filter_by(waba_phone_number_id=phone_number_id).first()
    if tenant is None:
        # An unregistered phone_number_id must never resolve to a tenant. The
        # acknowledgement stays 200 (see receive_message).
        logger.warning("⚠️ Webhook change dropped: unknown WABA phone id %s",
                       phone_number_id)
        return None

    # Phase RC2.5.19-C: the status rule lives in whatsapp_service
    # (INBOUND_ACCEPTED_TENANT_STATUSES). Phase RC2.5.19-D: PENDING stays
    # dropped -- ACTIVE and TRIAL only.
    from app.services.whatsapp_service import tenant_accepts_whatsapp_inbound
    if not tenant_accepts_whatsapp_inbound(tenant):
        logger.warning("⚠️ Webhook change dropped: tenant %s is %s",
                       tenant.id, tenant.status)
        return None
    return tenant.id


def _process_change(value):
    # Delivery / read receipts are ignored, as before -- but only for THIS
    # change; they no longer suppress messages elsewhere in the delivery.
    messages = _as_list(value.get("messages"))
    if not messages:
        return

    tenant_id = _resolve_accepting_tenant(value)
    if tenant_id is None:
        return

    contacts = [c for c in _as_list(value.get("contacts")) if isinstance(c, dict)]
    for message in messages:
        try:
            if isinstance(message, dict):
                _process_message(message, tenant_id, contacts)
        except Exception as e:                                  # noqa: BLE001
            _rollback_quietly()
            logger.error("❌ Webhook message failed (tenant=%s): %s",
                         tenant_id, type(e).__name__)


def _contact_name(contacts, from_number):
    """The sender's profile name: the contact whose wa_id matches, else the
    first contact (the previous behaviour), else "Student"."""
    chosen = next((c for c in contacts if c.get("wa_id") == from_number),
                  contacts[0] if contacts else None)
    profile = chosen.get("profile") if chosen else None
    name = profile.get("name") if isinstance(profile, dict) else None
    return name or "Student"


def _message_text(message, msg_type):
    """The text the bot routes on, or None for an unsupported type. Pure: no
    side effects, so an unsupported message is never claimed or recorded."""
    if msg_type == "text":
        return message["text"]["body"].strip()
    if msg_type == "interactive":
        itype = message["interactive"]["type"]
        if itype == "button_reply":
            return message["interactive"]["button_reply"]["id"]
        if itype == "list_reply":
            return message["interactive"]["list_reply"]["id"]
        return f"[interactive_{itype}]"
    if msg_type == "button":
        return message["button"]["text"]
    return None


def _claim_inbound(from_number, msg_text, msg_type, wamid, tenant_id):
    """Record the inbound message SYNCHRONOUSLY, before any side effect.

    Phase RC2.5.19-D. This row used to be written by a daemon thread started
    part-way through processing, so a duplicate delivery arriving before that
    thread committed passed the dedup SELECT and was processed twice (a second
    AI reply, duplicate lead events and Sheets rows). The row is now the
    claim: the partial unique index uq_conv_msg_incoming_wa_message_id makes a
    second insert of the same inbound wamid fail, and the loser stops.

    Returns False for a duplicate. Any other failure propagates, so a message
    that cannot be claimed is not processed.
    """
    from datetime import datetime
    from sqlalchemy.exc import IntegrityError
    from app.services.log_service import resolve_tenant_id, _MAX_TEXT

    def _already_claimed():
        return bool(wamid) and ConversationMessage.query.filter_by(
            wa_message_id=wamid, direction="incoming").first() is not None

    if _already_claimed():
        return False
    db.session.add(ConversationMessage(
        phone=from_number,
        direction="incoming",
        message=(msg_text or "")[:_MAX_TEXT],
        message_type=msg_type,
        source="user",
        # "" would collide under the unique index; no id means no claim key.
        wa_message_id=wamid or None,
        created_at=datetime.utcnow(),
        tenant_id=resolve_tenant_id(tenant_id),
    ))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        if _already_claimed():
            return False
        raise
    return True


def _reset_request_state_cache():
    """The flask.g conversation-state cache (Phase 1.5.5E) was built for one
    message per request. Clear it between messages so a second message from
    the same number is not treated as a brand-new lead again."""
    from flask import g
    from app.state import _G_CACHE_ATTR
    g.pop(_G_CACHE_ATTR, None)


def _process_message(message, tenant_id, contacts):
    from app import perf
    _reset_request_state_cache()

    from_number  = message.get("from", "")
    msg_type     = message.get("type", "")
    wamid        = message.get("id", "") or ""
    contact_name = _contact_name(contacts, from_number)
    masked       = _mask(from_number)

    msg_text = _message_text(message, msg_type)
    if msg_text is None:
        # Unsupported type — ignore silently (unchanged).
        return

    # Phase RC2.5.19-D: claim first. Nothing below runs for a duplicate.
    if not _claim_inbound(from_number, msg_text, msg_type, wamid, tenant_id):
        logger.info("♻️ Webhook deduplicated (tenant=%s)", tenant_id)
        return

    # Truncated: full message bodies are lead PII and stay out of logs
    # (same rationale as the Sprint 1 [DIAG] removal). Numbers are masked.
    logger.info(f"📱 {contact_name} ({masked}) tenant={tenant_id}: {msg_text[:40]!r}...")

    # Phase 11-D1 Task D & Phase 11-D2A: Opt-Out & Opt-In Infrastructure
    low_text = msg_text.lower()
    if low_text in {"stop", "unsubscribe", "cancel"}:
        state = ConversationState.query.filter_by(phone=from_number, tenant_id=tenant_id).first()
        if state:
            state.is_opted_out = True
            db.session.commit()
            logger.warning(f"🚫 Opt-out triggered for {masked}")
            # We can optionally send an opt-out confirmation here, but we just halt workflows
            return
    elif low_text in {"start", "resume", "unstop"}:
        state = ConversationState.query.filter_by(phone=from_number, tenant_id=tenant_id).first()
        if state and getattr(state, 'is_opted_out', False):
            state.is_opted_out = False
            db.session.commit()
            logger.info(f"✅ Opt-in recovery triggered for {masked}")
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

    # The inbound ConversationMessage row is written by _claim_inbound() above.

    # ── CRM save (background) ──
    threading.Thread(
        target=save_lead_to_sheets,
        args=(from_number, contact_name, msg_text, is_new_lead, tenant_id),
    ).start()

    # Phase 11-D3B2: Deliver Pending Messages (Interceptor Fallback)
    from app.models import PendingMessage
    from app.services.whatsapp_service import (
        send_text, is_transport_failure, is_ambiguous)
    pending_msgs = PendingMessage.query.filter_by(phone=from_number, tenant_id=tenant_id).order_by(PendingMessage.created_at.asc()).all()
    if pending_msgs:
        logger.info(f"📦 Delivering {len(pending_msgs)} pending messages to {masked}")
        for pm in pending_msgs:
            r = send_text(from_number, pm.text, tenant_id=tenant_id)
            # Phase RC2.5.18-A-FIX1: a queued row is removed once its send
            # has an outcome, EXCEPT when the send provably never left.
            #
            #   success / Meta rejection -> delete (unchanged behaviour)
            #   AMBIGUOUS transport      -> delete. The text may already be
            #                               on the customer's phone; keeping
            #                               the row would re-send it on the
            #                               next inbound message.
            #   NOT_SENT transport       -> KEEP, and stop. Nothing reached
            #                               Meta, so it is retried on the
            #                               contact's next message.
            #
            # Stopping at the first NOT_SENT does two things. It preserves
            # created_at delivery order (ADR-024): sending message 2 while
            # message 1 waits would deliver them out of order next time.
            # And the network is down, so every further attempt would only
            # wait out another connect timeout on this request.
            #
            # Before RC2.5.18-A the send RAISED on a transport error, the
            # handler's outer except swallowed it, and the uncommitted
            # deletes rolled back -- keeping every row, including ones
            # already delivered earlier in the loop (re-sent next time).
            if is_transport_failure(r) and not is_ambiguous(r):
                logger.warning("📦 Pending delivery not sent (%s) — %d "
                               "message(s) kept for the next inbound",
                               r.error_name,
                               len(pending_msgs) - pending_msgs.index(pm))
                break
            db.session.delete(pm)
        db.session.commit()

        # If the user just replied "Yes" to our re-engagement template,
        # suppress the AI to avoid confusing double-replies
        if msg_text.lower().strip() in {"yes", "y", "ok", "okay"}:
            return

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
