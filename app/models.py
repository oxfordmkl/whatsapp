from datetime import datetime
from app.extensions import db
from flask_login import UserMixin
from sqlalchemy import case, select, func
from sqlalchemy.ext.hybrid import hybrid_property
import uuid

class Tenant(db.Model):
    """
    Phase 12: Multi-Tenant Root Architecture
    Phase 13-A2B: SaaS Identity Schema Expansion
    """
    __tablename__ = 'tenants'

    # ── Core Identity ──────────────────────────────────────────────────────
    id         = db.Column(db.String(36), primary_key=True, default=lambda: uuid.uuid4().hex)
    name       = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # ── Phase 13-A2B: SaaS Identity Fields ────────────────────────────────
    # URL-friendly identifier for tenant routing (/t/<slug>). Immutable after creation.
    slug       = db.Column(db.String(30), nullable=False, unique=True)

    # Lifecycle state. Controls access to WhatsApp, AI, and campaigns at middleware.
    # Values: TRIAL | ACTIVE | PAST_DUE | SUSPENDED | CANCELLED | DELETED
    status     = db.Column(db.String(20), nullable=False, default='ACTIVE')

    # Subscription tier. Controls plan limits enforced in application config.
    # Values: STARTER | GROWTH | PROFESSIONAL | ENTERPRISE
    plan       = db.Column(db.String(20), nullable=False, default='ENTERPRISE')

    # Trial expiration timestamp. NULL = no active trial (already paid or grandfathered).
    trial_ends_at = db.Column(db.DateTime, nullable=True)

    # Billing contact email for invoices and plan-change notifications.
    billing_email = db.Column(db.String(100), nullable=True)

    # Industry vertical — used for default AI prompts and pipeline templates.
    # Values: Education | Healthcare | Real Estate | Insurance | Retail | Custom
    industry   = db.Column(db.String(50), nullable=False, default='Education')

    # ── Phase 13-A2B: Per-Tenant WhatsApp WABA Credentials ────────────────
    # Meta Phone Number ID for inbound webhook routing and outbound API calls.
    # NULL until the Tenant Admin completes WABA onboarding (Phase 13-B).
    waba_phone_number_id       = db.Column(db.String(50), nullable=True)

    # Meta Access Token stored encrypted at rest (Fernet encryption, Phase 13-B).
    # NULL until WABA onboarding completes.
    waba_access_token_encrypted = db.Column(db.Text, nullable=True)

    # ── Phase 13-A2B: Per-Tenant AI Persona Fields ────────────────────────
    # Bot display name shown to leads (e.g., "Oxford Nova", "Priya", "Rahul").
    # NULL = system default persona.
    ai_persona_name    = db.Column(db.String(50), nullable=True)

    # Full custom system prompt override for this tenant's AI bot.
    # NULL = system default AALIZA_PROMPT from app/bot/prompts.py.
    ai_prompt_override = db.Column(db.Text, nullable=True)

    # ── Phase 13-B4.1: Provider-Agnostic SaaS Billing ─────────────────────
    billing_provider            = db.Column(db.String(20), nullable=True) # e.g. 'stripe', 'razorpay'
    billing_customer_id         = db.Column(db.String(100), nullable=True)
    billing_subscription_id     = db.Column(db.String(100), nullable=True, unique=True)
    billing_subscription_status = db.Column(db.String(50), nullable=True)
    current_period_end          = db.Column(db.DateTime, nullable=True)
    past_due_at                 = db.Column(db.DateTime, nullable=True)
    billing_exempt              = db.Column(db.Boolean, default=False, nullable=False)
    currency                    = db.Column(db.String(3), default='USD', nullable=False)

    # ── Audit ──────────────────────────────────────────────────────────────
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class User(UserMixin, db.Model):
    """
    Phase 10: Authenticated CRM users (Admin/Staff).
    Phase 13-A2B: Added email field; username uniqueness now scoped per-tenant.
    """
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    # username uniqueness is now enforced per-tenant via __table_args__ composite constraint.
    # The unique=True / index=True on this column definition will be removed in the Alembic
    # migration (Step 6+7). The model reflects the TARGET state post-migration.
    username = db.Column(db.String(64), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='STAFF')
    is_active = db.Column(db.Boolean, default=True)
    require_password_change = db.Column(db.Boolean, default=False)
    tenant_id = db.Column(db.String(36), db.ForeignKey('tenants.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)

    # ── Phase 13-A2B: Email for Tenant Admin login and password reset ──────
    # nullable=True: Staff may not have emails. Tenant Admins must supply email at registration.
    email = db.Column(db.String(120), nullable=True, unique=True)
    
    # ── Phase 15C.5-B: Email Verification ──────
    email_verified_at = db.Column(db.DateTime, nullable=True, index=True)

    __table_args__ = (
        # Phase 13-A2B: Composite uniqueness — username is unique WITHIN a tenant,
        # not globally. Allows multiple tenants to each have a user named "admin", "kiran", etc.
        db.UniqueConstraint('tenant_id', 'username', name='uq_users_tenant_username'),
    )

class ConversationState(db.Model):
    """
    Persistent replacement for the old conversation_state dict.
    One row per WhatsApp phone number.
    """
    __tablename__ = "conversation_state"

    id           = db.Column(db.Integer, primary_key=True)
    phone        = db.Column(db.String(20),  nullable=False, index=True)
    name         = db.Column(db.String(200), default="")

    # ══ Phase 16.5A5-I: Enterprise ORM dual-write adapters ══════════════════
    # Phase 16.5A5-J CORRECTION: `is_admitted` is NO LONGER an adapter. It is an
    # independent business attribute and is declared as a plain column in the
    # Phase 4A block below. See ADR-018 (Business Conversion Independence).
    #
    # The FOUR attributes below (stage, course, batch_time, offer_course) are
    # hybrid_property ADAPTERS defined further down. Their PHYSICAL columns are
    # retained here under underscore-prefixed names mapped to the SAME database
    # column names — NO migration, NO data change.
    #
    #   read : prefer the new relational model when the row is relationally
    #          activated (pipeline_stage_id IS NOT NULL, set by Phase 16.5A6
    #          backfill); otherwise fall back to the legacy column.
    #   write: ALWAYS update the legacy column, AND keep an EXISTING relational
    #          link in sync. Creating a brand-new link is backfill (16.5A6) and
    #          is intentionally NOT done here.
    #
    # Because every production row currently has pipeline_stage_id = NULL, both
    # the Python getters and the SQL expressions reduce EXACTLY to the legacy
    # columns — zero behaviour and zero extra queries until backfill runs.
    #
    # `stage` COMPATIBILITY CONTRACT (ADR-019): app/bot/router.py dispatches on
    # exact legacy stage strings (e.g. stage in ("new","done","enrolled",
    # "goal_selection")). Any PipelineStage this row links to MUST carry an
    # internal_key byte-identical to the legacy value, or the router state
    # machine breaks. See ADR-019 (Compatibility Pipeline Standard).
    _stage        = db.Column('stage',        db.String(50),  default="new")
    _course       = db.Column('course',       db.String(200), default="")
    _batch_time   = db.Column('batch_time',   db.String(100), default="")
    _offer_course = db.Column('offer_course', db.String(50),  default="")

    goal         = db.Column(db.String(50),  default="")
    last_msg     = db.Column(db.String(50),  default="")
    last_text    = db.Column(db.Text,        default="")
    updated_at   = db.Column(db.DateTime,    default=datetime.utcnow,
                             onupdate=datetime.utcnow, nullable=False)

    # ── Phase 4A: CRM Expansion Fields ──
    created_at     = db.Column(db.DateTime,    default=datetime.utcnow, nullable=False)
    # lead_status: RETAINED as a legacy column (Phase 16.5A5-I keeps it intact).
    #   Migration to TagDefinition is DEFERRED — no adapter is applied yet, only a
    #   future hook. group_by(lead_status) analytics in admin.py depend on this
    #   physical column and MUST keep working unchanged.
    lead_status    = db.Column(db.String(50),  nullable=True, default="Lead")
    assigned_staff = db.Column(db.String(100), nullable=True)
    lead_score     = db.Column(db.Integer,     nullable=True, default=0)

    # is_admitted: INDEPENDENT business attribute — NOT derived from the pipeline.
    #   ADR-018 (Business Conversion Independence). Repository evidence: `stage` is
    #   written exclusively by the AI router (app/bot/router.py, app/bot/objections.py)
    #   and `is_admitted` exclusively by the staff form (app/routes/admin.py). No code
    #   path couples them, so they legitimately disagree (e.g. stage="new" +
    #   is_admitted=True when staff admit a lead the bot never advanced). A single
    #   pipeline_stage_id FK cannot reproduce both independent values, therefore
    #   is_admitted is NEVER derived from PipelineStage.stage_category.
    is_admitted    = db.Column(db.Boolean,     nullable=True, default=False)

    notes          = db.Column(db.Text,        nullable=True)

    # ── Phase 11-D1: Opt-Out Safety ──
    is_opted_out   = db.Column(db.Boolean,     nullable=True, default=False)

    # ── Phase 12-B: SaaS Architecture ──
    tenant_id      = db.Column(db.String(36), db.ForeignKey('tenants.id'), nullable=False, index=True)

    # ── Phase 16.5A5: Enterprise ORM Adapter Foundation ──
    # Nullable FK into the new relational pipeline model. NULL until Phase 16.5A6
    # backfill maps legacy `stage` strings to PipelineStage rows.
    pipeline_stage_id = db.Column(db.Integer, db.ForeignKey('pipeline_stages.id'),
                                  nullable=True, index=True)

    # JSON blob for legacy attributes without a dedicated relational home
    # (offer_course, batch_time). db.JSON: TEXT on SQLite, JSON on PostgreSQL.
    # Enterprise Baseline v1.1 — upgraded from db.Text per ADR-013.
    custom_attributes = db.Column(db.JSON, nullable=True)

    __table_args__ = (
        db.UniqueConstraint('phone', 'tenant_id', name='uq_conversation_state_phone_tenant'),
    )

    # ── Adapter constructor bridge ──────────────────────────────────────────
    # ConversationState(stage="new", course="", ...) must keep working now that
    # those names are hybrid_property adapters (not accepted by the default
    # SQLAlchemy __init__). Route adapter kwargs through the hybrid setters.
    # `is_admitted` is absent by design (ADR-018) — it is a plain column and is
    # handled natively by SQLAlchemy's __init__.
    _ADAPTER_INIT_KEYS = ('stage', 'course', 'batch_time', 'offer_course')

    def __init__(self, **kwargs):
        adapter_vals = {k: kwargs.pop(k) for k in self._ADAPTER_INIT_KEYS if k in kwargs}
        super().__init__(**kwargs)
        for k, v in adapter_vals.items():
            setattr(self, k, v)

    # ── Adapter helpers (instance-level; honour the no-relationship convention)
    def _lookup_pipeline_stage(self):
        """Load the linked PipelineStage, or None. Only called when the row is
        relationally activated, so it never queries on un-backfilled data."""
        if self.pipeline_stage_id is None:
            return None
        from app.models import PipelineStage
        return db.session.get(PipelineStage, self.pipeline_stage_id)

    def _first_offering(self):
        """First linked Offering via the bridge, or None. Guarded by the caller
        on pipeline_stage_id, so no query runs pre-backfill."""
        if self.id is None:
            return None
        from app.models import ConversationOffering, Offering
        link = (ConversationOffering.query
                .filter_by(conversation_state_id=self.id)
                .order_by(ConversationOffering.id)
                .first())
        if link is None:
            return None
        return db.session.get(Offering, link.offering_id)

    def _set_custom_attr(self, key, value):
        """Write/clear a key in the custom_attributes JSON blob. Empty values are
        removed so new leads keep custom_attributes = NULL until real data lands."""
        attrs = dict(self.custom_attributes or {})
        if value in (None, ""):
            attrs.pop(key, None)
        else:
            attrs[key] = value
        self.custom_attributes = attrs or None

    def _sync_stage_link(self, value):
        """Keep an EXISTING pipeline link in sync when the legacy stage changes.
        No-op until the row is relationally activated (Phase 16.5A6). Never
        creates a link (that is backfill)."""
        if self.pipeline_stage_id is None:
            return
        from app.models import PipelineStage
        current = db.session.get(PipelineStage, self.pipeline_stage_id)
        if current is None:
            return
        match = (PipelineStage.query
                 .filter_by(pipeline_id=current.pipeline_id, internal_key=value)
                 .first())
        if match is not None:
            self.pipeline_stage_id = match.id

    def _sync_offering_link(self, value):
        """Keep the course→Offering bridge in sync when the legacy course changes.

        Symmetric with _sync_stage_link (ADR-020): a no-op until the row is
        relationally activated, then the relational link is kept tracking the
        legacy value so `course` round-trips.

        Deliberate divergence from _sync_stage_link: when no Offering matches,
        this REMOVES the stale bridge rather than leaving it. _sync_stage_link can
        leave a stale link because every stage the router writes is guaranteed to
        be seeded. `course` has no such guarantee — the bot can assign any of the
        10 ALL_COURSES entries, while only courses actually present in production
        received an Offering — so keeping the link would return a stale course.
        Removing it makes _first_offering() return None and the getter fall back to
        _course, which is always correct (the Fail-Safe Property).

        Never creates an Offering: minting enterprise rows is backfill's job
        (Phase 16.5A6). Offerings are only ever reused, matched on the EXACT name
        within this tenant — no normalization, no case-folding (ADR-019).
        """
        if self.pipeline_stage_id is None:
            return          # gate closed — the getter reads _course directly
        if self.id is None:
            return          # not yet persisted — no bridge can exist to go stale

        from app.models import ConversationOffering, Offering

        links = (ConversationOffering.query
                 .filter_by(conversation_state_id=self.id)
                 .order_by(ConversationOffering.id)
                 .all())

        target_id = None
        if value not in (None, ""):
            offering = (Offering.query
                        .filter_by(tenant_id=self.tenant_id, name=value)
                        .first())
            if offering is not None:
                target_id = offering.id

        # Drop every bridge that is not the target, keeping at most one. When
        # target_id is None (empty course, or no Offering for this name) they are
        # all cleared and the getter falls back to _course.
        keep = None
        for link in links:
            if (target_id is not None and link.offering_id == target_id
                    and keep is None):
                keep = link
                continue
            db.session.delete(link)

        if target_id is not None and keep is None:
            db.session.add(ConversationOffering(
                conversation_state_id=self.id, offering_id=target_id))

    # ── stage adapter ───────────────────────────────────────────────────────
    @hybrid_property
    def stage(self):
        if self.pipeline_stage_id is not None:
            ps = self._lookup_pipeline_stage()
            if ps is not None:
                return ps.internal_key
        return self._stage

    @stage.setter
    def stage(self, value):
        self._stage = value
        self._sync_stage_link(value)

    @stage.expression
    def stage(cls):
        from app.models import PipelineStage
        return case(
            (cls.pipeline_stage_id.is_(None), cls._stage),
            else_=select(PipelineStage.internal_key)
                  .where(PipelineStage.id == cls.pipeline_stage_id)
                  .scalar_subquery()
        )

    # ── course adapter ──────────────────────────────────────────────────────
    @hybrid_property
    def course(self):
        if self.pipeline_stage_id is not None:
            off = self._first_offering()
            if off is not None:
                return off.name
        return self._course

    @course.setter
    def course(self, value):
        self._course = value
        self._sync_offering_link(value)

    @course.expression
    def course(cls):
        from app.models import Offering, ConversationOffering
        first_off = (select(Offering.name)
                     .where(Offering.id == ConversationOffering.offering_id)
                     .where(ConversationOffering.conversation_state_id == cls.id)
                     .order_by(ConversationOffering.id)
                     .limit(1)
                     .scalar_subquery())
        return case(
            (cls.pipeline_stage_id.is_(None), cls._course),
            else_=func.coalesce(first_off, cls._course)
        )

    # ── offer_course adapter (legacy column + custom_attributes JSON) ───────
    @hybrid_property
    def offer_course(self):
        attrs = self.custom_attributes or {}
        val = attrs.get('offer_course')
        if val not in (None, ""):
            return val
        return self._offer_course

    @offer_course.setter
    def offer_course(self, value):
        self._offer_course = value
        self._set_custom_attr('offer_course', value)

    @offer_course.expression
    def offer_course(cls):
        # No SQL predicate (WHERE/GROUP BY) uses offer_course — the legacy column
        # is the SQL source of truth; JSON preference is instance-level only.
        return cls._offer_course

    # ── batch_time adapter (legacy column + custom_attributes JSON) ─────────
    @hybrid_property
    def batch_time(self):
        attrs = self.custom_attributes or {}
        val = attrs.get('batch_time')
        if val not in (None, ""):
            return val
        return self._batch_time

    @batch_time.setter
    def batch_time(self, value):
        self._batch_time = value
        self._set_custom_attr('batch_time', value)

    @batch_time.expression
    def batch_time(cls):
        return cls._batch_time

    def to_dict(self) -> dict:
        return {
            "name":         self.name,
            "stage":        self.stage,
            "course":       self.course,
            "goal":         self.goal,
            "batch_time":   self.batch_time,
            "offer_course": self.offer_course,
            "last_msg":     self.last_msg,
            "last_text":    self.last_text,
            "lead_status":  self.lead_status,
            "assigned_staff": self.assigned_staff,
            "lead_score":   self.lead_score,
            "is_admitted":  self.is_admitted,
            "notes":        self.notes,
        }


class FollowUpJob(db.Model):
    """
    Persistent replacement for the old follow_up_queue list.
    One row per scheduled follow-up message.
    """
    __tablename__ = "follow_up_jobs"

    id         = db.Column(db.Integer,  primary_key=True)
    phone      = db.Column(db.String(20),  nullable=False, index=True)
    name       = db.Column(db.String(200), default="")
    send_at    = db.Column(db.DateTime,    nullable=False)
    message    = db.Column(db.Text,        nullable=False)
    day        = db.Column(db.Integer,     nullable=False)
    done       = db.Column(db.Boolean,     default=False, index=True)
    created_at = db.Column(db.DateTime,    default=datetime.utcnow)
    
    # Phase 12-B: SaaS Architecture
    tenant_id  = db.Column(db.String(36), db.ForeignKey('tenants.id'), nullable=False, index=True)
    
    # Phase 11-D2C: Retry Metadata
    retry_count     = db.Column(db.Integer,  default=0)
    last_attempt_at = db.Column(db.DateTime, nullable=True)
    failure_reason  = db.Column(db.Text,     nullable=True)


class PendingMessage(db.Model):
    """
    Phase 11-D3B2: Automation Interceptor Fallback Queue
    Stores outbound automated messages (Campaigns/Followups) that were intercepted
    because the 24-hour Meta window closed. Delivered instantly when the user replies.
    """
    __tablename__ = "pending_messages"

    id         = db.Column(db.Integer,    primary_key=True)
    phone      = db.Column(db.String(20), nullable=False, index=True)
    text       = db.Column(db.Text,       nullable=False)
    created_at = db.Column(db.DateTime,   default=datetime.utcnow)
    tenant_id  = db.Column(db.String(36), db.ForeignKey('tenants.id'), nullable=False, index=True)


class MessageLog(db.Model):
    """
    Immutable append-only log of every inbound and outbound message.
    One row per message event. No foreign key constraints.
    Phase 4D.
    """
    __tablename__ = "message_log"

    id           = db.Column(db.Integer,    primary_key=True)
    phone        = db.Column(db.String(20), nullable=False, index=True)
    direction    = db.Column(db.String(10), nullable=False)   # "inbound" | "outbound"
    message_type = db.Column(db.String(20), nullable=False)   # "user" | "ai" | "followup" | "manual"
    message_text = db.Column(db.Text,       nullable=True)
    meta_json    = db.Column(db.Text,       nullable=True)    # optional JSON string
    created_at   = db.Column(db.DateTime,   nullable=False, default=datetime.utcnow)
    tenant_id    = db.Column(db.String(36), db.ForeignKey('tenants.id'), nullable=False, index=True)

    __table_args__ = (
        db.Index("idx_msg_phone_created", "phone", "created_at"),
    )


class ConversationMessage(db.Model):
    """
    Structured per-message CRM timeline.
    Intentionally separate from MessageLog:
      - MessageLog          → lightweight raw technical event log (Phase 4D)
      - ConversationMessage → structured, CRM-renderable history  (Phase 5A)
    Richer fields: wa_message_id (deduplication), staff_name (audit trail).
    No foreign key constraints. UTC timestamps. All nullable except core fields.
    """
    __tablename__ = "conversation_message"

    id            = db.Column(db.Integer,     primary_key=True)
    phone         = db.Column(db.String(20),  nullable=False)
    direction     = db.Column(db.String(10),  nullable=False)
    # direction:    "incoming" | "outgoing"

    message       = db.Column(db.Text,        nullable=True)
    message_type  = db.Column(db.String(20),  nullable=True)
    # message_type: "text" | "interactive" | "button" | "template" | "system"

    source        = db.Column(db.String(20),  nullable=True)
    # source:       "user" | "ai" | "manual" | "followup" | "system"

    staff_name    = db.Column(db.String(100), nullable=True)
    # Populated only for manual CRM sends — identifies sender for audit trail

    wa_message_id = db.Column(db.String(100), nullable=True)
    # WhatsApp message ID from API — for future deduplication

    created_at    = db.Column(db.DateTime,    nullable=False, default=datetime.utcnow)
    tenant_id     = db.Column(db.String(36),  db.ForeignKey('tenants.id'), nullable=False, index=True)

    __table_args__ = (
        db.Index("idx_conv_msg_phone_created", "phone", "created_at"),
        db.Index("idx_conv_msg_wa_id",         "wa_message_id"),
    )


class LeadEvent(db.Model):
    """
    Phase 6A: Named business events per lead.
    Append-only — rows are never updated after insert.
    Tracks high-signal sales-funnel moments:
      COURSE_VIEWED, FEES_REQUESTED, DEMO_REQUESTED, PLACEMENT_ASKED
    """
    __tablename__ = "lead_event"

    id         = db.Column(db.Integer,    primary_key=True)
    phone      = db.Column(db.String(20), nullable=False, index=True)
    event_type = db.Column(db.String(50), nullable=False)
    event_data = db.Column(db.Text,       nullable=True)   # optional context (e.g. course name)
    created_at = db.Column(db.DateTime,   nullable=False, default=datetime.utcnow)
    tenant_id  = db.Column(db.String(36), db.ForeignKey('tenants.id'), nullable=False, index=True)

    __table_args__ = (
        db.Index("idx_lead_event_phone_created", "phone", "created_at"),
    )

class BillingInvoice(db.Model):
    """
    Phase 13-B4.1: Subscription Billing Foundation.
    Immutable ledger for SaaS billing.
    """
    __tablename__ = 'billing_invoices'
    
    id                   = db.Column(db.Integer, primary_key=True)
    tenant_id            = db.Column(db.String(36), db.ForeignKey('tenants.id'), nullable=False, index=True)
    provider             = db.Column(db.String(20), nullable=False) # 'stripe' | 'razorpay'
    provider_invoice_id  = db.Column(db.String(100), unique=True, nullable=False)
    amount_paid          = db.Column(db.Integer, nullable=False) # In cents/paise
    tax_amount           = db.Column(db.Integer, default=0, nullable=False)
    currency             = db.Column(db.String(3), nullable=False)
    status               = db.Column(db.String(20), nullable=False) # 'paid', 'failed', 'open'
    hosted_invoice_url   = db.Column(db.String(500), nullable=True)
    billing_period_start = db.Column(db.DateTime, nullable=True)
    billing_period_end   = db.Column(db.DateTime, nullable=True)
    created_at           = db.Column(db.DateTime, default=datetime.utcnow)


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 16.5A3 — Enterprise Configuration Foundation
#
# These models implement the frozen data model defined in:
#   docs/04_DATABASE/OXFORD_CRM_ENTERPRISE_DATA_MODEL_FREEZE_v1.0.md
#
# Architecture review: Phase 16.5A3-R1 (approved before implementation).
#
# Strict rules enforced in this phase:
#   - ZERO changes to any existing model above this line.
#   - No Alembic migration in this file; migration is a separate phase (16.5A3-M).
#   - Every model is tenant-isolated via explicit tenant_id FK.
#   - internal_key fields are immutable slug identifiers (a-z, 0-9, _).
#   - JSON stored as db.Text (matches existing meta_json pattern; SQLite + Postgres safe).
#   - No db.relationship() — all cross-table lookups remain explicit queries.
#   - ConversationState integration deferred to Phase 16.5A4 (ORM adapter layer).
# ═══════════════════════════════════════════════════════════════════════════════


class TenantSettings(db.Model):
    """
    Phase 16.5A3: Global tenant preferences.
    One row per tenant. Created on tenant registration.
    Lifecycle is tied to the parent Tenant — deleted when the Tenant is deleted.

    settings (JSON text) top-level keys:
      branding:      { primary_color, logo_url }
      locale:        { language, timezone, currency }
      working_hours: { monday: ["09:00","18:00"], tuesday: [...], ... }
      features:      { enable_ai_booking: true, enable_google_sheets: true, ... }

    Access pattern: json.loads(ts.settings or '{}')
    Never query this field server-side (no JSONB operator usage).
    """
    __tablename__ = 'tenant_settings'

    id         = db.Column(db.Integer, primary_key=True)

    # ── Tenant Ownership (1:1) ─────────────────────────────────────────────
    # unique=True enforces 1:1 at the DB level — one settings row per tenant.
    tenant_id  = db.Column(db.String(36), db.ForeignKey('tenants.id'),
                           nullable=False, index=True, unique=True)

    # ── Settings Blob (JSON text, parsed in Python) ────────────────────────
    settings   = db.Column(db.Text, nullable=False, default='{}')
    # settings: JSON text. Parse with json.loads(ts.settings or '{}').

    # ── Audit ──────────────────────────────────────────────────────────────
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)


class PipelineDefinition(db.Model):
    """
    Phase 16.5A3: Named lifecycle funnel owned by a tenant.
    A tenant may define multiple pipelines (Sales, Support, Onboarding, etc.).
    Exactly one pipeline per tenant should carry is_default=True; this is
    enforced at the application layer, not the DB layer.

    Industry examples:
      Education : "Admissions Pipeline"  (Lead → Demo → Admitted)
      Ecommerce : "Sales Pipeline"       (Visitor → Cart → Purchased)
      Healthcare: "Patient Journey"      (Inquiry → Appointment → Treated)

    internal_key: Immutable slug (a-z, 0-9, _ only). Used in business logic and
                  automation rules. NEVER use display name in conditionals.
                  Unique per tenant enforced by composite unique constraint.

    ConversationState integration: Deferred to Phase 16.5A4 (adapter layer).
    Stages queried explicitly: PipelineStage.query.filter_by(pipeline_id=x).all()
    No db.relationship() — follows existing codebase convention.
    """
    __tablename__ = 'pipeline_definitions'

    id           = db.Column(db.Integer, primary_key=True)

    # ── Tenant Ownership ───────────────────────────────────────────────────
    tenant_id    = db.Column(db.String(36), db.ForeignKey('tenants.id'),
                             nullable=False, index=True)

    # ── Identity ───────────────────────────────────────────────────────────
    internal_key = db.Column(db.String(50),  nullable=False)
    # internal_key: Immutable slug. Example: "admissions_pipeline", "ecommerce_sales".

    name         = db.Column(db.String(100), nullable=False)
    # name: Human-readable display label. Never used in conditionals.

    description  = db.Column(db.Text, nullable=True)
    # description: Optional freeform description for Tenant Admin UI.

    # ── State ──────────────────────────────────────────────────────────────
    is_default   = db.Column(db.Boolean, nullable=False, default=False)
    # is_default: True for the primary pipeline shown in CRM widgets.
    #             Only one pipeline per tenant should be default (app-layer enforced).

    is_active    = db.Column(db.Boolean, nullable=False, default=True)

    # ── Audit ──────────────────────────────────────────────────────────────
    created_at   = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow,
                             onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'internal_key',
                            name='uq_pipeline_def_tenant_key'),
        # Note: index=True on tenant_id FK already generates ix_pipeline_definitions_tenant_id.
        # No standalone index needed here (duplicate index cleanup — Phase 16.5A3-M2).
    )


