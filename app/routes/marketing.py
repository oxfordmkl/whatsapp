"""
Phase 8.2D — Marketing blueprint: Campaign V2 HTTP surface.

Registered unconditionally; every route is internally gated by
CAMPAIGN_ENGINE_V2. When OFF (production today) all routes return 404.

Layering:
    marketing_bp (here)
      → CampaignService  (app/marketing/campaign_service.py)
        → CampaignRepository (app/persistence/campaign_repository.py)

Auth: session + RBAC via admin_required / check_auth() — matches admin_bp.
Tenant: _actor_tenant_id() only; None always refuses (ADR-021).

Phase 8.2D.1: skeleton (blueprint, helpers, no routes).
Phase 8.2D.2: read routes — list, detail, progress.
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
def create_campaign():
    """POST /crm/campaigns/v2 — create a campaign draft.

    Accepts JSON:
        name            (required)
        description     (optional)
        message_body    (optional, mutually exclusive with template_id)
        template_id     (optional, mutually exclusive with message_body)
        audience_rule_id (optional)

    Validation is entirely owned by CampaignService.create_campaign() — the
    route does not duplicate any rules. On success returns 201 with the full
    campaign detail. On CampaignValidationError returns 400 with structured
    field errors.
    """
    from flask import request as req

    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 403

    tenant_id, err = _require_tenant()
    if err:
        return err

    body = req.get_json(silent=True) or {}

    # Resolve the acting user for the created_by audit field. Lazy import to
    # avoid pulling admin.py at module load time.
    from app.routes.admin import get_current_actor
    actor = get_current_actor()
    created_by = actor.get("username") if actor.get("authenticated") else None

    try:
        svc = _make_service()
        campaign = svc.create_campaign(
            tenant_id,
            name=body.get("name"),
            description=body.get("description"),
            message_body=body.get("message_body"),
            template_id=body.get("template_id"),
            audience_rule_id=body.get("audience_rule_id"),
            created_by=created_by,
        )
        return jsonify(_campaign_detail(campaign)), 201
    except Exception as exc:
        return _map_campaign_error(exc)


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
