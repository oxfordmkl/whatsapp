"""
log_service.py — Phase 4D / Phase 5A
Lightweight message logging helper.
Safe to call from webhook, scheduler, and CRM routes.
Never raises — all errors are logged internally.

Architecture:
  log_message()                         → writes to MessageLog (raw technical log)
  save_conversation_message()           → writes to ConversationMessage (CRM
                                          timeline); requires active app context
  save_conversation_message_in_thread() → thread-safe wrapper that opens its own
                                          Flask app context; pass app reference
                                          captured via current_app._get_current_object()
  log_message_in_thread()               → thread-safe wrapper for log_message()

Phase 12-C1 Emergency Hotfix:
  _get_default_tenant_id()              → resolves the active tenant_id from the
                                          Tenant table dynamically. Required after
                                          Phase 12-B added nullable=False tenant_id
                                          FK columns to all INSERT targets.
"""
import logging
from datetime import datetime

_MAX_TEXT = 5000   # Prevent oversized DB rows and abuse payloads


# ── Phase 12-C1: Tenant Resolution Helper ──────────────────────────────────

def _get_default_tenant_id() -> str:
    """
    Resolve the active tenant_id from the Tenant table.

    Strategy:
      - Query Tenant.query.first() — safe while only one tenant exists.
      - Returns the tenant's primary-key ID string.
      - Returns None on failure so the caller's existing except block handles it.

    This function must ONLY be called from within an active Flask app context.
    It is intentionally not cached at module level so it remains compatible
    with future multi-tenant routing (Phase 12-C Multi-WhatsApp).
    """
    try:
        from app.models import Tenant
        tenant = Tenant.query.first()
        if tenant:
            return tenant.id
        logging.error("[log_service] _get_default_tenant_id: No Tenant rows found in DB.")
        return None
    except Exception:
        logging.exception("[log_service] _get_default_tenant_id: Failed to resolve tenant_id.")
        return None


# ── Phase 4D: Raw technical event log ──────────────────────────────────────

def log_message(
    phone: str,
    direction: str,
    message_type: str,
    message_text: str,
    meta_json: str = None,
) -> None:
    """
    Append one message event to the message_log table (raw technical log).

    Args:
        phone:        WhatsApp phone number
        direction:    "inbound" or "outbound"
        message_type: "user", "ai", "followup", or "manual"
        message_text: raw message body (truncated to 5000 chars)
        meta_json:    optional JSON string for extra metadata
    """
    try:
        from app.models import MessageLog
        from app.extensions import db

        # Phase 12-C1: Resolve tenant_id before INSERT
        tenant_id = _get_default_tenant_id()

        entry = MessageLog(
            phone=phone,
            direction=direction,
            message_type=message_type,
            message_text=(message_text or "")[:_MAX_TEXT],
            meta_json=meta_json,
            created_at=datetime.utcnow(),
            tenant_id=tenant_id,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        logging.exception(
            f"[log_service] Failed to log MessageLog {direction}/{message_type} for {phone}"
        )


def log_message_in_thread(app, **kwargs) -> None:
    """
    Thread-safe wrapper for log_message().
    """
    with app.app_context():
        log_message(**kwargs)


# ── Phase 5A: Structured CRM message persistence ───────────────────────────

def save_conversation_message(
    phone: str,
    direction: str,
    message: str,
    message_type: str = "text",
    source: str = None,
    staff_name: str = None,
    wa_message_id: str = None,
) -> None:
    """
    Append one structured entry to conversation_message (CRM timeline).
    Requires an active Flask app context.
    Call directly from request handlers.
    For daemon threads use save_conversation_message_in_thread() instead.

    Args:
        phone:         WhatsApp phone number
        direction:     "incoming" or "outgoing"
        message:       raw message body (truncated to 5000 chars)
        message_type:  "text" | "interactive" | "button" | "template" | "system"
        source:        "user" | "ai" | "manual" | "followup" | "system"
        staff_name:    staff name for manual sends — audit trail (nullable)
        wa_message_id: WhatsApp message ID for deduplication (nullable)
    """
    try:
        from app.models import ConversationMessage
        from app.extensions import db

        # Phase 12-C1: Resolve tenant_id before INSERT
        tenant_id = _get_default_tenant_id()

        entry = ConversationMessage(
            phone=phone,
            direction=direction,
            message=(message or "")[:_MAX_TEXT],
            message_type=message_type,
            source=source,
            staff_name=staff_name,
            wa_message_id=wa_message_id,
            created_at=datetime.utcnow(),
            tenant_id=tenant_id,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:
            logging.exception("[log_service] Failed to rollback ConversationMessage transaction")
        logging.exception(
            f"[log_service] Failed to save ConversationMessage "
            f"{direction}/{source} for {phone}"
        )


def save_conversation_message_in_thread(app, **kwargs) -> None:
    """
    Thread-safe wrapper for save_conversation_message().
    Opens a dedicated Flask app context so daemon threads never hit
    'working outside application context' errors.

    Usage pattern (inside a request handler, before spawning thread):
        _app = current_app._get_current_object()
        threading.Thread(
            target=save_conversation_message_in_thread,
            kwargs=dict(app=_app, phone=..., direction=..., ...),
            daemon=True,
        ).start()
    """
    with app.app_context():
        save_conversation_message(**kwargs)


# ── Phase 6A: Lead event tracking ──────────────────────────────────────────

def log_lead_event(
    phone: str,
    event_type: str,
    event_data: str = None,
) -> None:
    """
    Append one named business event to the lead_event table.
    Requires an active Flask app context.
    For daemon threads use log_lead_event_in_thread() instead.
    Never raises — all errors are caught and logged internally.

    Args:
        phone:      WhatsApp phone number
        event_type: e.g. "COURSE_VIEWED", "FEES_REQUESTED",
                    "DEMO_REQUESTED", "PLACEMENT_ASKED"
        event_data: optional context string (e.g. course name)
    """
    try:
        from app.models import LeadEvent
        from app.extensions import db

        # Phase 12-C1: Resolve tenant_id before INSERT
        tenant_id = _get_default_tenant_id()

        entry = LeadEvent(
            phone=phone,
            event_type=event_type,
            event_data=event_data,
            created_at=datetime.utcnow(),
            tenant_id=tenant_id,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        logging.exception(
            f"[log_service] Failed to log LeadEvent {event_type} for {phone}"
        )


def log_lead_event_in_thread(app, **kwargs) -> None:
    """
    Thread-safe wrapper for log_lead_event().
    Opens its own Flask app context — safe to call from daemon threads
    that have no active request context.

    Usage (from within a request handler before spawning a thread):
        _app = current_app._get_current_object()
        threading.Thread(
            target=log_lead_event_in_thread,
            kwargs=dict(app=_app, phone=..., event_type=..., event_data=...),
            daemon=True,
        ).start()
    """
    with app.app_context():
        log_lead_event(**kwargs)

