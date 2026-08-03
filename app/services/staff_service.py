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


def _code_for(user) -> str:
    """Registry-style key for a user.

    Derived, never stored. Mirrors how staff_master.json keys look today
    (ANJU / KIRAN / NISHA) so consumers keying off the code keep working.
    """
    return (user.username or "").strip().upper()


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


def as_registry(tenant_id, include_admins=True):
    """Tenant's staff in load_staff_registry()'s exact shape.

    The drop-in replacement for the global file. Consumers that do
    `registry.items()` / `data.get("active")` / `data.get("display_name")`
    keep working unchanged.

    include_admins defaults True because the file it replaces stores a `role`
    per entry rather than filtering by it.
    """
    return {
        _code_for(u): {
            "display_name": _display_for(u),
            "role": u.role,
            "active": bool(u.is_active),
        }
        for u in list_staff(tenant_id, include_admins=include_admins)
    }


def active_display_names(tenant_id):
    """Active staff display names, sorted — the assignment dropdown's source.

    Mirrors today's
        [d["display_name"] for c, d in registry.items() if d.get("active")]
    """
    return sorted(_display_for(u)
                  for u in list_staff(tenant_id, active_only=True,
                                      include_admins=True))


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
