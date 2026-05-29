from datetime import datetime
from app.extensions import db


class ConversationState(db.Model):
    """
    Persistent replacement for the old conversation_state dict.
    One row per WhatsApp phone number.
    """
    __tablename__ = "conversation_state"

    id           = db.Column(db.Integer, primary_key=True)
    phone        = db.Column(db.String(20),  unique=True, nullable=False, index=True)
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

    __table_args__ = (
        db.Index("idx_lead_event_phone_created", "phone", "created_at"),
    )
