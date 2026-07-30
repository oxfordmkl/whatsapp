"""Phase 10.6 — Sales Pipeline seeding and lead linking.

Standalone by design, like backfill_service: invoked from a script, never from
a request path. Seeding is a migration activity, not application behaviour.

What it does, per tenant:

    Step 1  create the tenant's SALES PipelineDefinition (internal_key='sales')
    Step 2  seed its stages from models.LEAD_STATUSES
    Step 3  link each lead's sales_stage_id from its existing lead_status

The bot funnel's PipelineDefinition is never touched. Both live in
pipeline_stages, separated by their parent definition — see the migration
docstring for why they must not share a column.

Ordering matters
----------------
The write path (the lead_status adapter's setter) ships BEFORE this runs.
Backfilling first is what failed last time: pipeline_stage_id was linked on 29
rows in Phase 16.5A6 and then decayed, because nothing maintained the link on
create or update. With the setter in place, this script only has to catch up
history; new and edited rows keep themselves linked.

Safety
------
  * DRY RUN is the default. LIVE must be requested explicitly.
  * Idempotent: re-running reuses existing rows rather than duplicating.
  * Batched, and safe to interrupt — committed batches stay correct.
  * A tenant that fails is abandoned; other tenants continue.
  * Never writes lead_status. The legacy string is left exactly as-is, so
    every step is reversible by clearing sales_stage_id.
"""
import logging

logger = logging.getLogger(__name__)

BATCH_SIZE = 200

# Stage metadata derived from the approved vocabulary.
#
# stage_category closes the gap Phase 10.4 identified: the bot pipeline has 11
# 'open' and 1 'won' and NO 'lost' stage at all, which made win/loss reporting
# structurally impossible. The sales pipeline defines both outcomes from the
# start.
_WON_STATUSES = frozenset({"Enrolled"})
_LOST_STATUSES = frozenset({"Lost", "Not Interested"})


def _slugify(value):
    """internal_key from a display name. Mirrors backfill_service.slugify."""
    import re
    t = value.lower()
    t = re.sub(r"[^a-z0-9]+", "_", t)
    return t.strip("_")[:50].rstrip("_") or "stage"


def _stage_rows():
    """(internal_key, display_name, category, order, is_entry, is_terminal).

    Order follows LEAD_STATUSES, which is already authored as a funnel
    sequence, so order_index is simply its position. is_terminal is taken from
    LEAD_TERMINAL_STATUSES rather than restated, so the two cannot drift.
    """
    from app.models import LEAD_STATUSES, LEAD_TERMINAL_STATUSES
    rows = []
    for i, name in enumerate(LEAD_STATUSES):
        if name in _WON_STATUSES:
            category = "won"
        elif name in _LOST_STATUSES:
            category = "lost"
        else:
            category = "open"
        rows.append((
            _slugify(name),
            name,
            category,
            i,
            i == 0,                              # 'Lead' is the entry stage
            name in LEAD_TERMINAL_STATUSES,
        ))
    return rows


class SalesPipelineReport:
    def __init__(self, tenant_id):
        self.tenant_id = tenant_id
        self.pipeline_created = 0
        self.pipeline_reused = 0
        self.stages_created = 0
        self.stages_reused = 0
        self.leads_linked = 0
        self.leads_already = 0
        self.leads_unlinkable = 0
        self.unlinkable_values = []
        self.batches = 0
        self.aborted = None


