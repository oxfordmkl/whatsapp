"""Phase RC2.3E-0 — dual-read owner resolution.

DORMANT. Nothing in the application calls this module. It exists so the reader
migration (RC2.3E-1 onward) is a repointing exercise against ONE tested
abstraction instead of fifteen copy-pasted flag checks — the same shape that
made RC2.2D safe.

THE PROBLEM IT SOLVES
---------------------
Lead and task ownership is stored twice: `assigned_staff` (a display-name
string, authoritative today) and `assigned_user_id` (a FK, populated by the
RC2.3C backfill and kept current by RC2.3D dual-write). Flipping consumers
from one to the other means every one of them needs to know which is
authoritative. Fifteen consumers each reading STAFF_IDENTITY_READ_FK is
fifteen chances for one to be missed, inverted, or to drift — and a screen
that reads the string while the screen beside it reads the FK is exactly the
"authoritative and wrong" failure RC2.2D Batch 1 was grouped to avoid.

So the flag is read HERE and nowhere else.

THE KEY-SPACE IDEA
------------------
Every migrating consumer does the same two things: bucket rows per staff
member, and render the bucket's owner. Both work if the flag decides ONE
thing — what a row's owner KEY is:

    flag OFF   key = normalize_staff_name(assigned_staff)   e.g. 'Anju'
    flag ON    key = assigned_user_id                       e.g. 2

Everything else follows: owner_column() gives the column to GROUP BY or
filter on, staff_keys() gives the tenant's staff in the SAME key space, and
display_for_key() renders a key back to a name. A consumer written against
these four never branches on the flag itself.

BEHAVIOUR WHILE THE FLAG IS OFF
-------------------------------
Byte-identical to today. owner_key() is normalize_staff_name() — including
its "Unassigned" return for a blank owner, which several consumers rely on as
a real bucket. staff_keys() is the same {normalized: display} mapping
calculate_workload_scoring already builds. Nothing here changes a number while
the flag is off, and the tests assert that against the live implementations.

ROLLBACK
--------
app/flags.py re-reads os.environ per call, so the flag is a runtime toggle
with no redeploy. Because every migrated reader will resolve through this
module, flipping STAFF_IDENTITY_READ_FK back to false reverts all of them at
once, in seconds. That is only true for as long as nothing reads the flag
directly — test_no_consumer_reads_the_flag_directly guards it.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
No permission or authorization helper. RC2.3E discovery found two blockers:
get_current_actor() carries no user_id (so an authorization check would have
to look the actor up BY NAME to get an id — reintroducing the exact fragility
the FK removes, inside the security path), and the legacy task-completion path
holds its assignee in a LeadEvent JSON payload with no Task row and therefore
no FK at all. Notifications and the created_by/completed_by audit fields are
excluded for the same structural reason. Those stay string-based.

Framework independence
----------------------
No flask import. Callers pass tenant_id; this module never reads request,
session or current_user. Matches staff_service and sales_transition_service.
"""
import logging

logger = logging.getLogger(__name__)

#: The bucket a row falls into when it has no owner. Under the string regime
#: this is a real key produced by normalize_staff_name(""), and consumers
#: (allocation, admin tasks) render it as a heading. Under the FK regime an
#: unowned row has key None. is_unassigned() spans both so a consumer never
#: has to know which regime it is in.
UNASSIGNED = "Unassigned"


def _norm(name):
    """Mirror of admin.normalize_staff_name().

    Kept here so the service layer does not import from the routes layer
    (CODE_CONVENTIONS §3), the same precedent task_service set.

    It must match EXACTLY, including the "Unassigned" return for a blank
    value — that string is a real bucket key, not an error path. Note that
    task_service._norm() calls itself a mirror but returns "" for blanks;
    this one follows normalize_staff_name(), which is what every consumer
    migrating in RC2.3E actually uses. test_norm_matches_normalize_staff_name
    pins the agreement.
    """
    if not name:
        return UNASSIGNED
    cleaned = name.strip()
    if not cleaned:
        return UNASSIGNED
    return cleaned.title()


def read_fk_enabled() -> bool:
    """Whether assigned_user_id is authoritative for reads.

    THE ONLY place the application reads STAFF_IDENTITY_READ_FK. Read per call
    (app/flags.py re-reads os.environ), so a toggle takes effect without a
    redeploy and without restarting workers.
    """
    from app.flags import staff_identity_read_fk_enabled
    return staff_identity_read_fk_enabled()


