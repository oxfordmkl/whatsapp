"""audit_service.py — Phase 0 Sprint 3

Sovereign append-only security audit log (Constitution I.7).

Contract:
  - log_audit() is the ONLY write API. There is no update or delete API,
    by design — the table is append-only. Code review enforces this.
  - Never raises: an audit failure must not break the business action it
    records (but it is logged loudly, because silent audit loss is itself
    an incident).
  - Never log secrets: no passwords, tokens, or message bodies in `detail`.

Actions (Sprint 3): LOGIN_SUCCESS, LOGIN_FAILURE, ROLE_CHANGE,
BROADCAST_SEND, DATA_EXPORT (reserved — no export routes exist yet).
"""
import json
import logging

logger = logging.getLogger(__name__)

VALID_ACTIONS = {
    "LOGIN_SUCCESS", "LOGIN_FAILURE", "ROLE_CHANGE",
    "BROADCAST_SEND", "DATA_EXPORT",
}


def log_audit(action: str, actor: str = None, tenant_id: str = None,
              target: str = None, detail: dict = None, ip: str = None) -> None:
    """Append one security event to audit_log. Never raises."""
    try:
        from app.models import AuditLog
        from app.extensions import db

        if action not in VALID_ACTIONS:
            logger.error("[audit] rejected unknown action %r (target=%r)", action, target)
            return

        entry = AuditLog(
            tenant_id=tenant_id,
            actor=(actor or None),
            action=action,
            target=(target or None),
            detail=json.dumps(detail, default=str) if detail else None,
            ip_address=(ip or None),
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:
            pass
        logger.exception("[audit] FAILED to record %s (actor=%s target=%s)",
                         action, actor, target)


def request_ip() -> str:
    """Best-effort client IP for the current request (proxy-aware, first hop)."""
    try:
        from flask import request
        fwd = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return fwd or request.remote_addr or ""
    except Exception:
        return ""
