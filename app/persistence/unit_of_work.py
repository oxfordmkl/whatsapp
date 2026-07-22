"""
Phase 1.5.5B (Phase 1) — Unit of Work (isolated foundation, UNWIRED).

An execution-context-scoped transaction boundary around SQLAlchemy's own Unit
of Work. It does NOT reimplement dirty tracking — SQLAlchemy already does that
correctly. Its single job is to own *when* the transaction commits, so callers
can mutate managed rows freely and pay exactly one COMMIT per context instead of
one per assignment (the Phase 1.5.3 finding).

Scoping (approved Phase 1.5.4A): backed by contextvars, so one persistence
semantic serves HTTP requests, background threads, scheduled jobs and future
queue workers — not just Flask requests.

Re-entrancy: nested unit_of_work() blocks reuse the outermost unit. Only the
outermost block commits/rolls back; inner blocks are transparent. This lets a
job open a UoW and call helpers that also open one, without double-commits.

Phase 1 status: imported by nobody in production. The session is resolved lazily
from app.extensions.db so the module carries no import-time app dependency and
is unit-testable with an injected session.

Contract:
  - On clean exit  → commit().
  - On exception   → rollback(), then re-raise.
  - session is db.session by default, or an injected session for testing.
  - Never swallows exceptions.
"""
import logging
import contextvars
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_active_uow: contextvars.ContextVar = contextvars.ContextVar(
    "active_unit_of_work", default=None
)


class UnitOfWork:
    """A transaction boundary over a SQLAlchemy session.

    Prefer the unit_of_work() context manager; instantiate directly only when
    managing the lifecycle by hand (e.g. tests).
    """

    __slots__ = ("_session",)

    def __init__(self, session=None):
        # None → resolve db.session lazily on first use (production path).
        self._session = session

    @property
    def session(self):
        if self._session is not None:
            return self._session
        from app.extensions import db  # lazy: no import-time app dependency
        return db.session

    def flush(self) -> None:
        """Send pending changes to the DB WITHOUT ending the transaction.

        Used to obtain generated primary keys and to make writes visible to
        later reads inside the same unit. Does not commit.
        """
        self.session.flush()

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()


def current_unit_of_work():
    """Return the UnitOfWork active in this context, or None."""
    return _active_uow.get()


def reset_active_unit_of_work() -> None:
    """Safety-net hard reset of the active unit for the current context.

    unit_of_work() already resets its own token on exit; this exists only for the
    Phase 3 teardown_request net to clear a unit that leaked past an abnormal exit.
    """
    _active_uow.set(None)


@contextmanager
def unit_of_work(session=None):
    """Open (or join) a unit of work for the current execution context.

    If a unit is already active, this block joins it and is transparent — no
    commit/rollback happens here; the outermost block owns the boundary.
    Otherwise a new unit is created: commit on clean exit, rollback + re-raise
    on error.
    """
    existing = _active_uow.get()
    if existing is not None:
        # Re-entrant join: reuse the outer unit, do not manage the boundary.
        yield existing
        return

    uow = UnitOfWork(session=session)
    token = _active_uow.set(uow)
    try:
        yield uow
        uow.commit()
    except Exception:
        uow.rollback()
        raise
    finally:
        _active_uow.reset(token)
