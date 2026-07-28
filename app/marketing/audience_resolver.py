"""
Phase 8.2E.9-A — Audience resolver (ADR-025 D3/D4). UNWIRED.

Resolves a named audience segment to the concrete contacts a campaign would
address. This module RESOLVES ONLY — it performs no writes, opens no
transaction, and never commits. Materialising `CampaignRecipient` rows is
Phase 8.2E.9-C's job, and the launch-time refusal of an empty audience is
8.2E.9-D's.

Layering:

    CampaignService (8.2E.9-C)  →  audience_resolver (here)
                                     →  ConversationState  (tenant-scoped read)
                                     →  segment_source     (classification only)

Tenant safety (ADR-025 D3, ADR-021): `tenant_id` is a REQUIRED first argument
and appears in the WHERE clause of the resolver's own query. A falsy tenant_id
raises rather than resolving against an unfiltered set.

The resolver deliberately does NOT trust the tenant scoping of its segment
source. `_calculate_audiences()` scopes through `tenant_filter()`, whose
SUPER_ADMIN branch can return rows for every tenant (ADR-025 P3). The segment
source is therefore consumed for CLASSIFICATION ONLY — "which phones fall in
this segment" — and the resolver's own tenant-scoped query is what decides
which contacts may appear in the result. Set intersection is the containment:
a phone the segment source should never have returned cannot survive it,
because it is not in this tenant's contact set.

Known residual (Phase 8.2E.9-A audit): `ConversationState` is unique on
(phone, tenant_id), so one phone may exist in two tenants. If the segment
source is ever invoked in an unscoped context AND the same phone exists in
another tenant, the *classification* could be decided by the other tenant's
contact even though the returned row is correctly this tenant's. The contact
list cannot leak; the segment decision could. Both preconditions are currently
false — Campaign V2 refuses a non-impersonating SUPER_ADMIN before reaching
this module, and production holds zero cross-tenant phone collisions. Recorded
rather than mitigated; the fix if it ever becomes reachable is to extract the
segment calculation into a shared tenant-explicit service, not to reimplement
it here (which would let segment definitions drift from the live V1 engine).
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Segment vocabulary ────────────────────────────────────────────────────────
# Mirrored from _calculate_audiences() in app/routes/admin.py rather than
# imported: that module pulls the whole app graph at import time and cannot be
# loaded in a test process. A drift-guard test asserts these stay equal to the
# keys the live function actually produces.
SEGMENTS = (
    "HOT Leads",
    "WARM Leads",
    "Demo Requested",
    "Fees Requested",
    "Placement Interested",
    "Needs Reply",
    "Critical Leads",
    "All Leads",
)


class AudienceResolutionError(ValueError):
    """Raised when an audience cannot be resolved from the given inputs.

    Deliberately a local error type rather than CampaignValidationError: this
    module does not import campaign_service, matching the no-cross-import rule
    the repository and service layers already follow. The caller translates.
    """


def list_segments() -> tuple:
    """Return the selectable segment names, in display order."""
    return SEGMENTS


def _default_segment_source(tenant_id):
    """Resolve segments via the live engine used by legacy V1.

    Imported lazily and only when no `segment_source` was injected:
    `app.routes.admin` imports `app.config` at module load, which raises
    without a DATABASE_URL, so a module-level import here would make this
    module unloadable in any test process.
    """
    from app.routes.admin import _calculate_audiences
    return _calculate_audiences(tenant_id)


def _guard_and_classify(tenant_id, segment, segment_source):
    """Shared entry checks for resolve() and preview(): tenant + segment
    validation, then classification via the (possibly injected) segment
    source. Returns the set of classified phones — empty when nothing was
    classified, which callers short-circuit on before touching the DB.
    """
    if not tenant_id:
        raise AudienceResolutionError("tenant_id is required")
    if segment not in SEGMENTS:
        raise AudienceResolutionError(
            f"unknown audience segment {segment!r}; "
            f"expected one of: {', '.join(SEGMENTS)}"
        )

    if segment_source is None:
        segment_source = _default_segment_source

    audiences = segment_source(tenant_id) or {}
    return set(audiences.get(segment) or ())


def _window_open_at(last_msg, now) -> bool:
    """Pure 24-hour window check against a stored `last_msg` ISO string.

    Deliberately duplicates the check in campaign_worker._window_open()
    rather than importing it (ADR-024 R1 precedent: dispatch and audience
    stay decoupled). Both copies exist because they answer different
    questions at different times — dispatch checks "is it open right now, for
    this one send"; preview checks "how many of these N contacts are open,
    for a report" — but they must agree on what "open" means, so this is the
    single formula preview uses; keep it identical to campaign_worker's.
    """
    if not last_msg:
        return False
    try:
        last_dt = datetime.fromisoformat(last_msg)
    except (ValueError, TypeError):
        return False
    return (now - last_dt).total_seconds() < 86400


def resolve(tenant_id, segment, *, session=None, state_model=None,
            segment_source=None) -> list:
    """Resolve `segment` to this tenant's contacts. Read-only.

    Returns a list of ``{"phone": ..., "name": ...}`` dicts — the shape
    `CampaignRepository.add_recipients()` already accepts — ordered by phone so
    a materialised snapshot is reproducible across runs.

    Applies, in order:
      1. tenant_id required (ADR-021)
      2. segment must be known — an unrecognised name RAISES rather than
         resolving to empty, because D2 refuses an empty audience at launch and
         a typo must not be indistinguishable from a genuinely empty segment
      3. classification from `segment_source` (trusted for membership only)
      4. this tenant's contacts, opted-out excluded (D4)
      5. intersection — the tenant-scoped row is the authority for `name`

    `session`, `state_model` and `segment_source` are injectable for testing,
    matching CampaignRepository's model-injection convention; in production
    they resolve lazily to db.session, app.models.ConversationState and
    _calculate_audiences().
    """
    segment_phones = _guard_and_classify(tenant_id, segment, segment_source)
    if not segment_phones:
        # Nothing classified into this segment — no need to touch the DB.
        return []

    if session is None:
        from app.extensions import db      # lazy: no import-time app dependency
        session = db.session
    if state_model is None:
        from app.models import ConversationState as state_model

    # The resolver's OWN tenant filter (D3) — never tenant_filter(), never the
    # segment source's scoping. `.isnot(True)` and not `== False` (D4):
    # is_opted_out is nullable, and in SQL `NULL = False` is NULL, so equality
    # filtering silently drops every never-set row.
    rows = (
        session.query(state_model.phone, state_model.name)
        .filter(
            state_model.tenant_id == tenant_id,
            state_model.is_opted_out.isnot(True),
        )
        .order_by(state_model.phone)
        .all()
    )

    resolved = [
        {"phone": phone, "name": name}
        for phone, name in rows
        if phone in segment_phones
    ]

    logger.info(
        "Audience resolved — tenant=%s segment=%r classified=%d resolved=%d",
        tenant_id, segment, len(segment_phones), len(resolved),
    )
    return resolved


def preview(tenant_id, segment, *, session=None, state_model=None,
            segment_source=None, now=None) -> dict:
    """ADR-025 D6.1: reachability disclosure for a segment. Read-only.

    Returns::

        {
          "segment": str,
          "total_audience": int,   # this tenant's contacts classified into
                                    # the segment, BEFORE opt-out exclusion
          "opted_out_excluded": int,
          "reachable_now": int,    # inside the 24h window -> plain text
          "template_required": int # outside the window -> needs an approved
                                    # template (ADR-024 D2); this is the count
                                    # a caller combines with template readiness
                                    # (D7) to state the expected failure impact
        }

    Deliberately returns counts only, not the underlying contact list — the
    preview is a sizing and risk disclosure, not an audience export. The
    tenant filter and opt-out semantics are identical to resolve() (D3/D4);
    this function does not call resolve() because it needs is_opted_out and
    last_msg per row to classify into three buckets, where resolve() needs
    only phone/name for the two-state (excluded/kept) result.

    `now` is injectable so the 24h boundary is deterministic under test;
    production resolves it to datetime.utcnow() at call time.
    """
    segment_phones = _guard_and_classify(tenant_id, segment, segment_source)
    if not segment_phones:
        return {
            "segment": segment,
            "total_audience": 0,
            "opted_out_excluded": 0,
            "reachable_now": 0,
            "template_required": 0,
        }

    if session is None:
        from app.extensions import db
        session = db.session
    if state_model is None:
        from app.models import ConversationState as state_model
    if now is None:
        now = datetime.utcnow()

    rows = (
        session.query(state_model.phone, state_model.is_opted_out, state_model.last_msg)
        .filter(state_model.tenant_id == tenant_id)
        .all()
    )

    total_audience = 0
    opted_out_excluded = 0
    reachable_now = 0
    template_required = 0

    for phone, is_opted_out, last_msg in rows:
        if phone not in segment_phones:
            continue
        total_audience += 1
        # `is True`, not the falsy check used elsewhere — is_opted_out is one
        # of True / False / None here, and only True counts as excluded (D4).
        if is_opted_out is True:
            opted_out_excluded += 1
            continue
        if _window_open_at(last_msg, now):
            reachable_now += 1
        else:
            template_required += 1

    logger.info(
        "Audience preview — tenant=%s segment=%r total=%d opted_out=%d "
        "reachable_now=%d template_required=%d",
        tenant_id, segment, total_audience, opted_out_excluded,
        reachable_now, template_required,
    )
    return {
        "segment": segment,
        "total_audience": total_audience,
        "opted_out_excluded": opted_out_excluded,
        "reachable_now": reachable_now,
        "template_required": template_required,
    }
