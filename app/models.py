from datetime import datetime
from app.extensions import db
from flask_login import UserMixin
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
    # Bot display name shown to leads (e.g., "Aaliza", "Priya", "Rahul").
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
    stage        = db.Column(db.String(50),  default="new")
    course       = db.Column(db.String(200), default="")
    goal         = db.Column(db.String(50),  default="")
    batch_time   = db.Column(db.String(100), default="")
    offer_course = db.Column(db.String(50),  default="")
    last_msg     = db.Column(db.String(50),  default="")
    last_text    = db.Column(db.Text,        default="")
    updated_at   = db.Column(db.DateTime,    default=datetime.utcnow,
                             onupdate=datetime.utcnow, nullable=False)

    # ── Phase 4A: CRM Expansion Fields ──
    created_at     = db.Column(db.DateTime,    default=datetime.utcnow, nullable=False)
    lead_status    = db.Column(db.String(50),  nullable=True, default="Lead")
    assigned_staff = db.Column(db.String(100), nullable=True)
    lead_score     = db.Column(db.Integer,     nullable=True, default=0)
    is_admitted    = db.Column(db.Boolean,     nullable=True, default=False)
    notes          = db.Column(db.Text,        nullable=True)

    # ── Phase 11-D1: Opt-Out Safety ──
    is_opted_out   = db.Column(db.Boolean,     nullable=True, default=False)

    # ── Phase 12-B: SaaS Architecture ──
    tenant_id      = db.Column(db.String(36), db.ForeignKey('tenants.id'), nullable=False, index=True)

    __table_args__ = (
        db.UniqueConstraint('phone', 'tenant_id', name='uq_conversation_state_phone_tenant'),
    )

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

