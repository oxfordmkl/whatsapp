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

# Phase 1.6.2 — WhatsApp List Message transport. Narrowly scoped to the List
# Message platform feature only; it is NOT a general conversation-UX switch.
WA_LIST_MESSAGES = "WA_LIST_MESSAGES"

# Phase 8.2A — DB-backed Campaign engine (Campaign + CampaignRecipient +
# CampaignService + campaign worker). Narrowly scoped to the campaign send
# path; it does NOT gate the legacy /broadcast endpoints or campaign_service.py,
# which continue to serve production untouched while this is OFF.
CAMPAIGN_ENGINE_V2 = "CAMPAIGN_ENGINE_V2"

# Phase RC2.3A — staff identity migration (staff_master.json -> User.id).
# Both default OFF and NOTHING reads them in the Expand phase; they are
# declared here so the later phases are a toggle rather than a redeploy.
#
# STAFF_IDENTITY_DUAL_WRITE — write assigned_user_id alongside assigned_staff.
#   The string stays authoritative, so this is safe to enable and disable
#   freely; it only populates the FK.
# STAFF_IDENTITY_READ_FK    — make assigned_user_id authoritative for reads and
#   ownership. This is the only step that changes behaviour, which is why it is
#   separate: DUAL_WRITE can bake in for weeks before READ_FK is attempted.
STAFF_IDENTITY_DUAL_WRITE = "STAFF_IDENTITY_DUAL_WRITE"
STAFF_IDENTITY_READ_FK = "STAFF_IDENTITY_READ_FK"


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


def wa_list_messages_enabled() -> bool:
    """Phase 1.6.2 gate — send WhatsApp List Messages. Default OFF.

    When OFF, send_list() degrades to a plain-text rendering so no caller can
    break; it does not alter any other conversation behaviour.
    """
    return _enabled(WA_LIST_MESSAGES)


def campaign_engine_v2_enabled() -> bool:
    """Phase 8.2A gate — DB-backed Campaign engine. Default OFF.

    When OFF (always, today) the legacy campaign_service.start_campaign() and
    the /broadcast endpoints remain the only send paths, byte-for-byte
    unchanged. No production code reads this flag yet.
    """
    return _enabled(CAMPAIGN_ENGINE_V2)


def staff_identity_dual_write_enabled() -> bool:
    """Phase RC2.3B gate — populate assigned_user_id on write. Default OFF.

    Additive only: assigned_staff remains authoritative, so enabling this
    cannot change what any reader sees. No production code reads this flag in
    the Expand phase.
    """
    return _enabled(STAFF_IDENTITY_DUAL_WRITE)


def staff_identity_read_fk_enabled() -> bool:
    """Phase RC2.3C gate — assigned_user_id becomes authoritative. Default OFF.

    The ONLY step in the migration that changes behaviour. Kept separate from
    dual-write so the FK can be populated and verified in production for as
    long as needed before anything depends on it. No production code reads this
    flag in the Expand phase.
    """
    return _enabled(STAFF_IDENTITY_READ_FK)
