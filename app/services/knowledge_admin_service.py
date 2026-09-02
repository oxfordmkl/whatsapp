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

MUTATION (RC2.5.4b)
--------------------
RC2.5.4a was read-only. RC2.5.4b adds create / update / activation-toggle,
and nothing else -- there is deliberately no hard-delete anywhere in this
module. Deactivation is the only removal: an inactive row stays visible to
its owner and stops reaching the AI, which is reversible; a DELETE is not.

Every mutation follows the same five steps, in this order:
  1. the ROUTE resolves tenant_id from the authenticated session;
  2. validate_payload() checks the input and returns errors, never raises;
  3. ownership is verified by looking the row up by (id AND tenant_id);
  4. one atomic transaction;
  5. commit only that row.

Two write-side rules that are not obvious and are separately tested:

  * legacy_payment_url is NEVER writable through this module. It is carried
    over verbatim from the stored row on every update. RC2.5.3a-K keeps it
    out of the PROMPT; this keeps it out of the WRITE path, so a crafted
    form post cannot introduce or alter one.
  * updates MERGE into the stored attributes rather than replacing them.
    A row's regulatory components, historical_alias, or any future key an
    older phase wrote survive an edit made through a form that does not
    know about them. Replacing wholesale would silently destroy data.

PRICING: base_price, registration fee, tuition fee and any offer are stored
as SEPARATE declared facts. Nothing here derives one from another -- the
same rule the prompt-side renderer follows.
"""
import json
import logging
import re

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


# ═══ RC2.5.4b: validation ═══════════════════════════════════════════════════

# Mirrors knowledge_service's advisory KIND_* set. Duplicated deliberately
# rather than imported: importing knowledge_service here would couple the
# admin write path to the prompt path, which the RC2.5.4a separation exists
# to prevent (and which an RC2.5.4a test asserts). `kind` remains a free
# string in the SCHEMA -- this is a UI-level allow-list, not a DB constraint,
# so a future vertical still needs no migration.
ALLOWED_KINDS = ("course", "faq", "policy", "product", "service")

# Column limits from the model -- validated here so a too-long value is a
# friendly error, not a database exception.
MAX_TITLE_LEN = 200
MAX_DURATION_LEN = 100
MAX_URL_LEN = 500

# Anything not http/https is rejected outright. This is what stops
# javascript:, data:, file: and similar from being stored and later rendered
# to a tenant admin or fed to the AI.
_URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)

# NEVER writable through this module. See the module docstring.
_NON_WRITABLE_ATTR_KEYS = frozenset({"legacy_payment_url"})


def _clean_money(raw, field, errors):
    """Optional non-negative number, or None. Rejects negatives and junk."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        errors.append(f"{field} must be a number.")
        return None
    if value < 0:
        errors.append(f"{field} cannot be negative.")
        return None
    # Keep whole numbers as int so the rendered prompt says "19540", not
    # "19540.0" -- matching how the RC2.5.3a-K backfill stored them.
    return int(value) if value == int(value) else value


def validate_payload(form):
    """Validate one create/edit submission. Returns (cleaned, errors).

    Never raises and never touches the database. All checks are server-side;
    nothing here trusts HTML validation. `errors` is a list of human-readable
    strings -- empty means valid.
    """
    errors = []
    get = form.get if hasattr(form, "get") else (lambda k, d=None: form.get(k, d))

    title = (get("title") or "").strip()
    if not title:
        errors.append("Title is required.")
    elif len(title) > MAX_TITLE_LEN:
        errors.append(f"Title must be {MAX_TITLE_LEN} characters or fewer.")

    kind = (get("kind") or "").strip().lower()
    if kind not in ALLOWED_KINDS:
        errors.append(f"Type must be one of: {', '.join(ALLOWED_KINDS)}.")

    body = (get("body") or "").strip() or None

    sort_raw = (get("sort_order") or "").strip()
    sort_order = 0
    if sort_raw:
        try:
            sort_order = int(sort_raw)
        except (TypeError, ValueError):
            errors.append("Display order must be a whole number.")
        else:
            if sort_order < 0:
                errors.append("Display order cannot be negative.")

    duration = (get("duration") or "").strip() or None
    if duration and len(duration) > MAX_DURATION_LEN:
        errors.append(f"Duration must be {MAX_DURATION_LEN} characters or fewer.")

    currency = (get("currency") or "").strip() or None
    base_price = _clean_money(get("base_price"), "Price", errors)

    payment_url = (get("payment_url") or "").strip() or None
    if payment_url:
        if len(payment_url) > MAX_URL_LEN:
            errors.append(f"Payment link must be {MAX_URL_LEN} characters or fewer.")
        elif not _URL_RE.match(payment_url):
            errors.append("Payment link must be a valid http:// or https:// URL.")

    # Regulatory fee components. Each is an INDEPENDENT declared fact --
    # nothing here sums them into base_price or derives one from another.
    components = []
    for field, label, ctype in (
        ("registration_fee", "Registration Fee", "registration_fee"),
        ("tuition_fee", "Maximum Tuition Fee to ATC", "max_tuition_fee"),
        ("concession", "Mandatory Tuition Fee Concession", "concession"),
        ("net_tuition_fee", "Net Tuition Fee to ATC", "net_tuition_fee"),
        ("exam_fee", "Examination Fee", "exam_fee"),
    ):
        amount = _clean_money(get(field), label, errors)
        if amount is not None:
            components.append({"type": ctype, "label": label, "amount": amount})

    cleaned = {
        "title": title,
        "kind": kind,
        "body": body,
        "sort_order": sort_order,
        "duration": duration,
        "currency": currency,
        "base_price": base_price,
        "payment_url": payment_url,
        "components": components,
    }
    return cleaned, errors


