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
    from app.services.log_service import resolve_tenant_id

    tenant_id = resolve_tenant_id(tenant_id)

    row = ConversationState.query.filter_by(phone=phone, tenant_id=tenant_id).first()
    if row:
        for key in ("name", "stage", "course", "goal",
                    "batch_time", "offer_course", "last_msg", "last_text"):
            if key in st:
                setattr(row, key, st[key])
        db.session.commit()
    else:
        raise RuntimeError(f"State DB save error: Row not found for phone {phone}")


# ── StateProxy V2 (Phase 1.5.5C — flag STATE_ENGINE_V2) ─────────────────────
# Persisted keys — identical set to _db_save() above, so V2's write-through has
# exactly the same DB footprint as V1. Kept as a separate constant so V1 is left
# untouched.
_PERSISTED_KEYS = (
    "name", "stage", "course", "goal",
    "batch_time", "offer_course", "last_msg", "last_text",
)


class StateProxyV2(dict):
    """
    Managed-row StateProxy. Same dict API as StateProxy (V1); router and every
    other caller are unaffected.

    Difference from V1:
      - Wraps the live, session-attached ConversationState row (no detached
        re-fetch on every write — V1's redundant SELECT is gone).
      - Writes route through the model's OWN attribute setters
        (setattr(row, key, value)), so the hybrid stage/course/offer_course/
        batch_time logic and all model validation/business rules are preserved,
        never bypassed.

    Persistence policy:
      - If a UnitOfWork is active in this execution context, flush() and let the
        UoW own the commit (deferred — takes effect once Phase 3 wires the UoW).
      - Otherwise commit immediately. This immediate-flush fallback preserves
        today's per-assignment durability while the UoW is not yet wired.

    Reads come from the in-memory snapshot seeded from row.to_dict(), identical
    to V1, so read semantics are unchanged.
    """

    def __init__(self, phone: str, row, tenant_id: str = None):
        super().__init__(row.to_dict())
        self._phone = phone
        self._row = row
        self._tenant_id = tenant_id

    def __setitem__(self, key, value):
        super().__setitem__(key, value)          # keep snapshot in sync (as V1)
        if key in _PERSISTED_KEYS:
            setattr(self._row, key, value)       # model setter — rules preserved
            self._persist()

    def _persist(self) -> None:
        from app.persistence.unit_of_work import current_unit_of_work
        uow = current_unit_of_work()
        if uow is not None:
            uow.flush()                          # UoW owns commit (Phase 3+)
        else:
            from app.extensions import db
            db.session.commit()                  # immediate-flush fallback


# ── Load / wrap helpers ────────────────────────────────────────────────────
def _load_or_create_row(phone: str, name: str, tenant_id: str):
    """Load the ConversationState row (creating it on first contact).

    Returns (row, created). The single SELECT + optional INSERT/commit here is
    exactly what get_or_create_state performed inline before Phase 4.
    """
    from app.models import ConversationState
    from app.extensions import db

    row = ConversationState.query.filter_by(phone=phone, tenant_id=tenant_id).first()
    if row is not None:
        return row, False

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
    return row, True


def _wrap_proxy(phone: str, row, tenant_id: str):
    """Wrap a row in the flag-selected proxy (Phase 1.5.5C)."""
    from app.flags import state_engine_v2_enabled
    if state_engine_v2_enabled():
        return StateProxyV2(phone, row, tenant_id=tenant_id)
    return StateProxy(phone, row.to_dict(), tenant_id=tenant_id)


# ── Phase 1.5.5E: request-scoped state cache (flag STATE_MERGE_LOOKUP) ──────
# Lets resolve_is_new_lead() and the later get_or_create_state() share ONE load
# per request, removing the standalone phone_exists() SELECT. Backed by flask.g,
# which is cleared automatically at the end of every request → no cross-request
# staleness. Outside a request context (scheduler/CLI) the cache is skipped, so
# those callers behave exactly as before.
_G_CACHE_ATTR = "_state_engine_cache"


def _state_cache():
    """Return the per-request cache dict, or None when outside a request."""
    from flask import has_request_context, g
    if not has_request_context():
        return None
    cache = getattr(g, _G_CACHE_ATTR, None)
    if cache is None:
        cache = {}
        setattr(g, _G_CACHE_ATTR, cache)
    return cache


def _get_cached_state(tenant_id: str, phone: str):
    cache = _state_cache()
    if cache is None:
        return None
    return cache.get((tenant_id, phone))


def _set_cached_state(tenant_id: str, phone: str, proxy, created: bool) -> None:
    cache = _state_cache()
    if cache is not None:
        cache[(tenant_id, phone)] = (proxy, created)


# ── Public helpers (used by router, webhook, admin, health) ────────────────
def get_or_create_state(phone: str, name: str, tenant_id: str = None) -> StateProxy:
    """
    Load conversation state from DB.
    Creates a new row on first contact.
    Returns a StateProxy that auto-saves on mutation.
    """
    from app.services.log_service import resolve_tenant_id
    tenant_id = resolve_tenant_id(tenant_id)

    # Phase 1.5.5E: reuse a load already done by resolve_is_new_lead this request.
    from app.flags import state_merge_lookup_enabled
    merge = state_merge_lookup_enabled()
    if merge:
        cached = _get_cached_state(tenant_id, phone)
        if cached is not None:
            return cached[0]

    row, _created = _load_or_create_row(phone, name, tenant_id)
    proxy = _wrap_proxy(phone, row, tenant_id)
    if merge:
        _set_cached_state(tenant_id, phone, proxy, _created)
    return proxy


def resolve_is_new_lead(phone: str, name: str, tenant_id: str = None) -> bool:
    """Return True when this phone has no prior conversation state.

    Flag OFF (STATE_MERGE_LOOKUP) → legacy behaviour: a standalone existence
    count via phone_exists(), unchanged.

    Flag ON → merge the check into the state load: load-or-create the row ONCE,
    cache it on flask.g so the later get_or_create_state() in smart_reply reuses
    it (removing the second SELECT), and derive is_new_lead from whether the row
    was just created.
    """
    from app.flags import state_merge_lookup_enabled
    if not state_merge_lookup_enabled():
        return not phone_exists(phone, tenant_id=tenant_id)

    from app.services.log_service import resolve_tenant_id
    tenant_id = resolve_tenant_id(tenant_id)

    cached = _get_cached_state(tenant_id, phone)
    if cached is not None:
        return cached[1]

    row, created = _load_or_create_row(phone, name, tenant_id)
    proxy = _wrap_proxy(phone, row, tenant_id)
    _set_cached_state(tenant_id, phone, proxy, created)
    return created


def phone_exists(phone: str, tenant_id: str = None) -> bool:
    """True if this phone number has any conversation state in the DB."""
    from app.models import ConversationState
    from app.services.log_service import resolve_tenant_id

    tenant_id = resolve_tenant_id(tenant_id)

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
