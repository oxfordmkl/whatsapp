"""
Phase 1.5.5B (Phase 0) — Dynamic feature flags for the NEW State Engine only.

Why a separate module (not app.config):
  app.config evaluates its flags ONCE at import time, so flipping a Railway
  environment variable requires a process restart to take effect. The State
  Engine rollout needs *instant* rollback — toggle the env var, next request
  obeys it, no redeploy. These helpers therefore re-read os.environ on EVERY
  call.

Scope guard:
  This module governs ONLY the three new State Engine flags. It does NOT read,
  wrap, or alter MEMORY_OBSERVE_MODE, MEMORY_ACTIVATE, AUTH_MODE, Gemini, or any
  existing flag — those remain owned by app.config exactly as before.

All flags default to OFF (false). Nothing in this module is wired into any
production path in Phase 1; these helpers are dormant until later phases.
"""
import os

# Accepted truthy spellings — identical semantics to app.config's flag parsing
# so operators toggle State Engine flags the same way they toggle existing ones.
_TRUTHY = {"1", "true", "yes", "on"}

# Flag names (single source of truth for the env-var spelling).
STATE_ENGINE_V2 = "STATE_ENGINE_V2"       # Phase 2 — managed-row StateProxy
STATE_UOW_CONTEXT = "STATE_UOW_CONTEXT"   # Phase 3 — context-scoped deferred flush
STATE_MERGE_LOOKUP = "STATE_MERGE_LOOKUP" # Phase 4 — merge phone_exists into load


def _enabled(name: str) -> bool:
    """Return True iff env var `name` is set to a truthy value (read live)."""
    return os.environ.get(name, "false").strip().lower() in _TRUTHY


def state_engine_v2_enabled() -> bool:
    """Phase 2 gate — managed-row StateProxy path. Default OFF."""
    return _enabled(STATE_ENGINE_V2)


def state_uow_context_enabled() -> bool:
    """Phase 3 gate — context-scoped Unit of Work with deferred flush. Default OFF."""
    return _enabled(STATE_UOW_CONTEXT)


def state_merge_lookup_enabled() -> bool:
    """Phase 4 gate — derive is_new_lead from the state load. Default OFF."""
    return _enabled(STATE_MERGE_LOOKUP)