class PipelineStage(db.Model):
    """
    Phase 16.5A3: Ordered step within a PipelineDefinition.
    Each stage maps to the universal CRM concept of Open / Won / Lost via
    stage_category, enabling industry-agnostic KPI dashboards.

    stage_category values:
      'open'  — lead is actively being worked.
      'won'   — lead converted (admitted, purchased, booked, closed).
      'lost'  — lead dropped off (not interested, refunded, disqualified).

    is_entry: True for the default entry stage for NEW leads in this pipeline.
              Exactly one stage per pipeline should carry is_entry=True.
              Enforced at the application layer. Used by adapters to auto-assign
              ConversationState records when pipeline_stage_id is NULL.

    is_terminal: True for stages after which no automation rules should fire.
                 Both 'won' and 'lost' stages are typically terminal.
                 Explicit flag is safer than inferring from stage_category alone.

    Backward compatibility (Phase 16.5A5-J corrected):
      Legacy stage string → internal_key (EXACT match — see ADR-019)

      NOTE: The earlier mapping "legacy is_admitted=True → stage_category='won'"
      is DISPROVEN and REMOVED (ADR-018). `is_admitted` is written only by the
      staff form and `stage` only by the AI router; they are independent and
      legitimately disagree, so a single pipeline_stage_id FK cannot encode both.
      `is_admitted` is therefore NEVER derived from stage_category.
      stage_category remains valid for future relational KPIs, but it does not
      and must not drive is_admitted.

    internal_key: Immutable slug. For the first (Compatibility) pipeline these
      MUST be the exact legacy router stage strings — "new", "goal_selection",
      "course_recommendation", "course_viewed", "demo_time_ask", "demo_date_ask",
      "demo_booked", "offer_menu", "payment_pending", "enrolled", "not_sure",
      "done" — because app/bot/router.py dispatches on those literals (ADR-019).
      Renaming or normalizing them breaks the router state machine.
    """
    __tablename__ = 'pipeline_stages'

    id             = db.Column(db.Integer, primary_key=True)

    # ── Parent Pipeline ────────────────────────────────────────────────────
    pipeline_id    = db.Column(db.Integer, db.ForeignKey('pipeline_definitions.id'),
                               nullable=False, index=True)

    # ── Identity ───────────────────────────────────────────────────────────
    internal_key   = db.Column(db.String(50),  nullable=False)
    # internal_key: Immutable slug. Unique per pipeline (composite constraint below).

    display_name   = db.Column(db.String(100), nullable=False)
    # display_name: UI label. Never used in conditionals.

    # ── Category ───────────────────────────────────────────────────────────
    stage_category = db.Column(db.String(10), nullable=False, default='open')
    # stage_category: 'open' | 'won' | 'lost'

    # ── Ordering ───────────────────────────────────────────────────────────
    order_index    = db.Column(db.Integer, nullable=False, default=0)
    # order_index: Display and logical ordering within the pipeline. 0-based.

    # ── Behavioral Flags ───────────────────────────────────────────────────
    is_entry       = db.Column(db.Boolean, nullable=False, default=False)
    # is_entry: Exactly one per pipeline. Used to auto-assign new leads.

    is_terminal    = db.Column(db.Boolean, nullable=False, default=False)
    # is_terminal: No automation fires after this stage. Both won/lost are terminal.

    is_active      = db.Column(db.Boolean, nullable=False, default=True)

    # ── Audit ──────────────────────────────────────────────────────────────
    created_at     = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow,
                               onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('pipeline_id', 'internal_key',
                            name='uq_pipeline_stage_pipeline_key'),
        # Note: index=True on pipeline_id FK already generates ix_pipeline_stages_pipeline_id.
        # Keeping composite and category indexes; removing duplicate standalone FK index.
        db.Index('idx_pipeline_stage_active',   'pipeline_id', 'is_active'),
        db.Index('idx_pipeline_stage_category', 'stage_category'),
    )