def owner_column(model):
    """The column carrying ownership for this model, under the current regime.

    Use for GROUP BY, ORDER BY and filters so an aggregate query buckets on
    whichever column is authoritative:

        col = owner_column(ConversationState)
        db.session.query(col, func.count()).group_by(col)

    Index note: Task has composite indexes on BOTH (tenant_id, assigned_staff)
    and (tenant_id, assigned_user_id), so grouping is covered either way.
    ConversationState.assigned_staff has NO index while assigned_user_id does,
    so on leads the FK regime is better covered, not worse.
    """
    return model.assigned_user_id if read_fk_enabled() else model.assigned_staff


def owner_key(row):
    """The owner key for an ORM row: a normalized name, or a user id.

    Returns UNASSIGNED / None respectively when the row has no owner — use
    is_unassigned() rather than comparing, so the caller stays regime-agnostic.
    """
    if read_fk_enabled():
        return getattr(row, "assigned_user_id", None)
    return _norm(getattr(row, "assigned_staff", None))


def key_from_value(value):
    """owner_key() for a raw column value rather than a row.

    Aggregate queries select owner_column() and get back bare values, not ORM
    instances; this keys those results the same way.
    """
    if read_fk_enabled():
        return value
    return _norm(value)


def is_unassigned(key) -> bool:
    """True when a key represents "no owner", in either regime.

    Under the string regime that is the literal 'Unassigned' bucket; under the
    FK regime it is None. A consumer that checks this instead of comparing to
    a literal keeps working across the flip.
    """
    if key is None:
        return True
    if isinstance(key, str):
        return _norm(key) == UNASSIGNED
    return False


def staff_keys(tenant_id, active_only=True, include_admins=False):
    """The tenant's staff, keyed in the SAME space as owner_key().

    Returns {key: display_name} — exactly the mapping
    calculate_workload_scoring builds by hand today:

        {normalize_staff_name(display): display}     flag OFF
        {user.id: display}                           flag ON

    Fail-closed: an unresolvable tenant yields {}, matching staff_service and
    the Phase 14B.3 contract. A consumer seeding buckets from this therefore
    shows an empty screen rather than another tenant's staff.
    """
    from app.services import staff_service

    users = staff_service.list_staff(tenant_id, active_only=active_only,
                                     include_admins=include_admins)
    if read_fk_enabled():
        return {u.id: u.display_label() for u in users}
    return {_norm(u.display_label()): u.display_label() for u in users}


def display_for_key(tenant_id, key):
    """Render an owner key back to an operator-facing name, or None.

    Tenant-scoped in both regimes: a key belonging to another tenant resolves
    to nothing rather than leaking that tenant's staff name.

    Under the FK regime this resolves through staff_service.display_for_id().
    Callers rendering MANY rows should build staff_keys() once and look up in
    it rather than calling this per row — one query per row is how an N+1
    creeps into a list view.
    """
    from app.services import staff_service

    if is_unassigned(key):
        return UNASSIGNED
    if read_fk_enabled():
        return staff_service.display_for_id(tenant_id, key)
    # String regime: the key IS a normalized name. Prefer the tenant's stored
    # spelling when it is a known staff member, so a lead written as 'ANJU'
    # renders as 'Anju' rather than shouting.
    return staff_keys(tenant_id, active_only=False).get(key, key)


class AssignmentResolution:
    """The outcome of validating one assigned_staff value. See
    resolve_assignment()."""

    __slots__ = ("value", "user", "user_id", "ok", "reason", "canonical")

    def __init__(self, value, user, ok, reason, canonical):
        self.value = value          #: input, trimmed; None when blank
        self.user = user            #: resolved User, or None
        self.user_id = user.id if user is not None else None
        self.ok = ok               #: True when blank OR resolved
        self.reason = reason        #: why it failed, or None
        self.canonical = canonical  #: the tenant's stored spelling, or None

    @property
    def is_unassignment(self) -> bool:
        """A blank value means "clear the owner" — legal, not a failure."""
        return self.ok and self.user is None

    def __repr__(self):
        return (f"<AssignmentResolution value={self.value!r} ok={self.ok} "
                f"user_id={self.user_id} reason={self.reason!r}>")


