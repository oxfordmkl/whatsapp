"""
Phase 1.5.5B (Phase 1) — TenantContext (isolated foundation, UNWIRED).

An explicit, execution-context-scoped carrier for the active tenant id, backed
by contextvars so it is correct across HTTP requests, background threads,
scheduled jobs and (future) async/queue workers — unlike flask.g, which is
request-only.

Design intent (approved Phase 1.5.4A): replace the implicit resolve_tenant_id()
re-resolution sprawl with one value carried through the call context. This is
the seam that later enables per-tenant storage routing.

Phase 1 status: this module is imported by nobody in production. It exists only
as isolated infrastructure with unit tests. No behavior change.

Contract:
  - get_current_tenant_id() returns the active tenant id, or None if unset.
  - tenant_context(tid) is the preferred entry point: a scope that sets the id
    on enter and restores the previous value on exit (nesting-safe).
  - set_/reset_ are the low-level primitives for callers that manage their own
    lifecycle (e.g. middleware). Always pair a set with its reset via the token.
"""
import contextvars
from contextlib import contextmanager

# default=None → "no tenant bound in this context yet".
_current_tenant_id: contextvars.ContextVar = contextvars.ContextVar(
    "current_tenant_id", default=None
)


def get_current_tenant_id():
    """Return the tenant id bound to the current execution context, or None."""
    return _current_tenant_id.get()


def set_current_tenant_id(tenant_id):
    """Bind `tenant_id` to the current context. Returns a token for reset()."""
    return _current_tenant_id.set(tenant_id)


def reset_current_tenant_id(token) -> None:
    """Restore the previous binding using the token from set_current_tenant_id."""
    _current_tenant_id.reset(token)


@contextmanager
def tenant_context(tenant_id):
    """
    Scope the active tenant id for the duration of the `with` block.

    Nesting-safe: the previous value (including None) is restored on exit, even
    if the block raises.
    """
    token = _current_tenant_id.set(tenant_id)
    try:
        yield tenant_id
    finally:
        _current_tenant_id.reset(token)