class TagDefinition(db.Model):
    """
    Phase 16.5A3: Tenant-owned assignable label for leads / contacts.
    Tags enable multi-dimensional segmentation without modifying pipeline stages.

    category values: 'marketing' | 'sales' | 'support' | 'system'
    is_system: True for platform-seeded tags (e.g. 'OPT_OUT').
               Staff cannot delete system tags; Tenant Admin can deactivate.
    color_hex: 6-digit hex (#RRGGBB). Rendered as badge colour in CRM UI.

    M:M relationship with ConversationState via bridge table.
    Bridge table (ConversationTag) defined in Phase 16.5A4.
    """
    __tablename__ = 'tag_definitions'

    id           = db.Column(db.Integer, primary_key=True)

    # ── Tenant Ownership ───────────────────────────────────────────────────
    tenant_id    = db.Column(db.String(36), db.ForeignKey('tenants.id'),
                             nullable=False, index=True)

    # ── Identity ───────────────────────────────────────────────────────────
    internal_key = db.Column(db.String(50),  nullable=False)
    # internal_key: Immutable slug. Example: "vip", "payment_pending", "high_intent".

    display_name = db.Column(db.String(100), nullable=False)
    # display_name: UI label. Example: "VIP", "Payment Pending".

    category     = db.Column(db.String(20),  nullable=False, default='marketing')
    # category: 'marketing' | 'sales' | 'support' | 'system'

    color_hex    = db.Column(db.String(7),   nullable=False, default='#EEEEEE')
    # color_hex: #RRGGBB format. Default neutral grey.

    is_system    = db.Column(db.Boolean, nullable=False, default=False)
    # is_system: Platform-seeded tags. Staff cannot delete; Admin can deactivate.

    is_active    = db.Column(db.Boolean, nullable=False, default=True)

    # ── Audit ──────────────────────────────────────────────────────────────
    created_at   = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'internal_key',
                            name='uq_tag_def_tenant_key'),
        # Note: index=True on tenant_id FK already generates ix_tag_definitions_tenant_id.
        db.Index('idx_tag_def_category', 'tenant_id', 'category'),
    )


