"""
DB-backed state management.

Replaces the old in-memory:
    conversation_state: dict = {}
    follow_up_queue: list = []

All public functions require an active Flask application context.
"""

from datetime import datetime


# ── StateProxy ─────────────────────────────────────────────────────────────
class StateProxy(dict):
    """
    A dict subclass returned by get_or_create_state().

    Any assignment (st["stage"] = "...") automatically persists to the DB.
    This means zero changes are needed inside smart_reply() — it still works
    exactly like before, just durably.
    """

    def __init__(self, phone: str, data: dict, tenant_id: str = None):
        super().__init__(data)
        self._phone = phone
        self._tenant_id = tenant_id

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        _db_save(self._phone, self, self._tenant_id)


# ── Internal DB persistence ────────────────────────────────────────────────
def _db_save(phone: str, st: dict, tenant_id: str = None):
    """Write a state dict back to the ConversationState row."""
    from app.models import ConversationState
    from app.extensions import db
    from app.services.log_service import _get_default_tenant_id

    if tenant_id is None:
        tenant_id = _get_default_tenant_id()

    row = ConversationState.query.filter_by(phone=phone, tenant_id=tenant_id).first()
    if row:
        for key in ("name", "stage", "course", "goal",
                    "batch_time", "offer_course", "last_msg", "last_text"):
            if key in st:
                setattr(row, key, st[key])
        db.session.commit()
    else:
        raise RuntimeError(f"State DB save error: Row not found for phone {phone}")


# ── Public helpers (used by router, webhook, admin, health) ────────────────
def get_or_create_state(phone: str, name: str, tenant_id: str = None) -> StateProxy:
    """
    Load conversation state from DB.
    Creates a new row on first contact.
    Returns a StateProxy that auto-saves on mutation.
    """
    from app.models import ConversationState
    from app.extensions import db
    from app.services.log_service import _get_default_tenant_id
    
    if tenant_id is None:
        tenant_id = _get_default_tenant_id()

    row = ConversationState.query.filter_by(phone=phone, tenant_id=tenant_id).first()
    if row is None:
        row = ConversationState(
            phone=phone,
            name=name,
            stage="new",
            course="",
            goal="",
            batch_time="",
            offer_course="",
            last_msg=datetime.now().isoformat(),
            last_text="",
            tenant_id=tenant_id,  # Phase 12-C2: Required after Phase 12-B migration
        )
        db.session.add(row)
        db.session.commit()

    return StateProxy(phone, row.to_dict(), tenant_id=tenant_id)


def phone_exists(phone: str, tenant_id: str = None) -> bool:
    """True if this phone number has any conversation state in the DB."""
    from app.models import ConversationState
    from app.services.log_service import _get_default_tenant_id
    
    if tenant_id is None:
        tenant_id = _get_default_tenant_id()
        
    return ConversationState.query.filter_by(phone=phone, tenant_id=tenant_id).count() > 0


def count_states() -> int:
    """Total number of unique leads in DB."""
    from app.models import ConversationState
    return ConversationState.query.count()


def count_pending_followups() -> int:
    """Total follow-up jobs not yet sent."""
    from app.models import FollowUpJob
    return FollowUpJob.query.filter_by(done=False).count()


def get_all_states() -> list:
    """All conversation states — used by admin /stats endpoint."""
    from app.models import ConversationState
    return [
        {
            "name":        r.name,
            "stage":       r.stage,
            "last_text":   r.last_text,
            "last_active": r.last_msg,
            "course":      r.course,
        }
        for r in ConversationState.query.all()
    ]


def get_stage_breakdown() -> dict:
    """Stage counts — used by admin /stats endpoint."""
    from app.models import ConversationState
    stages = {
        "new", "goal_selection", "course_recommendation", "course_viewed",
        "demo_time_ask", "demo_date_ask", "demo_booked",
        "offer_menu", "payment_pending", "enrolled", "not_sure", "done",
    }
    return {
        s: ConversationState.query.filter(ConversationState.stage == s).count()
        for s in stages
    }
