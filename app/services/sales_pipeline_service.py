"""Phase 10.7 — Sales Pipeline read/query service.

ALL sales-pipeline query logic lives here. Routes render, templates display;
neither builds a query. That separation is deliberate: 16 calculate_* helpers
currently sit in admin.py and each carries the same full-table-scan pattern
(Phase 10.1 found ~20 routes doing it), so a new analytics surface must not
add a seventeenth.

Sales vs AI funnel
------------------
sales_stage_id is the ONLY reference used here. ConversationState.stage and
pipeline_stage_id belong to the AI conversation engine and are never read or
written by this module. Note that _build_leads_query()'s `stage_filter`
argument filters the AI funnel — passing a sales stage to it would silently
conflate the two pipelines, so this module filters on sales_stage_id directly.

Tenant + ownership
------------------
Every function takes tenant_id explicitly (never inferred) and `actor`, and the
STAFF ownership restriction is applied INSIDE the aggregate, not only on detail
views. A STAFF member owning 11 leads must not see "Lead 53" — a tenant-wide
count is a leak even when no individual lead is exposed.
"""
import logging

logger = logging.getLogger(__name__)


def _is_staff(actor) -> bool:
    """True when the actor is a session STAFF user (not ADMIN/SUPER_ADMIN)."""
    return bool(actor) and actor.get("source") == "SESSION" and actor.get("role") == "STAFF"


def _staff_ownership_clause(actor):
    """The `assigned_staff == me` predicate, or None when it does not apply.

    Mirrors the normalisation used by _build_leads_query and crm_my_leads
    (lower+trim on both sides) so ownership resolves identically everywhere.
    """
    if not _is_staff(actor):
        return None
    from app.models import ConversationState
    from sqlalchemy import func
    username = (actor.get("username") or "").strip().lower()
    return func.lower(func.trim(ConversationState.assigned_staff)) == username


def get_pipeline_summary(tenant_id, actor=None):
    """Per-stage lead counts for a tenant's sales pipeline.

    Returns a list of dicts ordered by order_index:
        {stage_id, internal_key, display_name, stage_category,
         order_index, is_terminal, lead_count, share_pct}

    Driven FROM pipeline_stages with a LEFT JOIN to leads, so a stage with no
    leads still appears with a count of 0. Grouping from the lead side would
    silently drop empty stages, which is exactly the part of a pipeline an
    operator most needs to see.

    One GROUP BY, not one query per stage. The ownership predicate is applied
    in the JOIN condition rather than a WHERE clause — in a LEFT JOIN, a WHERE
    on the right-hand table would discard the unmatched rows and reintroduce
    the missing-stage bug for STAFF users.
    """
    from app.models import ConversationState, PipelineDefinition, PipelineStage, SALES_PIPELINE_KEY
    from app.extensions import db
    from sqlalchemy import func

    if not tenant_id:
        return []

    join_on = [
        ConversationState.sales_stage_id == PipelineStage.id,
        ConversationState.tenant_id == tenant_id,
    ]
    ownership = _staff_ownership_clause(actor)
    if ownership is not None:
        join_on.append(ownership)

    rows = (
        db.session.query(
            PipelineStage.id,
            PipelineStage.internal_key,
            PipelineStage.display_name,
            PipelineStage.stage_category,
            PipelineStage.order_index,
            PipelineStage.is_terminal,
            func.count(ConversationState.id).label("lead_count"),
        )
        .join(PipelineDefinition, PipelineStage.pipeline_id == PipelineDefinition.id)
        .outerjoin(ConversationState, db.and_(*join_on))
        .filter(
            PipelineDefinition.tenant_id == tenant_id,
            PipelineDefinition.internal_key == SALES_PIPELINE_KEY,
            PipelineStage.is_active.is_(True),
        )
        .group_by(
            PipelineStage.id, PipelineStage.internal_key, PipelineStage.display_name,
            PipelineStage.stage_category, PipelineStage.order_index, PipelineStage.is_terminal,
        )
        .order_by(PipelineStage.order_index)
        .all()
    )

    total = sum(r.lead_count for r in rows)
    return [
        {
            "stage_id": r.id,
            "internal_key": r.internal_key,
            "display_name": r.display_name,
            "stage_category": r.stage_category,
            "order_index": r.order_index,
            "is_terminal": r.is_terminal,
            "lead_count": r.lead_count,
            # Share of pipeline (approved metric). Guarded against an empty
            # pipeline so a tenant with no leads renders 0.0 rather than
            # raising ZeroDivisionError.
            "share_pct": round(100.0 * r.lead_count / total, 1) if total else 0.0,
        }
        for r in rows
    ]