class MessageTemplate(db.Model):
    """
    Phase 16.5A3: Database cache of approved omnichannel message templates.
    Primary use: Meta WhatsApp Business Cloud API approved templates.
    Future use:  SMS, Email.

    Lifecycle states:
      'draft'            → Created by Tenant Admin; not yet submitted to provider.
      'approval_pending' → Submitted to Meta / SMS provider for review.
      'approved'         → Cleared for broadcast use by campaign_service.
      'rejected'         → Provider rejected; see rejection_reason for context.
      'archived'         → Deprecated; removed from UI selectors.

    Meta Cloud API specific fields (nullable for non-WhatsApp channels):
      category:             'MARKETING' | 'UTILITY' | 'AUTHENTICATION'
                            Meta billing tier — required before submission.
      header_type:          'TEXT' | 'IMAGE' | 'VIDEO' | 'DOCUMENT' | 'NONE'
      button_config:        JSON array of CTA / Quick Reply button definitions.
      provider_template_id: Meta WABA template name/ID. NULL until approval.
      rejection_reason:     Populated by Meta webhook callback on rejection.

    variables (JSON text array): Ordered list of placeholder names.
      Example: '["name", "course_name", "fee_amount"]'
      Used by Marketing Hub to render the variable-fill UI.

    UNIQUE constraint note: (tenant_id, provider_template_id) correctly allows
    multiple NULL provider_template_id rows (draft templates) because PostgreSQL
    treats each NULL as distinct — no false uniqueness violation on drafts.
    """
    __tablename__ = 'message_templates'

    id                   = db.Column(db.Integer, primary_key=True)

    # ── Tenant Ownership ───────────────────────────────────────────────────
    tenant_id            = db.Column(db.String(36), db.ForeignKey('tenants.id'),
                                     nullable=False, index=True)

    # ── Identity ───────────────────────────────────────────────────────────
    template_key         = db.Column(db.String(100), nullable=False)
    # template_key: Internal slug. Example: "welcome_education_ml", "cart_reminder_en".

    display_name         = db.Column(db.String(200), nullable=False)
    # display_name: Human label shown in Marketing Hub dropdowns.

    # ── Channel & Language ─────────────────────────────────────────────────
    channel              = db.Column(db.String(20), nullable=False, default='whatsapp')
    # channel: 'whatsapp' | 'sms' | 'email' | 'ai'

    language             = db.Column(db.String(10), nullable=False, default='en')
    # language: ISO 639-1. Example: 'en', 'ml', 'hi'.

    # ── Meta Cloud API Fields ──────────────────────────────────────────────
    category             = db.Column(db.String(20), nullable=True)
    # category: 'MARKETING' | 'UTILITY' | 'AUTHENTICATION'. NULL for non-WhatsApp.

    header_type          = db.Column(db.String(20), nullable=True, default='NONE')
    # header_type: 'TEXT' | 'IMAGE' | 'VIDEO' | 'DOCUMENT' | 'NONE'.

    provider_template_id = db.Column(db.String(200), nullable=True)
    # provider_template_id: Meta WABA template name/ID. NULL until approved.

    body_text            = db.Column(db.Text, nullable=True)
    # body_text: Preview copy of the approved template body. Not used for sending.

    button_config        = db.Column(db.Text, nullable=True, default='[]')
    # button_config: JSON array of button definitions (CTA / Quick Reply).
    # Example: '[{"type":"QUICK_REPLY","text":"Yes, interested"}]'

    variables            = db.Column(db.Text, nullable=False, default='[]')
    # variables: JSON array of placeholder names. Parse with json.loads().
    # Example: '["name", "course", "fee"]'

    rejection_reason     = db.Column(db.Text, nullable=True)
    # rejection_reason: Populated from Meta webhook callback on 'rejected' status.

    # ── Lifecycle ──────────────────────────────────────────────────────────
    status               = db.Column(db.String(20), nullable=False, default='draft')
    # status: 'draft' | 'approval_pending' | 'approved' | 'rejected' | 'archived'

    # ── Audit ──────────────────────────────────────────────────────────────
    created_at           = db.Column(db.DateTime, default=datetime.utcnow,
                                     nullable=False)
    updated_at           = db.Column(db.DateTime, default=datetime.utcnow,
                                     onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'provider_template_id',
                            name='uq_msg_template_tenant_provider'),
        # Note: index=True on tenant_id FK already generates ix_message_templates_tenant_id.
        db.Index('idx_msg_template_status',  'tenant_id', 'status'),
        db.Index('idx_msg_template_channel', 'tenant_id', 'channel'),
    )


