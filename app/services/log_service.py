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

Tenant resolution:
  resolve_tenant_id()                   → the ONLY tenant resolver. Explicit
                                          tenant_id, else PRIMARY_TENANT_ID,
                                          else None. See its docstring.

Phase RC2.4.3 removed _get_default_tenant_id() (Tenant.query.first()), the
Phase 12-C1 emergency hotfix that resolved an ARBITRARY tenant. H4-c had
already unwired it from resolve_tenant_id(); by RC2.4.3 it had 1 definition,
0 callers and 0 imports repository-wide, so deleting it changed no runtime
path. It is gone rather than merely uncalled so it cannot be rewired.
"""
import logging
from datetime import datetime

_MAX_TEXT = 5000   # Prevent oversized DB rows and abuse payloads


# ── Phase 0 Sprint 2: Explicit Tenant Resolution ───────────────────────────

def resolve_tenant_id(tenant_id: str = None) -> str:
    """
    Resolve the tenant context for a write path.

    Order:
      1. Explicit tenant_id from the caller — always wins.
      2. PRIMARY_TENANT_ID from app config (set in production). Logged at
         ERROR: the caller should have passed tenant_id explicitly, and this
         branch attributes data to a tenant nobody named.
      3. None. There is no third guess.

    PHASE H4-c — WHY THERE IS NO LONGER A THIRD LEG
    -----------------------------------------------
    This used to fall through to _get_default_tenant_id(), i.e.
    Tenant.query.first() — an ARBITRARY tenant. That is the exact mechanism
    behind TD-P0-1 / the Phase 17.1-C mis-filing incident, and the comment
    claiming it "never executes in production" was a statement about
    configuration, not a guarantee: it fires whenever PRIMARY_TENANT_ID is
    unset, which no code path enforces.

    Returning None instead means a caller that supplies no tenant gets no
    tenant. For the three log writers this surfaces as an IntegrityError on a
    NOT NULL column, caught by their own try/except and logged — one lost log
    line instead of a row silently attributed to another customer. In a
    multi-tenant CRM that is the correct trade; a lost audit line is
    recoverable, a cross-tenant write is not.

    Behaviour in production is UNCHANGED: PRIMARY_TENANT_ID is configured, so
    leg 2 has always answered before leg 3 could. This removes a trap, not a
    working path.

    NOTE: callers are not limited to the log writers. bot/router.smart_reply()
    also resolves through here and uses the result as conversation state, so
    the None case must stay loud rather than silent.
    """
    if tenant_id:
        return tenant_id
    try:
        from flask import current_app
        primary = (current_app.config.get("PRIMARY_TENANT_ID") or "").strip()
        if primary:
            logging.error(
                "[tenant] implicit resolution → PRIMARY_TENANT_ID "
                "(caller passed tenant_id=None; pass it explicitly)"
            )
            return primary
    except Exception:
        logging.exception("[tenant] resolve_tenant_id: config lookup failed")
    logging.error(
        "[tenant] UNRESOLVED: no explicit tenant_id and no PRIMARY_TENANT_ID. "
        "Refusing to guess a tenant; the caller must pass one."
    )
    return None


# ── Phase 4D: Raw technical event log ──────────────────────────────────────

def log_message(
    phone: str,
    direction: str,
    message_type: str,
    message_text: str,
    meta_json: str = None,
    tenant_id: str = None,
) -> None:
    """
    Append one message event to the message_log table (raw technical log).

    Args:
        phone:        WhatsApp phone number
        direction:    "inbound" or "outbound"
        message_type: "user", "ai", "followup", or "manual"
        message_text: raw message body (truncated to 5000 chars)
        meta_json:    optional JSON string for extra metadata
        tenant_id:    Tenant context (defaults to default tenant if None)
    """
    try:
        from app.models import MessageLog
        from app.extensions import db

        # Phase 0 Sprint 2: explicit tenant resolution (config-first)
        tenant_id = resolve_tenant_id(tenant_id)

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
        # Phase H4-d: roll back BEFORE logging.
        #
        # A failed flush leaves the Session inactive (SQLAlchemy 2.x), so every
        # later query in the SAME app context raises PendingRollbackError.
        # Flask-SQLAlchemy scopes one session per app context, so a daemon
        # thread is self-contained — but an in-request call shares the route's
        # session, and four lead-assignment routes do further DB work after
        # logging. Without this, a lost log line took the rest of the request
        # with it.
        #
        # Not only the unresolved-tenant case: any exception here poisons the
        # session, including a DataError from an untruncated VARCHAR column or
        # a transient DB fault.
        #
        # The nested try mirrors save_conversation_message(): a failing
        # rollback must not mask the original error.
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:
            logging.exception("[log_service] Failed to rollback MessageLog transaction")
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
    tenant_id: str = None,
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
        tenant_id:     Tenant context (defaults to default tenant if None)
    """
    try:
        from app.models import ConversationMessage
        from app.extensions import db

        # Phase 0 Sprint 2: explicit tenant resolution (config-first)
        tenant_id = resolve_tenant_id(tenant_id)

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
    tenant_id: str = None,
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
        tenant_id:  Tenant context (defaults to default tenant if None)
    """
    try:
        from app.models import LeadEvent
        from app.extensions import db

        # Phase 0 Sprint 2: explicit tenant resolution (config-first)
        tenant_id = resolve_tenant_id(tenant_id)

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
        # Phase H4-d: roll back BEFORE logging — see log_message() for the
        # full rationale. Same shape as save_conversation_message(), including
        # the nested try so a failing rollback cannot mask the original error.
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:
            logging.exception("[log_service] Failed to rollback LeadEvent transaction")
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

