"""Phase RC2.3A — tenant-scoped staff directory, sourced from User.

DORMANT. Nothing in the application calls this module in the Expand phase.
app/data/staff_master.json remains the source of truth and every one of its 16
consumers is untouched. This exists so the later phases are a repointing
exercise against a tested abstraction rather than 16 separate rewrites.

Why this module exists
----------------------
staff_master.json is a single global file with no tenant dimension: every
tenant reads and writes the same staff, and the file ships Oxford's staff to
every tenant on deploy (RC2.2). RC2.2C ratified User.id as the canonical staff
identity — User already carries tenant_id, role and is_active, and the tenant
portal (/tenant/staff) already creates staff as User rows.

Shape compatibility is deliberate
---------------------------------
as_registry() returns EXACTLY the shape load_staff_registry() returns:

    {STAFF_CODE: {"display_name": str, "role": str, "active": bool}}

That is what lets a consumer migrate by changing one line rather than its
logic. The dict key is derived from username, NOT stored: RC2.2C declined to
promote staff_code to a real identity because production codes (ANJU, KIRAN,
NISHA) are simply uppercased names carrying no information the name lacks.

Tenant is ALWAYS explicit
-------------------------
Every function takes tenant_id and refuses to guess. An unresolvable tenant
returns empty rather than falling back — the fail-closed contract established
in Phase 14B.3 after tenant_query() was found returning unfiltered rows.

Framework independence
----------------------
No flask import. Callers pass tenant_id; this module never reads request,
session or current_user. Matches sales_pipeline_service and
sales_transition_service.
"""
import logging

logger = logging.getLogger(__name__)


def _code_base(user) -> str:
    """Uppercased username — the legacy code shape (ANJU / KIRAN / NISHA)."""
    return (user.username or "").strip().upper() or f"USER{user.id}"


def _assign_codes(users):
    """Collision-free registry codes for a set of users. Returns {user.id: code}.

    Phase RC2.2D Stage 0 — resolves compatibility issue I3.

    THE DEFECT THIS REPLACES
    ------------------------
    The previous implementation was a bare `username.upper()` used directly as
    a dict key. Usernames are unique per tenant CASE-SENSITIVELY
    (uq_users_tenant_username), so 'anju2' and 'ANJU2' are both legal in one
    tenant — and both uppercased to 'ANJU2'. The second overwrote the first in
    the dict, so a real staff member vanished from every CRM screen with no
    error. Silent loss of a person from a staff directory is exactly the class
    of failure this migration exists to end.

    THE DESIGN
    ----------
    Codes are assigned in ascending user.id order:

      * the FIRST user (lowest id) claiming a base keeps it unsuffixed, so
        stable codes never churn when a colliding user is added later;
      * every subsequent collision gets `BASE#<id>`, which is unique because
        user.id is unique, and deterministic because the ordering is.

    Properties that matter:
      deterministic  — same inputs always produce the same mapping, so a code
                       does not flip between requests
      total          — every user receives a code; none can be dropped
      injective      — two users can never share a code
      stable         — adding or removing a user never renames an existing
                       unsuffixed code

    A blank username degrades to USER<id> rather than an empty key.

    Collisions are logged: they are legal but almost always a data-entry
    accident, and an operator should be able to see one rather than wonder why
    a code looks odd.
    """
    codes, taken = {}, set()
    for user in sorted(users, key=lambda u: u.id):
        base = _code_base(user)
        if base not in taken:
            codes[user.id] = base
            taken.add(base)
            continue
        code = f"{base}#{user.id}"
        codes[user.id] = code
        taken.add(code)
        logger.warning(
            "staff_service: registry code collision on %r in tenant %s — "
            "user id=%s assigned %r instead (usernames are unique per tenant "
            "case-sensitively, so this is legal but usually accidental)",
            base, user.tenant_id, user.id, code)
    return codes


def _code_for(user, codes=None) -> str:
    """One user's registry code.

    Prefer _assign_codes() when building a whole registry — a single user
    cannot see the collisions it participates in.
    """
    if codes is not None and user.id in codes:
        return codes[user.id]
    return _code_base(user)


def _display_for(user) -> str:
    """Operator-facing name, falling back to username.

    ONE resolution rule for the whole system. normalize_staff_name() applies
    .title(), which MUTATES identity — that is how 'Anju_display' became a
    permanent phantom staff member in every dashboard, and how the literal
    fallback 'Admin' collides with the title-cased real user 'admin'. This
    helper never transforms the stored value.
    """
    return user.display_label()


def list_staff(tenant_id, active_only=False, include_admins=False):
    """All staff Users for one tenant, ordered by display label.

    Returns [] for a missing tenant_id — fail closed, never unscoped.

    active_only     — mirrors the registry's `active` filter, which is what
                      gates the assignment dropdowns today.
    include_admins  — the registry stores a role per entry and production
                      contains ADMIN entries, so callers that populate an
                      assignment picker may need them. Default False keeps the
                      common case (STAFF only) explicit.
    """
    from app.models import User

    if not tenant_id:
        logger.warning("staff_service.list_staff called without tenant_id — "
                       "returning empty (fail-closed)")
        return []

    roles = ("STAFF", "ADMIN") if include_admins else ("STAFF",)
    q = User.query.filter(User.tenant_id == tenant_id, User.role.in_(roles))
    if active_only:
        q = q.filter(User.is_active.is_(True))
    return sorted(q.all(), key=lambda u: _display_for(u).lower())