def get_conversion_metrics(summary):
    """Derive headline metrics from an existing summary — no extra query.

    win_rate is won / (won + lost), i.e. share of CLOSED outcomes, and is None
    when nothing has closed. It is deliberately not won/total: with 59 leads
    still open, that would read 7.8% and look like failure rather than a
    pipeline in progress.

    `lost_recorded` is returned so the UI can caveat the figure. Today lost is
    0, which makes win_rate 100% — arithmetically correct and, without that
    caveat, badly misleading.
    """
    total = sum(s["lead_count"] for s in summary)
    won = sum(s["lead_count"] for s in summary if s["stage_category"] == "won")
    lost = sum(s["lead_count"] for s in summary if s["stage_category"] == "lost")
    closed = won + lost
    return {
        "total_leads": total,
        "open_count": sum(s["lead_count"] for s in summary if s["stage_category"] == "open"),
        "won_count": won,
        "lost_count": lost,
        "closed_count": closed,
        "win_rate": round(100.0 * won / closed, 1) if closed else None,
        "lost_recorded": lost > 0,
        "has_stages": bool(summary),
    }


def get_stage(tenant_id, stage_id):
    """Load one sales stage, or None if it is not this tenant's.

    The tenant check is the security boundary for the detail view: stage_id
    arrives from the URL and must never be trusted. Scoping the lookup means a
    cross-tenant id returns None and the caller 404s, rather than resolving a
    stage name belonging to another customer.

    Also refuses a stage from the AI funnel's pipeline — only the 'sales'
    definition resolves here.
    """
    from app.models import PipelineDefinition, PipelineStage, SALES_PIPELINE_KEY
    from app.extensions import db

    if not tenant_id or not stage_id:
        return None
    return (
        db.session.query(PipelineStage)
        .join(PipelineDefinition, PipelineStage.pipeline_id == PipelineDefinition.id)
        .filter(
            PipelineStage.id == stage_id,
            PipelineDefinition.tenant_id == tenant_id,
            PipelineDefinition.internal_key == SALES_PIPELINE_KEY,
        )
        .first()
    )


# ── Phase 10.8: stage movement recording ─────────────────────────────────────
#
# Actor labels for movements no operator initiated. Kept as constants so the
# notification-suppression rules below and the history rows agree on one
# vocabulary rather than repeating string literals at each call site.
ACTOR_CSV_IMPORT = "csv-import"
ACTOR_AUTO_ADMISSION = "auto-admission"


def record_stage_change(tenant_id, lead, from_stage_id, from_status,
                        actor=None, notify=False, notify_actor_name=None):
    """Record one Sales Pipeline movement. Call AFTER the business commit.

    Three complementary records, each with a distinct job:

      * lead_stage_history — analytics substrate (velocity, time-in-stage)
      * LeadEvent STAGE_CHANGED — the operator-visible lead timeline
      * notification — only when `notify` is True (see below)

    The audit_log entry is NOT written here: crm_lead_update already emits
    LEAD_STATUS_CHANGE and owns that record, so writing a second one would
    double-count. This function returns the stage ids so that caller can
    enrich its existing audit detail.

    NEVER RAISES. This runs after the lead write is already durable, so a
    failure here must not surface as a failed edit — the movement is recorded
    on a best-effort basis and logged loudly if it cannot be.

    `notify` is False by default and deliberately opt-in per call site.
    Approved policy (Phase 10.8): notify only on operator-initiated single
    changes — never CSV import (a 500-row file would emit 500 notifications)
    and never the automatic admission promotion.

    Returns {"from_stage_id", "to_stage_id"} for audit enrichment, or None if
    nothing was recorded.
    """
    from app.extensions import db

    try:
        to_stage_id = lead.sales_stage_id
        to_status = lead.lead_status

        # No movement, nothing to record. Guards against a form resubmit
        # writing an identical status and inflating the history.
        if from_stage_id == to_stage_id and (from_status or "") == (to_status or ""):
            return None

        _write_history(tenant_id, lead, from_stage_id, from_status,
                       to_stage_id, to_status, actor)
        _write_timeline(tenant_id, lead, from_status, to_status)

        if notify:
            _notify_stage_change(tenant_id, lead, from_status, to_status,
                                 notify_actor_name)

        return {"from_stage_id": from_stage_id, "to_stage_id": to_stage_id}
    except Exception:
        logger.exception(
            "[pipeline] FAILED to record stage change for lead %s — the lead "
            "edit itself is committed and unaffected", getattr(lead, "id", "?"))
        return None