class SalesPipelineSeeder:
    """Seed sales pipelines and link leads. dry_run=True writes nothing."""

    def __init__(self, dry_run=True, batch_size=BATCH_SIZE):
        self.dry_run = dry_run
        self.batch_size = batch_size

    # ── Entry point ───────────────────────────────────────────────────────
    def run(self):
        from app.models import Tenant
        mode = "DRY RUN" if self.dry_run else "LIVE"
        logger.info("Phase 10.6 sales pipeline seed — %s", mode)

        reports = []
        for tenant in Tenant.query.order_by(Tenant.created_at).all():
            report = SalesPipelineReport(tenant.id)
            try:
                pipeline_id = self._step1_pipeline(tenant.id, report)
                stage_map = self._step2_stages(pipeline_id, report)
                self._step3_link(tenant.id, stage_map, report)
            except Exception as exc:
                # One tenant's failure must not abandon the rest. Committed
                # batches for this tenant remain valid and correct.
                self._rollback()
                report.aborted = f"{type(exc).__name__}: {exc}"
                logger.warning("tenant %s aborted — %s", tenant.id, report.aborted)
            reports.append(report)
        return reports

    def _rollback(self):
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:
            pass

    # ── Step 1 — the sales PipelineDefinition ─────────────────────────────
    def _step1_pipeline(self, tenant_id, report):
        from app.models import PipelineDefinition, SALES_PIPELINE_KEY
        from app.extensions import db

        existing = (PipelineDefinition.query
                    .filter_by(tenant_id=tenant_id, internal_key=SALES_PIPELINE_KEY)
                    .first())
        if existing is not None:
            report.pipeline_reused += 1
            return existing.id

        report.pipeline_created += 1
        if self.dry_run:
            return None

        # is_default is NOT set here. The bot funnel may already hold it, and
        # silently moving a tenant's default pipeline is a behavioural change
        # this phase has no mandate to make.
        pipeline = PipelineDefinition(
            tenant_id=tenant_id,
            internal_key=SALES_PIPELINE_KEY,
            name="Sales Pipeline",
            description="Operator-managed sales stages (Phase 10.6). "
                        "Distinct from the AI conversation funnel.",
            is_default=False,
            is_active=True,
        )
        db.session.add(pipeline)
        db.session.flush()
        db.session.commit()
        return pipeline.id

    # ── Step 2 — stages ───────────────────────────────────────────────────
    def _step2_stages(self, pipeline_id, report):
        """Returns {lowercased display_name: stage_id} for Step 3.

        In dry run pipeline_id is None, so the map carries sentinel Nones and
        is used only for membership tests.
        """
        from app.models import PipelineStage
        from app.extensions import db

        existing = {}
        if pipeline_id is not None:
            existing = {
                s.display_name.strip().lower(): s.id
                for s in PipelineStage.query.filter_by(pipeline_id=pipeline_id).all()
            }

        stage_map = dict(existing)
        for key, display, category, order, is_entry, is_terminal in _stage_rows():
            lookup = display.strip().lower()
            if lookup in existing:
                report.stages_reused += 1
                continue
            report.stages_created += 1
            if self.dry_run:
                stage_map[lookup] = None
                continue
            stage = PipelineStage(
                pipeline_id=pipeline_id,
                internal_key=key,
                display_name=display,
                stage_category=category,
                order_index=order,
                is_entry=is_entry,
                is_terminal=is_terminal,
                is_active=True,
            )
            db.session.add(stage)
            db.session.flush()
            stage_map[lookup] = stage.id

        if not self.dry_run:
            db.session.commit()
        return stage_map

    # ── Step 3 — link leads ───────────────────────────────────────────────
    def _step3_link(self, tenant_id, stage_map, report):
        """Set sales_stage_id from the row's existing lead_status.

        Reads _lead_status (the raw legacy column) rather than the adapter:
        the adapter would return the linked stage name once a row is linked,
        which would make re-runs read their own output instead of the source.

        Never writes lead_status. Clearing sales_stage_id is therefore a
        complete rollback.
        """
        from app.models import ConversationState
        from app.extensions import db

        offset = 0
        while True:
            batch = (ConversationState.query
                     .filter(ConversationState.tenant_id == tenant_id)
                     .order_by(ConversationState.id)
                     .limit(self.batch_size).offset(offset).all())
            if not batch:
                break
            offset += len(batch)

            for lead in batch:
                if lead.sales_stage_id is not None:
                    report.leads_already += 1
                    continue

                raw = (lead._lead_status or "").strip()
                if not raw:
                    report.leads_unlinkable += 1
                    continue

                stage_id = stage_map.get(raw.lower())
                if stage_id is None and raw.lower() not in stage_map:
                    # Out-of-vocabulary (e.g. the two known 'fresh' rows).
                    # Left NULL so the adapter falls back to the legacy string
                    # — the lead stays editable and correct, just not yet
                    # represented in the pipeline.
                    report.leads_unlinkable += 1
                    if raw not in report.unlinkable_values:
                        report.unlinkable_values.append(raw)
                    continue

                report.leads_linked += 1
                if self.dry_run:
                    continue
                lead.sales_stage_id = stage_id

            if not self.dry_run:
                db.session.commit()
            report.batches += 1