def as_registry(tenant_id, include_admins=False):
    """Tenant's staff in load_staff_registry()'s exact shape.

    The drop-in replacement for the global file. Consumers that do
    `registry.items()` / `data.get("active")` / `data.get("display_name")`
    keep working unchanged.

    Phase RC2.2D Stage 0 — resolves compatibility issue I1.

    include_admins now defaults FALSE. It previously defaulted True on the
    reasoning that staff_master.json stores a role per entry rather than
    filtering by one. That reasoning was wrong in practice: every entry in the
    production file is role=STAFF, so defaulting True would have injected the
    tenant's ADMIN account into the registry the moment a consumer switched
    over — putting 'admin' into every assignment dropdown and taking the CRM
    "Staff Active" card from 3 to 4. False reproduces today's file exactly.

    Callers that genuinely want admins must now ask for them.
    """
    users = list_staff(tenant_id, include_admins=include_admins)
    codes = _assign_codes(users)
    return {
        _code_for(u, codes): {
            "display_name": _display_for(u),
            "role": u.role,
            "active": bool(u.is_active),
        }
        for u in users
    }


def active_display_names(tenant_id, include_admins=False):
    """Active staff display names, sorted — the assignment dropdown's source.

    Mirrors today's
        [d["display_name"] for c, d in registry.items() if d.get("active")]

    Phase RC2.2D Stage 0 — resolves compatibility issue I2. include_admins is
    now a parameter and defaults FALSE; it was previously hardcoded True, which
    would have added the tenant's ADMIN account to every assignment dropdown.
    """
    return sorted(_display_for(u)
                  for u in list_staff(tenant_id, active_only=True,
                                      include_admins=include_admins))


def resolve_code(tenant_id, code, include_admins=False):
    """The User behind a registry code, within ONE tenant. None if unknown.

    Phase RC2.2D Stage 2. The inverse of _assign_codes(), and it lives HERE
    rather than in the route on purpose: a caller that re-derived the code
    itself would have to reproduce the `BASE#<id>` collision suffix, and the
    two implementations would drift the moment either changed. There is one
    code-assignment rule and this is the only reader of it.

    Tenant-scoped, so a code from another tenant resolves to nothing rather
    than to that tenant's user — this is the lookup the write path uses to
    decide WHICH ROW TO MUTATE, so a fail-open here would be a cross-tenant
    write, not merely a disclosure.
    """
    if not tenant_id or not (code or "").strip():
        return None
    users = list_staff(tenant_id, include_admins=include_admins)
    codes = _assign_codes(users)
    wanted = code.strip()
    for user in users:
        if codes.get(user.id) == wanted:
            return user
    return None


def resolve(tenant_id, name):
    """Resolve a staff NAME to its User within one tenant, or None.

    The bridge between the string world and the id world, used by the later
    backfill and dual-write phases. Matching is case-insensitive and
    whitespace-trimmed against BOTH display_name and username, because
    assigned_staff currently holds display names while ownership checks compare
    usernames — production has both ('Anju' and 'kiran' on leads).

    Returns None rather than guessing when the name is unknown. Production
    contains 'Anju_display', which resolves to nothing; a migration that
    guessed there would silently reassign a real customer's lead.

    Ambiguity is refused, not resolved: if two users in the tenant match, this
    returns None and logs. That cannot happen via username (unique per tenant)
    but display_name carries no uniqueness constraint.
    """
    from app.models import User

    if not tenant_id:
        return None
    needle = (name or "").strip().lower()
    if not needle:
        return None

    matches = [
        u for u in User.query.filter(User.tenant_id == tenant_id).all()
        if (u.username or "").strip().lower() == needle
        or (u.display_name or "").strip().lower() == needle
    ]
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "staff_service.resolve: %r is ambiguous in tenant %s (%d matches) "
            "— refusing to guess", name, tenant_id, len(matches))
        return None
    return matches[0]


def resolve_id(tenant_id, name):
    """resolve() returning just the user id, or None."""
    user = resolve(tenant_id, name)
    return user.id if user is not None else None


def display_for_id(tenant_id, user_id):
    """Display label for a user id, scoped to the tenant, or None.

    Tenant-scoped on purpose: an id from another tenant must not render a name
    here, or the read path would reintroduce the cross-tenant disclosure this
    whole migration exists to remove.
    """
    from app.models import User

    if not tenant_id or not user_id:
        return None
    user = User.query.filter(User.id == user_id,
                             User.tenant_id == tenant_id).first()
    return _display_for(user) if user is not None else None
