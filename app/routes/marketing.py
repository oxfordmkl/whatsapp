"""
Phase 8.2D — Marketing blueprint: Campaign V2 HTTP surface.

Registered unconditionally; every route is internally gated by
CAMPAIGN_ENGINE_V2. When OFF (production today) all routes return 404.

Layering:
    marketing_bp (here)
      → CampaignService  (app/marketing/campaign_service.py)
        → CampaignRepository (app/persistence/campaign_repository.py)

Auth: check_auth() for authentication; campaign_admin_required for
ADMIN/SUPER_ADMIN role gate on mutation routes. GET routes require authn only.
Tenant: _actor_tenant_id() only; None always refuses (ADR-021).

Phase 8.2D.1: skeleton (blueprint, helpers, no routes).
Phase 8.2D.2: read routes — list, detail, progress.
Phase 8.2E.2: RBAC hardening — campaign_admin_required on all mutation routes.
Phase 8.2E.9-B: audience preview — segments, per-campaign reachability/template
readiness (ADR-025 D6/D7). Read-only; materialises nothing.
"""
import logging
from functools import wraps

from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)

marketing_bp = Blueprint("marketing", __name__, url_prefix="/crm/campaigns/v2")


# ── Feature-flag guard ────────────────────────────────────────────────────────

def _engine_enabled() -> bool:
    """Return True iff CAMPAIGN_ENGINE_V2 is ON. Always re-read, never cached."""
    from app.flags import campaign_engine_v2_enabled
    return campaign_engine_v2_enabled()


def require_campaign_engine(f):
    """Decorator: return 404 when CAMPAIGN_ENGINE_V2 is OFF.

    404 (not 403 or 503) is deliberate — it avoids advertising that a V2
    surface exists. The endpoint is simply not found while the flag is OFF.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _engine_enabled():
            return jsonify({"error": "Not found"}), 404
        return f(*args, **kwargs)
    return wrapper


# ── Tenant safety ─────────────────────────────────────────────────────────────

def _resolve_tenant():
    """Return the acting user's tenant_id or None.

    Callers must refuse to write when None is returned (ADR-021).
    Imports are lazy to avoid a circular dependency at module load time.
    """
    from app.routes.admin import _actor_tenant_id
    return _actor_tenant_id()


def _require_tenant():
    """Return (tenant_id, None) or (None, error_response).

    Usage::
        tenant_id, err = _require_tenant()
        if err:
            return err
    """
    tid = _resolve_tenant()
    if not tid:
        return None, (jsonify({"error": "Tenant context required"}), 403)
    return tid, None


# ── Exception → HTTP mapping ──────────────────────────────────────────────────

def _map_campaign_error(exc):
    """Convert CampaignService exceptions to Flask JSON responses.

    Imports are lazy so this module never pulls in the service at load time.
    Returns a (response, status_code) tuple or re-raises for unexpected types.
    """
    from app.marketing.campaign_service import (
        CampaignEngineDisabled,
        CampaignValidationError,
        CampaignTransitionError,
    )

    if isinstance(exc, CampaignEngineDisabled):
        # Should never reach here (require_campaign_engine fires first), but
        # defence-in-depth: treat as 404 for the same reason as the decorator.
        return jsonify({"error": "Not found"}), 404

    if isinstance(exc, CampaignValidationError):
        return jsonify({
            "error": "Validation failed",
            "detail": exc.result.as_dict(),
        }), 400

    if isinstance(exc, CampaignTransitionError):
        return jsonify({
            "error": "Illegal transition",
            "detail": str(exc),
        }), 409

    raise exc


# ── Auth helpers (thin re-exports, no new logic) ──────────────────────────────

def _check_auth() -> bool:
    """Delegate to admin.check_auth() without re-implementing auth logic."""
    from app.routes.admin import check_auth
    return check_auth()


def _resolve_impersonated_by(actor: dict) -> str | None:
    """Return the actor username when currently operating under impersonation.

    Reads the Flask session to detect the impersonation context. Returns None
    when not impersonating. Isolated into a helper so tests can stub it without
    needing a full Flask session — CampaignService must never call this directly
    (ADR-023 D3: session access belongs in the route layer only).
    """
    try:
        from flask import session
        if session.get("impersonate_tenant_id"):
            return actor.get("username")
    except Exception:
        pass
    return None


def _auth_mode() -> str:
    """Return the resolved AUTH_MODE from the current app config.

    Returns SESSION_ONLY when the config value is absent or not a plain string.
    In production AUTH_MODE is always a string — it is validated by config.py
    and written to app.config by create_app(). A non-string value only occurs
    in test stubs that replace current_app with a MagicMock; treating that as
    SESSION_ONLY keeps those tests' existing assertions intact.
    """
    from flask import current_app
    mode = current_app.config.get("AUTH_MODE", "SESSION_ONLY")
    return mode if isinstance(mode, str) else "SESSION_ONLY"


def campaign_admin_required(f):
    """Decorator: SESSION_ONLY auth + ADMIN/SUPER_ADMIN role required.

    Decorator order on every mutation route:
        @require_campaign_engine    # outermost — 404 when flag OFF
        @campaign_admin_required    # then mode check, then authn, then role

    AUTH_MODE gate (ADR-023 D1):
        Returns 404 when AUTH_MODE is not SESSION_ONLY. 404 matches the
        non-disclosure posture of require_campaign_engine — a misconfigured
        deployment must not advertise the V2 surface via a mode-specific error.
        DUAL is refused for the same structural reason as ADMIN_KEY_ONLY: the
        key branch yields no current_user, _actor_tenant_id() returns None, and
        get_current_actor() gives the key path precedence over an active session,
        creating a privilege-escalation path around the role gate (ADR-023 R1).

    Delegates entirely to existing admin helpers — no new auth logic here:
      - _check_auth()         for authentication
      - get_current_actor()   for role resolution

    Returns JSON 4xx (not HTML redirects) because every route in this
    blueprint is a JSON API.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        if _auth_mode() != "SESSION_ONLY":
            return jsonify({"error": "Not found"}), 404
        if not _check_auth():
            return jsonify({"error": "Unauthorized"}), 403
        from app.routes.admin import get_current_actor
        if get_current_actor().get("role") not in ("ADMIN", "SUPER_ADMIN"):
            return jsonify({"error": "Forbidden"}), 403
        return f(*args, **kwargs)
    return wrapper


