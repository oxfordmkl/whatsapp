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


#: Phase RC2.5.17 Gate A.1 — THE TRUST BOUNDARY, stated once.
#:
#: This application terminates behind Railway's edge proxy and is not reachable
#: except through it (no public port, no direct container address). The edge is
#: therefore the ONLY hop that may be trusted to describe the client, and this
#: is the platform behaviour the resolution below depends on:
#:
#:   * Railway strips client-supplied X-Forwarded-For at the edge, so a client
#:     cannot prepend a value; the FIRST XFF entry is the real connecting IP.
#:   * Railway sets X-Real-IP as a single source of truth for that same
#:     address, and overwrites it from Cf-Connecting-IP when (and only when)
#:     the request genuinely arrives via Cloudflare.
#:   (Railway staff statement, Railway Central Station, "Security-Critical
#:    Questions on Edge Proxy Header Handling and Hop Count".)
#:
#: CORROBORATED, NOT ASSUMED. Community answers on that same thread claim the
#: opposite -- that Railway appends and only the RIGHTMOST value is safe, with
#: internal hops in 100.0.0.0/8. Production data refutes that reading: of the
#: 70 distinct addresses this application has recorded across 5,593 audit rows,
#: ZERO are private, loopback or CGNAT and all 70 are public IPv4. Had the code
#: been reading an internal hop, those rows would be full of 100.x addresses.
#:
#: WHY THIS IS STILL WORTH HARDENING. The old one-liner was CORRECT on this
#: platform and undefended everywhere else: it trusted a header unconditionally
#: and returned whatever string it found, so the moment the app runs behind a
#: different proxy, or none, an attacker chooses its own identity for every
#: IP-keyed control. Nothing in the code recorded that dependency either.
#:
#: NOT SOLVED BY TAKING THE RIGHTMOST ENTRY. On this platform the rightmost
#: entry is an INTERNAL hop, so preferring it would collapse every client onto
#: one address and turn per-IP limits into a global lock. The hop count is not
#: contractually documented and is explicitly not assumed here.
#:
#: NOT FILTERED BY RANGE. An earlier draft carried a private-prefix list to
#: reject RFC1918/loopback candidates; it is deliberately absent. Rejecting
#: private addresses would break every non-Railway deployment (local dev, a
#: reverse proxy on the same host) while adding nothing here, because the edge
#: never presents one -- all 70 addresses this application has recorded are
#: public. Validity, not range, is what this boundary checks.


def _valid_ip(value: str) -> str:
    """Return `value` if it parses as an IP address, else "".

    The previous implementation returned the header verbatim. Unvalidated, that
    string becomes a rate-limit bucket key AND is persisted to
    audit_log.ip_address (a String(45) column), so anything able to influence
    the header could write arbitrary junk into the security record or mint
    unlimited distinct limiter buckets. Parsing is cheap and removes the class.

    RC2.5.17 Gate B: returns the CANONICAL form, not the spelling supplied.
    The first version validated and then returned the raw string, so
    2001:db8::1 and 2001:0db8:0000:0000:0000:0000:0000:0001 -- the same
    address -- produced two different strings. As an audit value that is
    merely untidy; as a RATE-LIMIT BUCKET KEY it is a bypass, because an
    IPv6 client can re-spell its own address and mint a fresh budget each
    time. Harmless so far only because all 70 addresses this application has
    recorded are IPv4, which has one spelling.
    """
    import ipaddress
    s = (value or "").strip()
    if not s or len(s) > 45:
        return ""
    try:
        parsed = ipaddress.ip_address(s)
    except ValueError:
        return ""
    return str(parsed)


def request_ip() -> str:
    """The client IP for the current request, per the trust boundary above.

    Resolution order, most authoritative first:
      1. X-Real-IP      -- Railway's designated single source of truth
      2. X-Forwarded-For, FIRST entry -- the real connecting IP on this edge
      3. request.remote_addr -- the immediate peer, when no proxy header exists

    Every candidate must parse as an IP address or it is skipped, so a
    malformed or hostile header degrades to the next source rather than
    propagating. Returns "" when nothing usable remains; callers must treat ""
    as "unknown", never as an identity to group by.

    This is the ONE implementation. app.routes.public.get_client_ip delegates
    here so the platform assumption lives in a single place and cannot drift
    between the audit log and the rate limiter.
    """
    try:
        from flask import request

        real = _valid_ip(request.headers.get("X-Real-IP"))
        if real:
            return real

        fwd = request.headers.get("X-Forwarded-For") or ""
        first = _valid_ip(fwd.split(",")[0])
        if first:
            return first

        return _valid_ip(request.remote_addr)
    except Exception:                                           # noqa: BLE001
        return ""