class AudienceRule(db.Model):
    """
    Phase 16.5A3: Dynamic contact segmentation rule for a tenant.
    Defines a named audience that the Audience Engine (Phase 16.5A5) evaluates
    at runtime by translating rule_json into SQLAlchemy filters against
    ConversationState.

    rule_json (JSON text) — logic tree consumed ONLY by the Audience Engine:
      {
        "operator": "AND",
        "conditions": [
          {"field": "pipeline_stage_key", "op": "==",       "value": "demo"},
          {"field": "lead_score",         "op": ">=",       "value": 60},
          {"field": "tags",               "op": "contains", "value": "vip"}
        ]
      }
    Business logic MUST NOT parse rule_json outside the Audience Engine.

    estimated_count:    Cached audience size from last evaluation.
                        Displayed in Campaign Center UI ("HOT Leads (142)").
                        Prevents full-table scan on every Campaign Center page load.
                        NULL = not yet evaluated.

    last_evaluated_at:  Timestamp of last count evaluation.
                        UI can display staleness ("Evaluated 3 hours ago").
                        NULL = not yet evaluated.

    internal_key:       Optional slug for programmatic reference by AutomationRules.
                        NULL for ad-hoc / one-off audience rules.

    Replaces: Hardcoded segments in _calculate_audiences() — Phase 16.5A5.
    ConversationState integration: Deferred to Phase 16.5A5 (Audience Engine).
    """
    __tablename__ = 'audience_rules'

    id                = db.Column(db.Integer, primary_key=True)

    # ── Tenant Ownership ───────────────────────────────────────────────────
    tenant_id         = db.Column(db.String(36), db.ForeignKey('tenants.id'),
                                  nullable=False, index=True)

    # ── Identity ───────────────────────────────────────────────────────────
    name              = db.Column(db.String(200), nullable=False)
    # name: Human label for Campaign Center. Example: "HOT Leads", "Cart Abandoned".

    internal_key      = db.Column(db.String(100), nullable=True)
    # internal_key: Optional slug for automation references. NULL for ad-hoc rules.

    description       = db.Column(db.Text, nullable=True)
    # description: Freeform explanation for Tenant Admin UI.

    # ── Rule Logic ─────────────────────────────────────────────────────────
    rule_json         = db.Column(db.Text, nullable=False, default='{}')
    # rule_json: JSON logic tree. Parse and evaluate ONLY in the Audience Engine.

    # ── Cached Evaluation Metadata ─────────────────────────────────────────
    estimated_count   = db.Column(db.Integer, nullable=True)
    # estimated_count: Last evaluated audience size. NULL = not yet evaluated.

    last_evaluated_at = db.Column(db.DateTime, nullable=True)
    # last_evaluated_at: Timestamp of last count. NULL = not yet evaluated.

    # ── State ──────────────────────────────────────────────────────────────
    is_active         = db.Column(db.Boolean, nullable=False, default=True)

    # ── Audit ──────────────────────────────────────────────────────────────
    created_at        = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at        = db.Column(db.DateTime, default=datetime.utcnow,
                                  onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        # Note: index=True on tenant_id FK already generates ix_audience_rules_tenant_id.
        db.Index('idx_audience_rule_active', 'tenant_id', 'is_active'),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 16.5A5 — Enterprise ORM Adapter Foundation (Schema Expansion)
#
# Approved in the Phase 16.5A5-B Discovery Audit (implementation_plan).
# This block adds ONLY the schema expansion required before the dual-write ORM
# adapters can be written. There are intentionally NO @property / @hybrid_property
# setters here — those are a separate, later step within Phase 16.5A5, added only
# after this migration is applied and verified on Railway.
#
# Conventions (identical to the Phase 16.5A3 block above):
#   - JSON stored as db.Text (SQLite + Postgres safe) — NEVER JSONB.
#   - No db.relationship() — cross-table lookups remain explicit queries.
#   - Bridge tables derive tenant scope via conversation_state_id (mirrors
#     PipelineStage deriving tenant via pipeline_id); tenant_id is NOT
#     denormalized onto bridges.
#   - internal_key: immutable slug (a-z, 0-9, _), unique per tenant.
#
# Legacy → new target mapping (adapters implemented in a later step):
#   course        → Offering via conversation_state_offerings bridge
#   offer_course  → ConversationState.custom_attributes['offer_course']
#   batch_time    → ConversationState.custom_attributes['batch_time']
#   stage         → ConversationState.pipeline_stage_id → PipelineStage.display_name
#   is_admitted   → ConversationState.pipeline_stage_id → PipelineStage.stage_category=='won'
# ═══════════════════════════════════════════════════════════════════════════════


class Offering(db.Model):
    """
    Phase 16.5A5: Tenant-owned sellable / deliverable item.
    Generalizes the legacy ConversationState.course string into a first-class,
    industry-agnostic entity (course, product, service, plan, treatment, ...).

    internal_key: Immutable slug. Unique per tenant. Used in business logic and
                  automation rules — never use `name` in conditionals.
    M:M with ConversationState via the conversation_state_offerings bridge.
    No db.relationship() — lookups stay explicit (codebase convention).
    """
    __tablename__ = 'offering'

    id           = db.Column(db.Integer, primary_key=True)

    # ── Tenant Ownership ───────────────────────────────────────────────────
    tenant_id    = db.Column(db.String(36), db.ForeignKey('tenants.id'),
                             nullable=False, index=True)

    # ── Identity ───────────────────────────────────────────────────────────
    internal_key = db.Column(db.String(50),  nullable=False)
    # internal_key: Immutable slug. Example: "python_fullstack", "data_science".

    name         = db.Column(db.String(200), nullable=False)
    # name: Human-readable display label. Never used in conditionals.
    #   IDENTITY CONTRACT (ADR-019): ConversationState.course reads back
    #   Offering.name through the bridge. The Phase 16.5A6 backfill MUST store the
    #   EXACT legacy course string here — no normalization, no lowercasing, no
    #   slug-based deduplication — or the course value silently changes for rows
    #   whose raw strings differ only by case/spacing. Deduplicate on the exact
    #   (tenant_id, name) string; resolve internal_key collisions with a suffix.

    description  = db.Column(db.Text, nullable=True)
    # description: Optional freeform description for the Tenant Admin UI.

    # ── State ──────────────────────────────────────────────────────────────
    is_active    = db.Column(db.Boolean, nullable=False, default=True)

    # ── Pricing (Enterprise Baseline v1.1 / ADR-016) ───────────────────────
    # Nullable — not all verticals have list pricing. Per-tenant requiredness
    # enforced at application layer via TenantSettings.
    price        = db.Column(db.Numeric(12, 2), nullable=True)

    # ── Enterprise Extension (Enterprise Baseline v1.1 / ADR-013, ADR-014) ──
    # db.JSON: TEXT on SQLite, JSON on PostgreSQL. Name is `custom_attributes`
    # — `metadata` is SQLAlchemy-reserved and un-implementable on ORM models.
    custom_attributes = db.Column(db.JSON, nullable=True)

    # ── Audit ──────────────────────────────────────────────────────────────
    created_at   = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow,
                             onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'internal_key',
                            name='uq_offering_tenant_key'),
        # Note: index=True on tenant_id FK already generates ix_offering_tenant_id.
    )