# ── Serialisation (no business logic — shape only) ────────────────────────────

def _campaign_summary(c) -> dict:
    """Minimal campaign summary for list responses.

    Includes denormalised counters from the model (maintained by the worker);
    does NOT call CampaignRepository — the Campaign ORM object already carries
    them so no extra query is needed per row.
    """
    return {
        "id":               c.id,
        "name":             c.name,
        "status":           c.status,
        "total_recipients": c.total_recipients,
        "sent_count":       c.sent_count,
        "failed_count":     c.failed_count,
        "scheduled_at":     c.scheduled_at.isoformat() if c.scheduled_at else None,
        "started_at":       c.started_at.isoformat()   if c.started_at   else None,
        "completed_at":     c.completed_at.isoformat() if c.completed_at else None,
        "created_at":       c.created_at.isoformat()   if c.created_at   else None,
        "created_by":       c.created_by,
        # Phase 9.1a: both columns were already persisted but never returned.
        # audience_segment is the campaign's provenance (ADR-025 D8) — which
        # segment produced this recipient set. impersonated_by closes the
        # outstanding *visibility* half of ADR-023 D3: the marker was written
        # at create time (8.2E.6B) but no surface ever read it back, leaving a
        # SUPER_ADMIN-created campaign correctly recorded and invisible.
        "audience_segment": c.audience_segment,
        "impersonated_by":  c.impersonated_by,
    }


def _campaign_detail(c) -> dict:
    """Full campaign detail for single-resource responses.

    Extends the summary with content fields (message_body, description, ids)
    that would inflate list responses but are useful on the detail page.
    """
    d = _campaign_summary(c)
    d.update({
        "description":      c.description,
        "message_body":     c.message_body,
        "template_id":      c.template_id,
        "audience_rule_id": c.audience_rule_id,
        "failure_reason":   c.failure_reason,
        "updated_at":       c.updated_at.isoformat() if c.updated_at else None,
    })
    return d


# ── Service factory (lazy, injectable for tests) ──────────────────────────────

