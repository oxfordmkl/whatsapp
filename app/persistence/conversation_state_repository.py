"""
Phase 1.5.5B (Phase 1) — ConversationState repository (isolated foundation, UNWIRED).

The narrow persistence seam approved in Phase 1.5.4A: business logic depends on
this interface, not on db.session directly. It is deliberately scoped to the ONE
boundary known to need flexibility (the conversation-state hot path) — it is not
a project-wide repository layer.

Why this seam exists: it is the single point behind which future storage
strategies (Redis cache-aside, read replicas, per-tenant engines) can be added
without touching business logic.

Transaction ownership: the repository NEVER commits. Commit/rollback belong to
the Unit of Work (unit_of_work.py). Repository methods may flush() to obtain a
generated primary key and to make writes visible to later reads within the same
unit — but the transaction boundary is not theirs to close.

Managed rows: get()/get_or_create() return the live, session-attached
ConversationState ORM instance (NOT a detached dict). Mutating it and letting
the Unit of Work commit is the whole point — it removes the per-assignment
re-SELECT + COMMIT the current StateProxy pays.

Phase 1 status: imported by nobody in production. The session and model are
resolved lazily (and are injectable) so the module has no import-time app
dependency and is unit-testable against an in-memory database.

Tenant isolation: tenant_id appears in the WHERE clause of every query.
"""
from abc import ABC, abstractmethod


class ConversationStateRepository(ABC):
    """Persistence contract for conversation state, keyed by (tenant_id, phone)."""

    @abstractmethod
    def get(self, tenant_id, phone):
        """Return the managed state row, or None if it does not exist."""
        raise NotImplementedError

    @abstractmethod
    def exists(self, tenant_id, phone) -> bool:
        """Return True iff a state row exists for (tenant_id, phone)."""
        raise NotImplementedError

    @abstractmethod
    def get_or_create(self, tenant_id, phone, name):
        """Return (row, created): the managed row and whether it was just created.

        A newly created row is added and flushed (so its primary key is assigned
        and it is visible to later reads in this unit) but NOT committed.
        """
        raise NotImplementedError


class SQLAlchemyConversationStateRepository(ConversationStateRepository):
    """SQLAlchemy-backed implementation over the ConversationState model.

    session/model are injectable for testing; in production both resolve lazily
    to app.extensions.db.session and app.models.ConversationState.
    """

    __slots__ = ("_session", "_model")

    def __init__(self, session=None, model=None):
        self._session = session
        self._model = model

    @property
    def _s(self):
        if self._session is not None:
            return self._session
        from app.extensions import db  # lazy
        return db.session

    @property
    def _model_cls(self):
        if self._model is not None:
            return self._model
        from app.models import ConversationState  # lazy
        return ConversationState

    def get(self, tenant_id, phone):
        m = self._model_cls
        return (
            self._s.query(m)
            .filter(m.phone == phone, m.tenant_id == tenant_id)
            .first()
        )

    def exists(self, tenant_id, phone) -> bool:
        m = self._model_cls
        return (
            self._s.query(m)
            .filter(m.phone == phone, m.tenant_id == tenant_id)
            .count()
            > 0
        )

    def get_or_create(self, tenant_id, phone, name):
        row = self.get(tenant_id, phone)
        if row is not None:
            return row, False

        m = self._model_cls
        # Identity fields only; the model's own column defaults populate the
        # rest (stage="new", course="", ...). Field parity with the legacy
        # get_or_create_state() is reconciled at Phase 2 wiring, not here.
        row = m(phone=phone, name=name, tenant_id=tenant_id)
        self._s.add(row)
        self._s.flush()  # assign PK + make visible in-unit; NO commit
        return row, True