class ConversationOffering(db.Model):
    """
    Phase 16.5A5: M:M bridge — ConversationState ↔ Offering.
    Backs the legacy `course` adapter. A conversation may link to multiple
    offerings; the legacy `course` getter returns the first linked offering name.

    Tenant scope is derived via conversation_state_id → conversation_state.tenant_id
    (mirrors PipelineStage deriving tenant via pipeline_id — no denormalized
    tenant_id column). Application queries MUST join through ConversationState to
    enforce tenant isolation.
    """
    __tablename__ = 'conversation_state_offerings'

    id                    = db.Column(db.Integer, primary_key=True)

    conversation_state_id = db.Column(db.Integer,
                                      db.ForeignKey('conversation_state.id'),
                                      nullable=False, index=True)
    offering_id           = db.Column(db.Integer,
                                      db.ForeignKey('offering.id'),
                                      nullable=False, index=True)

    created_at            = db.Column(db.DateTime, default=datetime.utcnow,
                                      nullable=False)

    __table_args__ = (
        db.UniqueConstraint('conversation_state_id', 'offering_id',
                            name='uq_conv_offering'),
    )


class ConversationTag(db.Model):
    """
    Phase 16.5A5: M:M bridge — ConversationState ↔ TagDefinition.
    Fulfils the bridge referenced in the TagDefinition docstring, enabling
    multi-dimensional lead segmentation without altering pipeline stages.

    Tenant scope is derived via conversation_state_id → conversation_state.tenant_id
    (no denormalized tenant_id). Application queries MUST join through
    ConversationState to enforce tenant isolation.
    """
    __tablename__ = 'conversation_state_tags'

    id                    = db.Column(db.Integer, primary_key=True)

    conversation_state_id = db.Column(db.Integer,
                                      db.ForeignKey('conversation_state.id'),
                                      nullable=False, index=True)
    tag_definition_id     = db.Column(db.Integer,
                                      db.ForeignKey('tag_definitions.id'),
                                      nullable=False, index=True)

    created_at            = db.Column(db.DateTime, default=datetime.utcnow,
                                      nullable=False)

    __table_args__ = (
        db.UniqueConstraint('conversation_state_id', 'tag_definition_id',
                            name='uq_conv_tag'),
    )


