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

Actions (Phase 8.2E.6A): IMPERSONATION_START, IMPERSONATION_END — a
SUPER_ADMIN entering or leaving a tenant context. Recorded so that a platform
operator acting inside a customer tenant is never indistinguishable from that
tenant's own staff (ADR-023 D3).

Actions (Phase 10.2A): LEAD_* — mutations to customer records. Until this
phase the audit log covered authentication and broadcasts but no CRM data
change, so "who reassigned this lead / changed this score" was unanswerable
from a tamper-evident source. LEAD_REASSIGNED was written to lead_event, but
that is the lead's own timeline — operator-visible business data, not an
append-only security record.

IMPORTANT for callers: log_audit() COMMITS the session. Call it only after the
business transaction has itself committed, never between a mutation and its
commit, or the audit write will commit that mutation early.
"""
import json
import logging

logger = logging.getLogger(__name__)

VALID_ACTIONS = {
    "LOGIN_SUCCESS", "LOGIN_FAILURE", "ROLE_CHANGE",
    "BROADCAST_SEND", "DATA_EXPORT",
    # Phase 8.2E.6A (ADR-023 D3): platform-operator impersonation boundaries.
    "IMPERSONATION_START", "IMPERSONATION_END",
    # Phase 10.2A: lead record mutations.
    "LEAD_CREATE", "LEAD_UPDATE", "LEAD_ASSIGN",
    "LEAD_STATUS_CHANGE", "LEAD_SCORE_CHANGE",
    "LEAD_ADMISSION", "LEAD_MESSAGE_SENT",
    # Phase 10.3: bulk CSV import. DATA_EXPORT (reserved since Sprint 3) is
    # now actually used by the lead export route.
    "LEAD_IMPORT",
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
