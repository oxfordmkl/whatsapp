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
                             onupdate=datetime.utcnow)

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