class Task(db.Model):
    """
    Phase 16.5A7: First-class staff task, owned by Admin, executed by Staff.

    Supersedes the event-sourced task model for the Task Engine, but does NOT
    replace it: `FOLLOW_UP_TASK` / `FOLLOW_UP_COMPLETED` LeadEvents are still
    written on create and complete (ADR-021), so the 15 existing analytics,
    activity-feed and lead-detail readers keep working byte-for-byte unchanged.
    Those events remain an immutable audit trail — edits and deletes here do NOT
    rewrite history, which is the correct semantic for an activity feed.

    Ownership (ADR-021): only ADMIN / SUPER_ADMIN may create, assign, edit or
    delete. STAFF may view, update status, add notes, and complete.

    assigned_staff is a normalized display-name string, not a FK — this mirrors
    the established ConversationState.assigned_staff convention.
    No db.relationship() — lookups stay explicit (codebase convention).
    """
    __tablename__ = 'tasks'

    id             = db.Column(db.Integer, primary_key=True)

    # ── Tenant Ownership ───────────────────────────────────────────────────
    tenant_id      = db.Column(db.String(36), db.ForeignKey('tenants.id'),
                               nullable=False, index=True)

    # task_uid: mirrors the legacy LeadEvent payload `task_id` so a Task row and
    # its legacy events can be correlated. Hex uuid4, as the legacy route emits.
    task_uid       = db.Column(db.String(32), nullable=False, index=True)

    # ── Subject ────────────────────────────────────────────────────────────
    # Nullable: a task may be standalone (not attached to a lead).
    lead_phone     = db.Column(db.String(20), nullable=True, index=True)

    title          = db.Column(db.String(200), nullable=False)
    notes          = db.Column(db.Text, nullable=True)       # admin brief
    staff_notes    = db.Column(db.Text, nullable=True)       # staff progress notes

    # ── Classification ─────────────────────────────────────────────────────
    priority       = db.Column(db.String(10), nullable=False, default='NORMAL')
    # priority: 'LOW' | 'NORMAL' | 'HIGH' | 'URGENT'

    status         = db.Column(db.String(12), nullable=False, default='OPEN')
    # status: 'OPEN' | 'IN_PROGRESS' | 'COMPLETED'

    # ── Scheduling ─────────────────────────────────────────────────────────
    # due_date stored as a string (YYYY-MM-DD) to round-trip byte-identically
    # with the legacy event payload, which the activity feed still reads.
    due_date       = db.Column(db.String(10), nullable=True)
    remind_at      = db.Column(db.DateTime, nullable=True, index=True)
    reminder_sent  = db.Column(db.Boolean, nullable=False, default=False)

    # ── Assignment / audit ─────────────────────────────────────────────────
    assigned_staff = db.Column(db.String(100), nullable=True, index=True)
    created_by     = db.Column(db.String(100), nullable=True)
    completed_by   = db.Column(db.String(100), nullable=True)
    completed_at   = db.Column(db.DateTime, nullable=True)

    created_at     = db.Column(db.DateTime, default=datetime.utcnow,
                               nullable=False)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow,
                               onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'task_uid', name='uq_task_tenant_uid'),
        db.Index('idx_task_tenant_status', 'tenant_id', 'status'),
        db.Index('idx_task_tenant_staff', 'tenant_id', 'assigned_staff'),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_uid": self.task_uid,
            "lead_phone": self.lead_phone,
            "title": self.title,
            "notes": self.notes,
            "staff_notes": self.staff_notes,
            "priority": self.priority,
            "status": self.status,
            "due_date": self.due_date,
            "assigned_staff": self.assigned_staff,
            "created_by": self.created_by,
            "completed_by": self.completed_by,
            "completed_at": self.completed_at,
            "created_at": self.created_at,
        }


