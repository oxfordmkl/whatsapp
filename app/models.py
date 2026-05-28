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
