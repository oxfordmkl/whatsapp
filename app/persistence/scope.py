"""
Phase 1.5.5D (Phase 3) — flag-aware wiring layer for the State Engine UnitOfWork.

This is the ONLY place that decides, per the STATE_UOW_CONTEXT flag, whether an
entry point (webhook, and later scheduler/broadcast/CLI) runs inside a
context-scoped UnitOfWork. Keeping the flag logic here keeps the entry-point
diffs tiny and the behavior in one auditable spot.

Behavior:
  - Flag OFF → state_unit_of_work() is a no-op scope (yields None). No UnitOfWork
    is opened, so StateProxyV2 keeps its immediate-flush fallback and V1 is wholly
    unaffected. Byte-identical to pre-Phase-3 behavior.
  - Flag ON  → state_unit_of_work() opens a real UnitOfWork for the context. State
    writes made inside are deferred (flushed) and committed once when the scope
    exits — which, because send_reply() runs inside the scope, is AFTER the reply
    is sent, keeping the commit off the user-facing latency path.

Nothing here changes business logic or routing.
"""
from contextlib import contextmanager


@contextmanager
def state_unit_of_work():
    """Open a UnitOfWork for this context iff STATE_UOW_CONTEXT is enabled.

    Yields the active UnitOfWork (flag ON) or None (flag OFF).
    """
    from app.flags import state_uow_context_enabled

    if not state_uow_context_enabled():
        yield None
        return

    from app.persistence.unit_of_work import unit_of_work
    with unit_of_work() as uow:
        yield uow


def flush_state_writes() -> None:
    """Flush deferred state writes if a UnitOfWork is active; else a no-op.

    Called explicitly after send_reply() so pending state SQL is pushed (and any
    DB error surfaces) before post-reply work runs. The durable commit still
    happens when the scope exits.
    """
    from app.persistence.unit_of_work import current_unit_of_work

    uow = current_unit_of_work()
    if uow is not None:
        uow.flush()