class Notification(db.Model):
    """
    Phase 16.5A7: Per-recipient notification with read state (ADR-021).

    recipient is the normalized staff display name (normalize_staff_name), which
    is how staff identity is already expressed across the CRM
    (ConversationState.assigned_staff, task assignment, staff registry). Using a
    User FK here would not match how leads and tasks are assigned today.

    Delivery is in-app only. No email/WhatsApp fan-out in this phase.
    No db.relationship() — lookups stay explicit (codebase convention).
    """
    __tablename__ = 'notifications'

    # Canonical types. Anything not in this set is rejected at the service layer.
    TYPE_NEW_LEAD_ASSIGNED = 'NEW_LEAD_ASSIGNED'
    TYPE_TASK_ASSIGNED     = 'TASK_ASSIGNED'
    TYPE_TASK_UPDATED      = 'TASK_UPDATED'
    TYPE_TASK_COMPLETED    = 'TASK_COMPLETED'
    TYPE_LEAD_REASSIGNED   = 'LEAD_REASSIGNED'
    TYPE_REMINDER_DUE      = 'REMINDER_DUE'
    TYPE_SYSTEM_ALERT      = 'SYSTEM_ALERT'

    VALID_TYPES = (
        TYPE_NEW_LEAD_ASSIGNED, TYPE_TASK_ASSIGNED, TYPE_TASK_UPDATED,
        TYPE_TASK_COMPLETED, TYPE_LEAD_REASSIGNED, TYPE_REMINDER_DUE,
        TYPE_SYSTEM_ALERT,
    )

    id          = db.Column(db.Integer, primary_key=True)

    tenant_id   = db.Column(db.String(36), db.ForeignKey('tenants.id'),
                            nullable=False, index=True)

    recipient   = db.Column(db.String(100), nullable=False, index=True)
    notif_type  = db.Column(db.String(30), nullable=False)

    title       = db.Column(db.String(200), nullable=False)
    body        = db.Column(db.String(500), nullable=True)

    # Click targets. Both nullable: a SYSTEM_ALERT has neither.
    lead_phone  = db.Column(db.String(20), nullable=True)
    task_id     = db.Column(db.Integer, db.ForeignKey('tasks.id'), nullable=True)
    # No ondelete=CASCADE — forbidden by SCHEMA_RULES §12. Task deletion nulls
    # this at the application layer so the notification survives as a record.

    is_read     = db.Column(db.Boolean, nullable=False, default=False)
    read_at     = db.Column(db.DateTime, nullable=True)

    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False, index=True)

    __table_args__ = (
        # The unread-badge query: WHERE tenant_id=? AND recipient=? AND is_read=false
        db.Index('idx_notif_recipient_unread', 'tenant_id', 'recipient', 'is_read'),
        db.Index('idx_notif_recipient_created', 'tenant_id', 'recipient', 'created_at'),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.notif_type,
            "title": self.title,
            "body": self.body,
            "lead_phone": self.lead_phone,
            "task_id": self.task_id,
            "is_read": self.is_read,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
