"""Phase RC2.5.5b-1: the ONLY runtime resolver for tenant-owned payment URLs.

WHY THIS MODULE EXISTS
-----------------------
The deterministic bot flow issues payment links from two module-level
constants in app/bot/constants.py -- COURSE_PAYMENT_LINKS (keyed by course
name) and OFFER_MENU (keyed by menu digit). Both hold the same four live
Oxford Razorpay URLs. Four separate code paths read them, and none receives a
tenant_id. A second tenant's customer who selects a course and taps ENROLL is
therefore handed Oxford's payment link and pays Oxford. That is not a display
bug; it is money arriving in the wrong account.

This module is the tenant-owned replacement. It resolves a payment URL from
TenantKnowledge rows the tenant admin authored and owns.

FAIL-CLOSED, DELIBERATELY THE OPPOSITE OF knowledge_service
------------------------------------------------------------
knowledge_service.fetch_knowledge() fails OPEN: on a DB error it returns no
knowledge and the AI answers with less context. Degraded content is
acceptable.

This module fails CLOSED. Every failure -- no tenant, no row, inactive row,
blank URL, malformed URL, ambiguous match, DB outage -- returns NO LINK. The
caller then falls back to the human counselor handoff that already exists for
courses without a link. A wrong link takes a customer's money to the wrong
account; showing a phone number instead does not. There is no error condition
under which guessing is better than declining.

There is NO fallback of any kind: not to COURSE_PAYMENT_LINKS, not to
OFFER_MENU, not to a platform default, not to the primary tenant. A tenant
that has not authored a payment URL has no payment URL. That is the entire
point of the phase, and it is why this module imports nothing from
app.bot.constants.

WHY (tenant_id, code) AND NOT TITLE
------------------------------------
`title` is free text a tenant admin may reword at any time; matching on it
would break the moment someone fixes a typo. `commercial.code` (RC2.5.5b-1)
is a stable key, uppercased and charset-restricted on write, that survives a
rename.

legacy_payment_url IS NEVER READ HERE. It is an archival record of what the
hardcoded constant held, quarantined from both the prompt (knowledge_service)
and the write path (knowledge_admin_service). Promoting it to a runtime
source would resurrect a value deliberately excluded from customer-facing
output, and would make Oxford's payments depend on a key no other tenant can
populate -- a global fallback wearing a per-tenant costume.

PHASE BOUNDARY: RC2.5.5b-1 builds this resolver and leaves it UNWIRED. The
four emission paths still read the constants, so customer-facing behaviour is
byte-for-byte unchanged. Flipping them is RC2.5.5b-2, and only after Oxford's
rows carry the codes and URLs this module needs.
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

# Same charset the admin write path enforces (knowledge_admin_service).
# Repeated rather than imported: this is a READ-side guard, and it must keep
# rejecting junk even if the write side is ever loosened.
_CODE_RE = re.compile(r"^[A-Z0-9_-]+$")
_MAX_CODE_LEN = 32

# Only http(s). A payment "link" that is javascript:, data: or file: is not a
# payment link; if such a value ever reached a row, refusing is correct.
_URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)
_MAX_URL_LEN = 500

# Never a runtime source. Named here so the exclusion is explicit and
# testable rather than merely absent.
_NEVER_READ_ATTR_KEYS = frozenset({"legacy_payment_url"})

KIND = "course"


def normalise_code(code):
    """Canonical form of a course code, or None if it is not a usable key.

    Uppercased and stripped so "pgdca", " PGDCA " and "PGDCA" are one course.
    Case drift must never yield two rows that both look like a match.
    """
    if code is None:
        return None
    text = str(code).strip().upper()
    if not text or len(text) > _MAX_CODE_LEN or not _CODE_RE.match(text):
        return None
    return text


def _row_code(row):
    """The stable code stored on a row, or None.

    Never raises: attributes is free-form JSON text written by earlier phases,
    and a malformed blob must make the row unmatchable, not crash the bot.
    """
    try:
        attrs = json.loads(row.attributes or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(attrs, dict):
        return None
    commercial = attrs.get("commercial")
    if not isinstance(commercial, dict):
        return None
    return normalise_code(commercial.get("code"))


def _row_payment_url(row):
    """The ACTIVE payment URL on a row, or None.

    Reads commercial.payment_url only. legacy_payment_url is never consulted,
    and no other key is treated as a payment URL.
    """
    try:
        attrs = json.loads(row.attributes or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(attrs, dict):
        return None
    commercial = attrs.get("commercial")
    if not isinstance(commercial, dict):
        return None

    url = commercial.get("payment_url")
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or len(url) > _MAX_URL_LEN or not _URL_RE.match(url):
        return None
    return url


def resolve_payment_url(tenant_id, code):
    """Return this tenant's payment URL for `code`, or None.

    None means "no link -- hand off to a counselor". It NEVER means "use a
    default". Every rejection below is logged at a level matching how much it
    suggests a real problem, so a tenant whose links silently stop working is
    diagnosable without turning the failure into a customer-visible error.
    """
    if not tenant_id:
        # A falsy tenant is a caller bug or an unresolved conversation. It is
        # never "any tenant" and never the primary tenant.
        logger.error("[payment] refused: no tenant_id resolved")
        return None

    key = normalise_code(code)
    if key is None:
        logger.info("[payment] no link: unusable course code %r", code)
        return None

    try:
        from app.models import TenantKnowledge
        rows = TenantKnowledge.query.filter(
            TenantKnowledge.tenant_id == tenant_id,
            TenantKnowledge.kind == KIND,
            TenantKnowledge.is_active.is_(True),
        ).all()
    except Exception:
        # Fail CLOSED. knowledge_service degrades to less context here; this
        # path would degrade to the WRONG ACCOUNT, so it declines instead.
        logger.exception(
            "[payment] lookup failed for tenant=%s code=%s -- no link issued",
            tenant_id, key)
        return None

    # Defence in depth, mirroring knowledge_service and knowledge_admin_service:
    # prove ownership of what we are about to act on, rather than trusting the
    # filter above to have been applied.
    leaked = [r for r in rows if r.tenant_id != tenant_id]
    if leaked:
        logger.error(
            "[payment] ISOLATION VIOLATION: query for tenant=%s returned %d "
            "row(s) owned by another tenant -- no link issued",
            tenant_id, len(leaked))
        return None

    matches = [r for r in rows if _row_code(r) == key]
    if not matches:
        logger.info("[payment] no link: tenant=%s has no active course %s",
                    tenant_id, key)
        return None

    if len(matches) > 1:
        # Ambiguity is refused, not broken by ordering. Picking "the first"
        # would make which account gets paid depend on row ids.
        logger.error(
            "[payment] AMBIGUOUS: tenant=%s has %d active rows for code=%s "
            "(ids=%s) -- refusing to guess, no link issued",
            tenant_id, len(matches), key, [r.id for r in matches])
        return None

    url = _row_payment_url(matches[0])
    if url is None:
        # The row exists but its owner has not authored a link. This is the
        # normal, expected state for the nine Oxford courses that have never
        # had one -- not an error.
        logger.info("[payment] no link configured: tenant=%s code=%s",
                    tenant_id, key)
        return None

    return url


def has_payment_url(tenant_id, code):
    """Convenience predicate for callers that only need the yes/no."""
    return resolve_payment_url(tenant_id, code) is not None
