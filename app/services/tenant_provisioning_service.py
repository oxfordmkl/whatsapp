"""Phase 14D — tenant provisioning.

Makes a newly registered tenant OPERATIONAL. Registration previously created a
Tenant row and an ADMIN User and nothing else, which left the tenant unable to
use the product it had just signed up for:

  * no sales pipeline  -> _sync_sales_stage_link() found no stage to resolve,
    so every lead entered with sales_stage_id NULL, the Sales Pipeline
    dashboard was permanently empty and the whole Phase 10.6-10.9 feature set
    was inert for that tenant
  * no TenantSettings  -> the model's own docstring claims the row is "created
    on tenant registration", but nothing ever instantiated it: production held
    ZERO rows across 10 tenants

The 10 existing tenants only have pipelines because SalesPipelineSeeder was run
by hand. That is the gap this closes.

WHY NOT REUSE SalesPipelineSeeder
---------------------------------
It commits internally, three times per tenant, by design: it is a batch
backfill built to be interrupt-safe across thousands of leads. Registration
needs the opposite guarantee — ONE transaction that rolls back completely if
any part fails, so a half-provisioned tenant can never exist.

So this module never commits. The caller owns the transaction. Stage
definitions are imported from the seeder rather than restated, so the two
cannot drift.

IDEMPOTENT
----------
Every step checks before it creates. Running twice creates nothing the second
time and never duplicates, which is what makes the backfill safe to re-run and
makes a retried registration harmless.

NEVER MODIFIES EXISTING DATA. It only adds what is missing; it does not update,
reorder or reconcile anything a tenant already has.
"""
import json
import logging

logger = logging.getLogger(__name__)

# Defaults applied to a brand-new tenant's settings blob. Deliberately
# conservative: nothing here turns on a feature that costs money or sends a
# message without the operator choosing it.
DEFAULT_SETTINGS = {
    "branding": {
        "primary_color": "#2563eb",
        "logo_url": None,
    },
    "locale": {
        "language": "en",
        "timezone": "Asia/Kolkata",
        "currency": "INR",
    },
    "working_hours": {
        "monday":    ["09:00", "18:00"],
        "tuesday":   ["09:00", "18:00"],
        "wednesday": ["09:00", "18:00"],
        "thursday":  ["09:00", "18:00"],
        "friday":    ["09:00", "18:00"],
        "saturday":  ["09:00", "14:00"],
        "sunday":    None,
    },
    "ai": {
        # Persona name None = fall back to the system default persona. A tenant
        # that has not chosen a name must not be given someone else's.
        "persona_name": None,
        "enabled": True,
        "auto_reply": True,
        # Custom prompt override stays empty: the tenant inherits the shared
        # prompt until it deliberately writes its own.
        "prompt_override": None,
    },
    "notifications": {
        "stage_change": True,
        "lead_assigned": True,
        "task_due": True,
        # Campaign completion is off by default — it is a bulk event and a new
        # tenant has no basis yet for wanting it.
        "campaign_complete": False,
        "digest_frequency": "daily",
    },
    "features": {
        "enable_ai_booking": True,
        "enable_google_sheets": False,
        "enable_campaigns": True,
    },
}


class ProvisioningReport:
    """What provisioning did. Every field is a count so a backfill across many
    tenants can be summarised without re-querying."""

    def __init__(self, tenant_id):
        self.tenant_id = tenant_id
        self.pipeline_created = 0
        self.pipeline_reused = 0
        self.stages_created = 0
        self.stages_reused = 0
        self.settings_created = 0
        self.settings_reused = 0
        self.errors = []

    @property
    def changed(self):
        return bool(self.pipeline_created or self.stages_created
                    or self.settings_created)

    def as_dict(self):
        return {
            "tenant_id": self.tenant_id,
            "pipeline_created": self.pipeline_created,
            "pipeline_reused": self.pipeline_reused,
            "stages_created": self.stages_created,
            "stages_reused": self.stages_reused,
            "settings_created": self.settings_created,
            "settings_reused": self.settings_reused,
            "changed": self.changed,
            "errors": self.errors,
        }

    def __repr__(self):
        return (f"<ProvisioningReport {self.tenant_id} "
                f"pipeline+{self.pipeline_created} stages+{self.stages_created} "
                f"settings+{self.settings_created}>")


def _provision_sales_pipeline(tenant_id, report):
    """Create the tenant's sales PipelineDefinition if absent. Returns its id.

    Mirrors SalesPipelineSeeder._step1_pipeline, minus the commit. is_default
    is deliberately NOT set: the AI funnel's pipeline may already hold it, and
    silently moving a tenant's default is a behavioural change provisioning has
    no mandate to make.
    """
    from app.models import PipelineDefinition, SALES_PIPELINE_KEY
    from app.extensions import db

    existing = (PipelineDefinition.query
                .filter_by(tenant_id=tenant_id, internal_key=SALES_PIPELINE_KEY)
                .first())
    if existing is not None:
        report.pipeline_reused += 1
        return existing.id

    pipeline = PipelineDefinition(
        tenant_id=tenant_id,
        internal_key=SALES_PIPELINE_KEY,
        name="Sales Pipeline",
        description="Operator-managed sales stages. Distinct from the AI "
                    "conversation funnel.",
        is_default=False,
        is_active=True,
    )
    db.session.add(pipeline)
    db.session.flush()          # assigns pipeline.id without committing
    report.pipeline_created += 1
    return pipeline.id