def resolve_assignment(tenant_id, value):
    """Validate an assigned_staff value against the tenant's real staff.

    Phase H3-0 — DORMANT. No write path calls this yet.

    THE DEFECT THIS EXISTS TO CLOSE
    -------------------------------
    All eight assigned_staff write paths accept a free string. The ROW is
    tenant-scoped everywhere (Phase 14B.2 C1), but the VALUE is not checked
    against anything: an admin can store a colleague's name from another
    tenant, a deleted staff member, or 'asdf'. Production already carries one
    such row ('Anju_display').

    Today that is cosmetic — the lead shows under a bucket matching no real
    person. After RC2.3E flips reads to the FK it becomes INVISIBLE: the FK is
    NULL, so the lead vanishes from every per-staff view and reappears only
    under Unassigned. A lead someone believes is assigned stops being shown to
    its owner.

    WHY IT DELEGATES RATHER THAN RE-IMPLEMENTS
    ------------------------------------------
    It calls staff_backfill_service.resolve_user_id() — the SAME function
    sync_assigned_user() uses to populate the FK. That is not convenience, it
    is the correctness property: if this validator accepted a value the
    dual-write could not resolve, validation would pass and the FK would still
    land NULL, which is strictly worse than no validation at all. One resolver
    means the two cannot disagree. test_agrees_with_sync_assigned_user pins it.

    INACTIVE STAFF RESOLVE
    ----------------------
    resolve_user_id()'s candidate pool is every User in the tenant, including
    inactive ones and admins. That is deliberate and matches dual-write: an
    inactive member is a REAL person whose FK populates correctly, and
    inactive-but-assigned is already an established state — crm_staff_management
    blocks deactivating someone who still holds leads (BLOCK_DEACTIVATION), so
    the reverse pairing must remain expressible. Refusing them here would
    diverge from the FK the dual-write then writes.

    BLANK IS NOT A FAILURE
    ----------------------
    Clearing an assignment is legal on every path. A blank value returns
    ok=True with user=None (is_unassignment), so a caller can distinguish
    "unassign this lead" from "I could not resolve this name".

    RETURNS AN AssignmentResolution — IT DOES NOT DECIDE POLICY
    -----------------------------------------------------------
    Whether an unresolvable value is REJECTED (400 / skipped CSV row) or
    accepted with a warning is a per-path decision for H3-1, and the paths
    differ: a crafted JSON POST should probably be refused, while failing a
    whole CSV row over one bad cell may not be what an operator wants. This
    function reports; the caller decides.

    `canonical` carries the tenant's stored spelling (so 'anju' resolves to
    'Anju') for a caller that wants to normalise on write. Using it CHANGES
    stored values, so it is offered, never applied here.
    """
    from app.services.staff_backfill_service import resolve_user_id
    from app.models import User

    trimmed = (value or "").strip() or None
    if trimmed is None:
        return AssignmentResolution(None, None, True, None, None)
    if not tenant_id:
        return AssignmentResolution(trimmed, None, False,
                                    "no tenant context", None)

    user_id, reason = resolve_user_id(tenant_id, trimmed)
    if user_id is None:
        logger.info("assignment rejected: tenant=%s value=%r reason=%s",
                    tenant_id, trimmed, reason)
        return AssignmentResolution(trimmed, None, False, reason, None)

    user = User.query.get(user_id)
    return AssignmentResolution(trimmed, user, True, None,
                                user.display_label() if user else None)


def owner_filter(model, user):
    """Predicate selecting the rows owned by ONE user.

    For the later filtering batch (RC2.3E-3) — built here so that phase does
    not reintroduce a copy-pasted flag check.

    Under the FK regime this is an integer comparison against an indexed
    column. Under the string regime it reproduces the existing
    lower(trim(col)) == name idiom used by _build_leads_query and
    _staff_ownership_clause, so ownership resolves identically to today.

    NOT an authorization primitive. Authorization additionally depends on
    role and on the legacy no-Task-row path; see the module docstring.
    """
    from sqlalchemy import func, false

    if user is None:
        return false()
    if read_fk_enabled():
        return model.assigned_user_id == user.id
    label = (user.display_label() or "").strip().lower()
    if not label:
        return false()
    return func.lower(func.trim(model.assigned_staff)) == label