def _make_service():
    """Instantiate CampaignService with lazy DB collaborators (production path)."""
    from app.marketing.campaign_service import CampaignService
    return CampaignService()


# ── Read routes (Phase 8.2D.2) ────────────────────────────────────────────────

_PAGE_SIZE = 50   # matches CampaignRepository.list_for_tenant default


@marketing_bp.route("", methods=["GET"])
@require_campaign_engine
def list_campaigns():
    """GET /crm/campaigns/v2 — list campaigns for the current tenant.

    Query params:
        status  (optional) — filter by lifecycle status
        page    (optional, default 1) — 1-indexed page number
        limit   (optional, default 50, max 100) — items per page

    Returns:
        {campaigns: [...], total: int, page: int, limit: int, pages: int}
    """
    from flask import request as req
    from app.marketing.campaign_service import CampaignService

    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 403

    tenant_id, err = _require_tenant()
    if err:
        return err

    status = req.args.get("status") or None
    page   = max(1, req.args.get("page",  1,           type=int))
    limit  = min(100, max(1, req.args.get("limit", _PAGE_SIZE, type=int)))
    offset = (page - 1) * limit

    svc = _make_service()
    campaigns = svc.list_campaigns(tenant_id, status=status, limit=limit, offset=offset)

    # Total count for pagination metadata — one COUNT query, reusing the same
    # service scope (tenant + optional status filter).
    repo = svc.repository
    total = repo.count_for_tenant(tenant_id, status=status)
    pages = max(1, -(-total // limit))   # ceiling division

    return jsonify({
        "campaigns": [_campaign_summary(c) for c in campaigns],
        "total":  total,
        "page":   page,
        "limit":  limit,
        "pages":  pages,
    })


@marketing_bp.route("/<int:campaign_id>", methods=["GET"])
@require_campaign_engine
def get_campaign(campaign_id):
    """GET /crm/campaigns/v2/<campaign_id> — campaign detail."""
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 403

    tenant_id, err = _require_tenant()
    if err:
        return err

    svc = _make_service()
    campaign = svc.get_campaign(tenant_id, campaign_id)
    if campaign is None:
        return jsonify({"error": "Campaign not found"}), 404

    return jsonify(_campaign_detail(campaign))


@marketing_bp.route("", methods=["POST"])
@require_campaign_engine
@campaign_admin_required
def create_campaign():
    """POST /crm/campaigns/v2 — create a campaign draft.

    Accepts JSON:
        name             (required)
        description      (optional)
        message_body     (optional, mutually exclusive with template_id)
        template_id      (optional, mutually exclusive with message_body)
        audience_rule_id (optional)
        audience_segment (optional) — the named segment this campaign will
                         resolve at launch (ADR-025 D8)

    Validation is entirely owned by CampaignService.create_campaign() — the
    route does not duplicate any rules. On success returns 201 with the full
    campaign detail. On CampaignValidationError returns 400 with structured
    field errors.
    """
    from flask import request as req

    tenant_id, err = _require_tenant()
    if err:
        return err

    body = req.get_json(silent=True) or {}

    # get_current_actor() was already called by campaign_admin_required; calling
    # it again here is cheap (no DB) and keeps created_by resolution local.
    from app.routes.admin import get_current_actor
    actor = get_current_actor()
    created_by = actor.get("username") if actor.get("authenticated") else None

    # Phase 8.2E.6B (ADR-023 D3): record the SUPER_ADMIN identity when this
    # create was performed under impersonation. The session key is read here in
    # the route; CampaignService must never access Flask session directly.
    impersonated_by = _resolve_impersonated_by(actor)

    try:
        svc = _make_service()
        campaign = svc.create_campaign(
            tenant_id,
            name=body.get("name"),
            description=body.get("description"),
            message_body=body.get("message_body"),
            template_id=body.get("template_id"),
            audience_rule_id=body.get("audience_rule_id"),
            # Phase 9.1F (ADR-025 D8): the segment is "written once at create
            # time, or before launch". CampaignService and the repository have
            # accepted this since 8.2E.9-C, but the route never forwarded it,
            # so a segment supplied at create was silently discarded and only
            # ever persisted later by mark_running(). That left drafts with a
            # NULL segment and defeated D8's stated purpose — giving a future
            # scheduled launch something to read without a live request.
            audience_segment=body.get("audience_segment"),
            created_by=created_by,
            impersonated_by=impersonated_by,
        )
        return jsonify(_campaign_detail(campaign)), 201
    except Exception as exc:
        return _map_campaign_error(exc)


# ── Lifecycle routes (Phase 8.2D.4) ──────────────────────────────────────────

def _run_lifecycle(campaign_id, action_fn):
    """Common guard sequence for lifecycle mutation routes.

    Checks auth, tenant, and existence before calling action_fn.
    Returns 404 when the campaign is not found for the current tenant —
    the service raises CampaignValidationError for not-found, but the correct
    HTTP semantic is 404, not 400. A pre-check here makes the distinction
    explicit without inspecting the error message.

    action_fn signature: (svc, tenant_id) -> Campaign
    """
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 403

    tenant_id, err = _require_tenant()
    if err:
        return err

    svc = _make_service()
    if svc.get_campaign(tenant_id, campaign_id) is None:
        return jsonify({"error": "Campaign not found"}), 404

    try:
        campaign = action_fn(svc, tenant_id)
        return jsonify(_campaign_detail(campaign))
    except Exception as exc:
        return _map_campaign_error(exc)


@marketing_bp.route("/<int:campaign_id>/validate", methods=["POST"])
@require_campaign_engine
@campaign_admin_required
def validate_campaign(campaign_id):
    """POST /crm/campaigns/v2/<id>/validate — draft → validated."""
    return _run_lifecycle(
        campaign_id,
        lambda svc, tid: svc.mark_validated(tid, campaign_id),
    )


@marketing_bp.route("/<int:campaign_id>/schedule", methods=["POST"])
@require_campaign_engine
@campaign_admin_required
def schedule_campaign(campaign_id):
    """POST /crm/campaigns/v2/<id>/schedule — validated → scheduled.

    Expects JSON body: {"scheduled_at": "<ISO 8601 datetime string>"}

    Datetime parsing happens in the route so the service receives a proper
    datetime object. A missing or unparseable value returns 400 before the
    service is called — no ambiguity about whether the error came from the
    scheduler or the parser.
    """
    from flask import request as req
    from datetime import datetime as _dt

    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 403

    tenant_id, err = _require_tenant()
    if err:
        return err

    body   = req.get_json(silent=True) or {}
    raw_dt = body.get("scheduled_at")
    if not raw_dt:
        return jsonify({"error": "scheduled_at is required"}), 400
    try:
        scheduled_at = _dt.fromisoformat(str(raw_dt))
    except (ValueError, TypeError):
        return jsonify({"error": "scheduled_at must be a valid ISO 8601 datetime"}), 400

    svc = _make_service()
    if svc.get_campaign(tenant_id, campaign_id) is None:
        return jsonify({"error": "Campaign not found"}), 404

    try:
        campaign = svc.schedule(tenant_id, campaign_id, scheduled_at)
        return jsonify(_campaign_detail(campaign))
    except Exception as exc:
        return _map_campaign_error(exc)


@marketing_bp.route("/<int:campaign_id>/launch", methods=["POST"])
@require_campaign_engine
@campaign_admin_required
def launch_campaign(campaign_id):
    """POST /crm/campaigns/v2/<id>/launch — validated/scheduled → running.

    Arming the campaign worker: once this transition commits, the polling
    worker will pick up queued recipients on its next cycle. The route does
    NOT start a thread, call send_automation(), or touch recipient rows
    directly — CampaignService.mark_running() resolves and materialises the
    audience (ADR-025), and dispatch is exclusively the worker's
    responsibility (Phase 8.2C).

    Accepts an optional JSON body:
        audience_segment  (optional) — persisted onto the campaign before
                                        resolving (ADR-025 D8); omit to launch
                                        using whatever segment was already set
        acknowledged       (optional, default false) — required (ADR-025
                                        D6.2) whenever GET .../preview reports
                                        template_required > 0 for this segment

    Audit is owned by CampaignService._audit_status_changed(). No duplicate
    audit logic here.
    """
    def _launch(svc, tid):
        from flask import request as req
        body = req.get_json(silent=True) or {}
        return svc.mark_running(
            tid, campaign_id,
            audience_segment=body.get("audience_segment"),
            acknowledged=bool(body.get("acknowledged", False)),
        )

    return _run_lifecycle(campaign_id, _launch)


@marketing_bp.route("/<int:campaign_id>/cancel", methods=["POST"])
@require_campaign_engine
@campaign_admin_required
def cancel_campaign(campaign_id):
    """POST /crm/campaigns/v2/<id>/cancel — running → cancelled.

    Phase 9.6A: this docstring previously claimed
    "running/validated/scheduled -> cancelled", contradicting the transition
    map in campaign_service.py, which permits the cancelled state only from
    running. A validated or scheduled campaign returns 409 here. The prose was
    the sole source of that claim and had already misled the Phase 9.3 Details
    UI into offering Cancel where the server refuses it; corrected to match
    the map, which is the authority.
    """
    return _run_lifecycle(
        campaign_id,
        lambda svc, tid: svc.cancel(tid, campaign_id),
    )


@marketing_bp.route("/<int:campaign_id>/archive", methods=["POST"])
@require_campaign_engine
@campaign_admin_required
def archive_campaign(campaign_id):
    """POST /crm/campaigns/v2/<id>/archive — terminal → archived."""
    return _run_lifecycle(
        campaign_id,
        lambda svc, tid: svc.archive(tid, campaign_id),
    )


@marketing_bp.route("/<int:campaign_id>/progress", methods=["GET"])
@require_campaign_engine
def campaign_progress(campaign_id):
    """GET /crm/campaigns/v2/<campaign_id>/progress — recipient status roll-up.

    Returns the raw {status: count} breakdown from status_breakdown(), plus
    a derived `total` for convenience. No aggregation logic lives here — the
    sum is arithmetic on the map, not a DB query.
    """
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 403

    tenant_id, err = _require_tenant()
    if err:
        return err

    svc = _make_service()

    # Verify the campaign exists and belongs to this tenant before exposing
    # progress — progress() on an unknown id would return an empty dict, which
    # is ambiguous (no recipients vs. wrong tenant vs. not found).
    if svc.get_campaign(tenant_id, campaign_id) is None:
        return jsonify({"error": "Campaign not found"}), 404

    breakdown = svc.progress(tenant_id, campaign_id)
    return jsonify({
        "campaign_id": campaign_id,
        "breakdown":   breakdown,
        "total":       sum(breakdown.values()),
    })


# ── Recipient Inspector (Phase 9.4) ───────────────────────────────────────────

def _recipient_summary(r) -> dict:
    """Per-recipient shape for the Recipient Inspector.

    tenant_id and campaign_id are withheld — implicit in the request scope,
    same rationale as Campaign._campaign_summary withholding tenant_id.
    delivered_at/read_at are included but will read null for every row until
    a future Meta status webhook writes them (see CampaignRecipient docstring,
    app/models.py) — this endpoint does not change that.
    """
    return {
        "id":              r.id,
        "phone":           r.phone,
        "name":            r.name,
        "status":          r.status,
        "retry_count":     r.retry_count,
        "failure_reason":  r.failure_reason,
        "wa_message_id":   r.wa_message_id,
        "send_at":         r.send_at.isoformat()         if r.send_at         else None,
        "last_attempt_at": r.last_attempt_at.isoformat() if r.last_attempt_at else None,
        "sent_at":         r.sent_at.isoformat()         if r.sent_at         else None,
        "delivered_at":    r.delivered_at.isoformat()    if r.delivered_at    else None,
        "read_at":         r.read_at.isoformat()         if r.read_at         else None,
        "created_at":      r.created_at.isoformat()      if r.created_at      else None,
    }


@marketing_bp.route("/<int:campaign_id>/recipients", methods=["GET"])
@require_campaign_engine
@campaign_admin_required
def list_campaign_recipients(campaign_id):
    """GET /crm/campaigns/v2/<campaign_id>/recipients — paginated recipient rows.

    ADMIN/SUPER_ADMIN only (@campaign_admin_required), unlike the other GET
    routes on this blueprint which are authn-only. This is the one GET that
    returns bulk recipient PII (phone numbers, names) for a campaign; the
    legacy CRM restricts STAFF to leads assigned to them (see crm_leads(),
    crm_lead_detail() in app/routes/admin.py), and an authn-only recipient
    list would let any STAFF user enumerate numbers for leads they are not
    assigned to. Gating this route closes that gap (Phase 9.4 discovery,
    approved 2026-07-28).

    Query params:
        status  (optional) — filter by recipient status
        page    (optional, default 1)
        limit   (optional, default 50, max 100)
    """
    from flask import request as req

    tenant_id, err = _require_tenant()
    if err:
        return err

    svc = _make_service()

    # 404 the campaign first — an empty recipient list is otherwise ambiguous
    # between "no recipients", "wrong tenant" and "not found" (same rationale
    # as campaign_progress()).
    if svc.get_campaign(tenant_id, campaign_id) is None:
        return jsonify({"error": "Campaign not found"}), 404

    status = req.args.get("status") or None
    page   = max(1, req.args.get("page",  1,           type=int))
    limit  = min(100, max(1, req.args.get("limit", _PAGE_SIZE, type=int)))
    offset = (page - 1) * limit

    recipients = svc.list_recipients(tenant_id, campaign_id, status=status, limit=limit, offset=offset)
    total = svc.count_recipients(tenant_id, campaign_id, status=status)
    pages = max(1, -(-total // limit))   # ceiling division

    return jsonify({
        "campaign_id": campaign_id,
        "recipients":  [_recipient_summary(r) for r in recipients],
        "total":  total,
        "page":   page,
        "limit":  limit,
        "pages":  pages,
    })


# ── Audience preview (Phase 8.2E.9-B, ADR-025 D6/D7) ──────────────────────────
#
# Read-only. Reports what a launch WOULD do; materialises nothing. Launch
# itself (and the D6.2 acknowledgement precondition) is Phase 8.2E.9-C.

@marketing_bp.route("/segments", methods=["GET"])
@require_campaign_engine
def list_audience_segments():
    """GET /crm/campaigns/v2/segments — the selectable audience segments.

    Names only, no per-segment sizing — sizing is tied to a specific
    campaign's reachability and template state (see .../preview) and is not
    meaningful in isolation.
    """
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 403

    from app.marketing.audience_resolver import list_segments
    return jsonify({"segments": list(list_segments())})


@marketing_bp.route("/<int:campaign_id>/preview", methods=["GET"])
@require_campaign_engine
def campaign_audience_preview(campaign_id):
    """GET /crm/campaigns/v2/<campaign_id>/preview?segment=<name>

    ADR-025 D6.1: the reachability disclosure an operator must see before
    launch — total audience, opt-out exclusions, contacts reachable now
    (24h window open) vs. requiring an approved template (window closed).

    ADR-025 D7: template readiness reported per-condition (configured /
    found / correct tenant / approved / provider id present), not collapsed
    to a single pass/fail, so a failure is diagnosable.

    `expected_failed` and `requires_acknowledgement` are the derived fields
    D6.1/D6.2 call for: the count that will fail given the CURRENT template
    state, and whether launch will require operator acknowledgement. Both are
    computed here, not left for the caller to infer from raw counts.
    """
    from flask import request as req

    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 403

    tenant_id, err = _require_tenant()
    if err:
        return err

    svc = _make_service()
    campaign = svc.get_campaign(tenant_id, campaign_id)
    if campaign is None:
        return jsonify({"error": "Campaign not found"}), 404

    segment = req.args.get("segment")
    if not segment:
        return jsonify({"error": "segment query parameter is required"}), 400

    from app.marketing.audience_resolver import preview as resolve_preview, AudienceResolutionError
    try:
        breakdown = resolve_preview(tenant_id, segment)
    except AudienceResolutionError as exc:
        return jsonify({"error": "Validation failed", "detail": str(exc)}), 400

    from app.marketing.campaign_service import describe_campaign_template
    template = describe_campaign_template(tenant_id, campaign.template_id)

    template_required = breakdown["template_required"]
    expected_failed = 0 if template["ready"] else template_required
    requires_acknowledgement = template_required > 0

    return jsonify({
        "campaign_id": campaign_id,
        **breakdown,
        "template": template,
        "expected_failed": expected_failed,
        "requires_acknowledgement": requires_acknowledgement,
    })