def _write_history(tenant_id, lead, from_stage_id, from_status,
                   to_stage_id, to_status, actor):
    """Append the analytics row. Owns its own commit, like log_audit()."""
    from app.models import LeadStageHistory
    from app.extensions import db

    db.session.add(LeadStageHistory(
        tenant_id=tenant_id,
        conversation_state_id=lead.id,
        from_stage_id=from_stage_id,
        to_stage_id=to_stage_id,
        from_status=from_status,
        to_status=to_status,
        actor=actor,
    ))
    db.session.commit()


def _write_timeline(tenant_id, lead, from_status, to_status):
    """Put the movement on the operator-visible lead timeline.

    Reuses log_lead_event (which never raises) rather than writing LeadEvent
    directly, so the timeline entry is created exactly like every other one.
    """
    import json
    from app.services.log_service import log_lead_event

    log_lead_event(
        tenant_id=tenant_id,
        phone=lead.phone,
        event_type="STAGE_CHANGED",
        event_data=json.dumps({"from": from_status or "", "to": to_status or ""}),
    )


def _notify_stage_change(tenant_id, lead, from_status, to_status, actor_name):
    """Tell the assigned staff member their lead moved stage.

    Suppressed when there is no assignee (nobody to tell) and when the actor
    is the assignee — a staff member who just moved their own lead does not
    need to be told they did it.
    """
    from app.models import Notification
    from app.services import notification_service

    recipient = (lead.assigned_staff or "").strip()
    if not recipient:
        return
    if actor_name and recipient.lower() == actor_name.strip().lower():
        return

    label = (lead.name or "").strip() or lead.phone
    notification_service.notify(
        tenant_id=tenant_id,
        recipient=recipient,
        notif_type=Notification.TYPE_STAGE_CHANGED,
        title=f"Stage changed: {label}",
        body=f"{from_status or 'unset'} -> {to_status or 'unset'}"
             + (f" by {actor_name}" if actor_name else ""),
        lead_phone=lead.phone,
    )


def get_stage_history(tenant_id, lead_id, limit=20):
    """Recent stage movements for one lead, newest first.

    Tenant-scoped on this table's own tenant_id rather than by joining the
    lead, so a mis-scoped call cannot read another tenant's history.
    """
    from app.models import LeadStageHistory
    from app.extensions import db

    if not tenant_id or not lead_id:
        return []
    return (db.session.query(LeadStageHistory)
            .filter(LeadStageHistory.tenant_id == tenant_id,
                    LeadStageHistory.conversation_state_id == lead_id)
            .order_by(LeadStageHistory.changed_at.desc(),
                      LeadStageHistory.id.desc())
            .limit(limit).all())


def get_stage_leads(tenant_id, stage_id, actor=None, page=1, per_page=25):
    """Paginated leads sitting in one sales stage.

    Filters on sales_stage_id — never on ConversationState.stage, which is the
    AI funnel. Ownership and tenant scoping are applied here, so the route
    cannot forget them.

    Returns a Flask-SQLAlchemy Pagination, matching what crm_leads and
    crm_my_leads already hand to _pagination.html.
    """
    from app.models import ConversationState
    from app.extensions import db

    if not tenant_id or not stage_id:
        return None

    q = db.session.query(ConversationState).filter(
        ConversationState.tenant_id == tenant_id,
        ConversationState.sales_stage_id == stage_id,
    )
    ownership = _staff_ownership_clause(actor)
    if ownership is not None:
        q = q.filter(ownership)

    return (q.order_by(ConversationState.updated_at.desc())
             .paginate(page=page, per_page=per_page, error_out=False))