def _provision_stages(pipeline_id, report):
    """Create any missing stage rows for the pipeline.

    Stage specs come from sales_pipeline_seed._stage_rows(), which derives them
    from LEAD_STATUSES / LEAD_TERMINAL_STATUSES. Importing rather than
    restating them means a new tenant and a backfilled one can never be given
    different pipelines.
    """
    from app.models import PipelineStage
    from app.extensions import db
    from app.services.sales_pipeline_seed import _stage_rows

    existing = {
        (s.display_name or "").strip().lower()
        for s in PipelineStage.query.filter_by(pipeline_id=pipeline_id).all()
    }

    for key, display, category, order, is_entry, is_terminal in _stage_rows():
        if display.strip().lower() in existing:
            report.stages_reused += 1
            continue
        db.session.add(PipelineStage(
            pipeline_id=pipeline_id,
            internal_key=key,
            display_name=display,
            stage_category=category,
            order_index=order,
            is_entry=is_entry,
            is_terminal=is_terminal,
            is_active=True,
        ))
        report.stages_created += 1

    db.session.flush()


def _provision_settings(tenant_id, report):
    """Create the tenant's TenantSettings row if absent.

    An EXISTING row is left completely untouched — not merged, not topped up
    with new default keys. A tenant that has customised its settings must not
    have provisioning silently reintroduce a default it deliberately changed.
    Callers reading the blob already tolerate missing keys (json.loads of '{}'
    is the documented access pattern).
    """
    from app.models import TenantSettings
    from app.extensions import db

    existing = TenantSettings.query.filter_by(tenant_id=tenant_id).first()
    if existing is not None:
        report.settings_reused += 1
        return existing.id

    row = TenantSettings(
        tenant_id=tenant_id,
        settings=json.dumps(DEFAULT_SETTINGS),
    )
    db.session.add(row)
    db.session.flush()
    report.settings_created += 1
    return row.id


def provision_tenant(tenant_id, commit=False):
    """Provision everything a tenant needs to be operational.

    DOES NOT COMMIT by default. The caller owns the transaction, which is what
    lets registration be a single atomic unit: if provisioning raises, the
    caller's rollback removes the Tenant and User too, so a half-provisioned
    tenant cannot exist.

    Pass commit=True only from a standalone context such as the backfill CLI,
    where each tenant is its own unit of work.

    RAISES on failure — deliberately. A registration that cannot provision must
    fail loudly and roll back, not hand the operator an unusable account that
    looks fine until they open the pipeline.

    Idempotent: safe to call repeatedly.
    """
    from app.extensions import db

    if not tenant_id:
        raise ValueError("provision_tenant requires a tenant_id")

    report = ProvisioningReport(tenant_id)

    pipeline_id = _provision_sales_pipeline(tenant_id, report)
    _provision_stages(pipeline_id, report)
    _provision_settings(tenant_id, report)

    if commit:
        db.session.commit()

    logger.info("Tenant provisioning %s: %s", tenant_id, report.as_dict())
    return report


def is_provisioned(tenant_id):
    """True when the tenant has a sales pipeline with stages AND a settings row.

    Used by the backfill to report before/after without mutating anything.
    """
    from app.models import (PipelineDefinition, PipelineStage, TenantSettings,
                            SALES_PIPELINE_KEY)
    from app.services.sales_pipeline_seed import _stage_rows

    pipeline = (PipelineDefinition.query
                .filter_by(tenant_id=tenant_id, internal_key=SALES_PIPELINE_KEY)
                .first())
    if pipeline is None:
        return False
    stage_count = PipelineStage.query.filter_by(pipeline_id=pipeline.id).count()
    if stage_count < len(_stage_rows()):
        return False
    return TenantSettings.query.filter_by(tenant_id=tenant_id).first() is not None


def backfill_all_tenants(dry_run=True):
    """Provision every tenant that is missing anything. Returns [report].

    dry_run=True (the default) reports what WOULD change and writes nothing —
    the same safety posture as SalesPipelineSeeder, and for the same reason:
    the first run of anything that touches every tenant should be observable
    before it is permanent.

    Each tenant is its own transaction, so one failure cannot roll back another
    tenant's successful provisioning, and a partial run leaves every completed
    tenant correct.
    """
    from app.models import Tenant
    from app.extensions import db

    reports = []
    for tenant in Tenant.query.order_by(Tenant.created_at).all():
        report = ProvisioningReport(tenant.id)
        try:
            if dry_run:
                # Inspect only. Nothing is added to the session.
                from app.models import (PipelineDefinition, PipelineStage,
                                        TenantSettings, SALES_PIPELINE_KEY)
                from app.services.sales_pipeline_seed import _stage_rows
                pipeline = (PipelineDefinition.query
                            .filter_by(tenant_id=tenant.id,
                                       internal_key=SALES_PIPELINE_KEY).first())
                if pipeline is None:
                    report.pipeline_created = 1
                    report.stages_created = len(_stage_rows())
                else:
                    report.pipeline_reused = 1
                    have = PipelineStage.query.filter_by(
                        pipeline_id=pipeline.id).count()
                    report.stages_reused = have
                    report.stages_created = max(0, len(_stage_rows()) - have)
                if TenantSettings.query.filter_by(tenant_id=tenant.id).first():
                    report.settings_reused = 1
                else:
                    report.settings_created = 1
            else:
                report = provision_tenant(tenant.id, commit=True)
        except Exception as exc:
            db.session.rollback()
            report.errors.append(str(exc))
            logger.exception("Provisioning failed for tenant %s", tenant.id)
        reports.append(report)
    return reports