def _merge_attributes(existing, cleaned):
    """Merge validated form values INTO the stored attributes.

    Deliberately a merge, not a replace: a row's offers, historical_alias,
    regulatory.source/as_of, or any key written by an earlier phase must
    survive an edit submitted through a form that has no field for it.

    legacy_payment_url is carried over from `existing` and can never arrive
    from `cleaned` -- validate_payload() does not produce it, and the
    explicit strip below means even a future bug that added it would not
    write it.
    """
    attrs = dict(existing) if isinstance(existing, dict) else {}

    commercial = dict(attrs.get("commercial") or {})
    regulatory = dict(attrs.get("regulatory") or {})

    if cleaned["duration"] is not None:
        attrs["duration"] = cleaned["duration"]
    else:
        attrs.pop("duration", None)

    if cleaned["currency"] is not None:
        commercial["currency"] = cleaned["currency"]
    else:
        commercial.pop("currency", None)

    if cleaned["base_price"] is not None:
        commercial["base_price"] = cleaned["base_price"]
    else:
        commercial.pop("base_price", None)

    # An empty submission clears the ACTIVE link; it never touches the legacy
    # one, which is preserved untouched below.
    commercial["payment_url"] = cleaned["payment_url"]

    # Preserve offers exactly as stored -- RC2.5.4b has no offer editor, so
    # it must not silently drop them.
    if "offers" not in commercial:
        commercial["offers"] = []

    if cleaned["components"]:
        regulatory["components"] = cleaned["components"]
    elif "components" in regulatory:
        regulatory["components"] = []

    if commercial:
        attrs["commercial"] = commercial
    if regulatory:
        attrs["regulatory"] = regulatory

    # Belt and braces: no non-writable key may originate from this merge.
    # Whatever was stored is restored verbatim; anything else is stripped.
    for key in _NON_WRITABLE_ATTR_KEYS:
        stored_commercial = (existing or {}).get("commercial") or {}
        if key in stored_commercial:
            attrs.setdefault("commercial", {})[key] = stored_commercial[key]
        elif key in attrs.get("commercial", {}):
            attrs["commercial"].pop(key, None)
        attrs.pop(key, None)

    return attrs


# ═══ RC2.5.4b: mutations ════════════════════════════════════════════════════

def create_knowledge(tenant_id, form):
    """Create one knowledge row for this tenant. Returns (row, errors).

    tenant_id MUST come from the authenticated session. Nothing in `form` is
    consulted for tenant ownership.
    """
    if not tenant_id:
        return None, ["No tenant associated with your account."]

    cleaned, errors = validate_payload(form)
    if errors:
        return None, errors

    from app.extensions import db
    from app.models import TenantKnowledge

    try:
        row = TenantKnowledge(
            tenant_id=tenant_id,                      # session-derived only
            kind=cleaned["kind"],
            title=cleaned["title"],
            body=cleaned["body"],
            attributes=json.dumps(_merge_attributes({}, cleaned)),
            is_active=True,
            sort_order=cleaned["sort_order"],
        )
        db.session.add(row)
        db.session.commit()
        return row, []
    except Exception:
        db.session.rollback()
        logger.exception(
            "[knowledge_admin] create failed for tenant=%s", tenant_id
        )
        return None, ["Could not save. Please try again."]


def update_knowledge(tenant_id, row_id, form):
    """Update one row OWNED by this tenant. Returns (row, errors).

    Ownership is the lookup boundary: get_knowledge() resolves by
    (id AND tenant_id), so another tenant's row is simply not found and the
    caller renders its ordinary 404.
    """
    if not tenant_id:
        return None, ["No tenant associated with your account."]

    row = get_knowledge(tenant_id, row_id)
    if row is None:
        return None, None          # None errors => "not found", not "invalid"

    cleaned, errors = validate_payload(form)
    if errors:
        return row, errors

    from app.extensions import db

    try:
        existing = parse_attributes(row)
        row.title = cleaned["title"]
        row.kind = cleaned["kind"]
        row.body = cleaned["body"]
        row.sort_order = cleaned["sort_order"]
        row.attributes = json.dumps(_merge_attributes(existing, cleaned))
        db.session.commit()
        return row, []
    except Exception:
        db.session.rollback()
        logger.exception(
            "[knowledge_admin] update failed for tenant=%s row=%s",
            tenant_id, row_id
        )
        return row, ["Could not save. Please try again."]


def set_active(tenant_id, row_id, active):
    """Deactivate or reactivate one row owned by this tenant.

    This is the ONLY removal mechanism in this module -- there is no hard
    delete. An inactive row remains visible to its owner and stops reaching
    the AI (knowledge_service filters is_active=True), which is reversible.
    Returns (row, errors); (None, None) means not found.
    """
    if not tenant_id:
        return None, ["No tenant associated with your account."]

    row = get_knowledge(tenant_id, row_id)
    if row is None:
        return None, None

    from app.extensions import db

    try:
        row.is_active = bool(active)
        db.session.commit()
        return row, []
    except Exception:
        db.session.rollback()
        logger.exception(
            "[knowledge_admin] activation toggle failed for tenant=%s row=%s",
            tenant_id, row_id
        )
        return row, ["Could not update. Please try again."]
