"""
Phase 1.5.5B (Phase 1) — persistence foundation package (isolated, UNWIRED).

Public API for the State Engine foundation. Nothing here is imported by
production code in Phase 1; these are dormant seams introduced ahead of the
later, flag-gated wiring phases.
"""
from app.persistence.unit_of_work import (
    UnitOfWork,
    unit_of_work,
    current_unit_of_work,
    reset_active_unit_of_work,
)
from app.persistence.conversation_state_repository import (
    ConversationStateRepository,
    SQLAlchemyConversationStateRepository,
)
from app.persistence.scope import (
    state_unit_of_work,
    flush_state_writes,
)

__all__ = [
    "UnitOfWork",
    "unit_of_work",
    "current_unit_of_work",
    "reset_active_unit_of_work",
    "ConversationStateRepository",
    "SQLAlchemyConversationStateRepository",
    "state_unit_of_work",
    "flush_state_writes",
]
