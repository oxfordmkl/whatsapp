"""Phase RC2.5.4a: tenant-scoped ADMIN listing of knowledge rows (read-only).

WHY THIS IS A SEPARATE MODULE FROM knowledge_service
-----------------------------------------------------
knowledge_service serves the AI PROMPT path. Its bounds exist to protect the
prompt: MAX_ITEMS=8 is a REAL ceiling (RC2.5.3b made it min(), precisely so a
caller cannot bypass it), and it filters is_active=True because an inactive
row must never reach a customer.

Both of those are exactly WRONG for an admin screen:

  * Oxford has 18 knowledge rows. An admin "Courses" page calling
    fetch_knowledge(tenant_id, limit=100) would silently render 8 of them --
    the same class of silent truncation RC2.5.3b was created to fix.
  * An admin must SEE inactive rows in order to manage them; the prompt path
    structurally cannot return one.

So this module has its own, independent pagination bound. It deliberately
does NOT import or reuse MAX_ITEMS, and it does not touch knowledge_service
in any way -- the prompt path's behaviour is unchanged by this phase.

ISOLATION -- THE SAME CONTRACT, INDEPENDENTLY ENFORCED
-------------------------------------------------------
This is still a read path over tenant-owned data, so it repeats
knowledge_service's discipline rather than assuming it:

  * a falsy tenant_id returns an empty page -- it never means "no filter";
  * every query filters tenant_id == the caller's tenant;
  * get_knowledge() looks the row up BY (id AND tenant_id) together, so a
    guessed/enumerated row id belonging to another tenant simply is not
    found -- it never 403s in a way that confirms the row exists;
  * both entry points re-verify ownership of what they are about to return,
    as defence in depth against a future refactor loosening the filter.

The caller must pass a tenant_id resolved from the authenticated session
(_get_current_tenant()), never one taken from a query string, form field or
URL path. Routes are responsible for that; this module cannot verify the
provenance of what it is handed, which is precisely why the ownership
re-check below is not redundant.

READ-ONLY: this module contains no create, update or delete. RC2.5.4a is a
listing foundation only.
"""
import json
import logging

logger = logging.getLogger(__name__)

# Independent of knowledge_service.MAX_ITEMS by design -- see module
# docstring. This bounds ONE PAGE of an admin table, not a prompt payload,
# so it is a UI-ergonomics number, not a token-budget one.
DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 100


def _admin_base_query(tenant_id, kind=None):
    """Every admin read starts here. The tenant filter is not optional.

    Unlike knowledge_service._base_query() this does NOT filter on
    is_active -- an admin must be able to see and manage inactive rows.
    """
    from app.models import TenantKnowledge

    q = TenantKnowledge.query.filter(TenantKnowledge.tenant_id == tenant_id)
    if kind:
        q = q.filter(TenantKnowledge.kind == kind)
    return q.order_by(TenantKnowledge.sort_order.asc(),
                      TenantKnowledge.id.asc())


def list_knowledge(tenant_id, kind=None, page=1, per_page=DEFAULT_PER_PAGE):
    """Return one page of this tenant's knowledge rows, active and inactive.

    Returns a dict: {rows, page, per_page, total, pages, has_prev, has_next}.
    Never raises -- any failure degrades to an empty page, matching the
    fail-open discipline used throughout this service layer. An admin screen
    showing "no rows" on an outage is safe; a 500 is not.
    """
    empty = {"rows": (), "page": 1, "per_page": DEFAULT_PER_PAGE,
             "total": 0, "pages": 0, "has_prev": False, "has_next": False}

    if not tenant_id:
        # Explicitly NOT "list everything" -- a falsy tenant_id is a caller
        # bug or an unauthenticated path; either way it lists nothing.
        return dict(empty)

    # Independent bound. A caller asking for per_page=99999 is clamped here,
    # exactly as the prompt path clamps limit -- but to this module's own
    # ceiling, not the prompt's.
    try:
        per_page = min(max(1, int(per_page)), MAX_PER_PAGE)
        page = max(1, int(page))
    except (TypeError, ValueError):
        per_page, page = DEFAULT_PER_PAGE, 1

    try:
        q = _admin_base_query(tenant_id, kind)
        total = q.count()
        rows = q.limit(per_page).offset((page - 1) * per_page).all()
    except Exception:
        logger.exception(
            "[knowledge_admin] listing failed for tenant=%s -- empty page",
            tenant_id
        )
        return dict(empty)

    # Defence in depth: prove every row we are about to hand back is this
    # tenant's, before it reaches a template.
    leaked = [r for r in rows if r.tenant_id != tenant_id]
    if leaked:
        logger.error(
            "[knowledge_admin] ISOLATION VIOLATION: listing for tenant=%s "
            "returned %d row(s) belonging to another tenant -- discarding",
            tenant_id, len(leaked)
        )
        return dict(empty)

    pages = (total + per_page - 1) // per_page if total else 0
    return {
        "rows": tuple(rows),
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": pages,
        "has_prev": page > 1,
        "has_next": page < pages,
    }


def get_knowledge(tenant_id, row_id):
    """Return one row owned by this tenant, or None.

    Looked up by (id AND tenant_id) TOGETHER: another tenant's row id is
    simply not found, so the route renders an ordinary 404 and never reveals
    that the id exists elsewhere. Returns None (not an exception) for a
    missing row, a wrong-tenant row, a non-integer id, or any DB failure.
    """
    if not tenant_id or row_id is None:
        return None

    try:
        row_id = int(row_id)
    except (TypeError, ValueError):
        return None

    try:
        from app.models import TenantKnowledge
        row = TenantKnowledge.query.filter(
            TenantKnowledge.id == row_id,
            TenantKnowledge.tenant_id == tenant_id,
        ).first()
    except Exception:
        logger.exception(
            "[knowledge_admin] get failed for tenant=%s row=%s -- returning none",
            tenant_id, row_id
        )
        return None

    if row is None:
        return None

    # Defence in depth: the filter above should make this impossible.
    if row.tenant_id != tenant_id:
        logger.error(
            "[knowledge_admin] ISOLATION VIOLATION: get for tenant=%s "
            "returned a row owned by another tenant -- discarding", tenant_id
        )
        return None

    return row


def parse_attributes(row):
    """Parse one row's attributes JSON for display. Never raises.

    Read-only helper for the admin templates. Deliberately returns the RAW
    structure including legacy_payment_url: this is the internal admin
    surface, and the template is responsible for labelling that field as
    hidden from AI/customers. knowledge_service._flatten_attrs() remains the
    thing that keeps it out of the PROMPT -- that exclusion is untouched by
    this phase and is not weakened by showing the value to an authenticated
    tenant admin who owns it.
    """
    try:
        parsed = json.loads(row.attributes or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        logger.warning(
            "[knowledge_admin] malformed attributes JSON on knowledge id=%s "
            "-- showing empty", getattr(row, "id", "?")
        )
        return {}
