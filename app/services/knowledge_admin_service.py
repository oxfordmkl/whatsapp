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
MAX_CODE_LEN = 32

# Phase RC2.5.4c-x-6a: the awarding body's formal course name, e.g.
# "Certificate in Word Processing and Data Entry Operator (CWPDE)". The
# longest in production is 75 characters; 200 matches MAX_TITLE_LEN, which is
# the closest existing analogue -- both are a single-line human name for the
# same row -- so this introduces no new limit convention.
MAX_OFFICIAL_NAME_LEN = 200

# Phase RC2.5.4c-x-6a: a CLOSED vocabulary, exactly the three values in
# production (11 x "10th and Above", 2 x "+2 & Above", 1 x "Any Degree").
#
# Deliberately a select rather than free text. Eligibility is an entry
# requirement -- the same class of claim prompt_composer tells the model never
# to invent ("Never invent prices, offers, guarantees, eligibility or legal
# claims") and constants.py annotates "always quote exactly -- never invent
# eligibility". Free text would let an admin type a regulatory claim in prose
# straight into the AI's context; a fixed list keeps the stored values
# identical to the ones already authored. Adding a fourth value is a
# deliberate vocabulary change, exactly as CATEGORIES is.
ELIGIBILITY_VALUES = ("10th and Above", "+2 & Above", "Any Degree")

# Anything not http/https is rejected outright. This is what stops
# javascript:, data:, file: and similar from being stored and later rendered
# to a tenant admin or fed to the AI.
_URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)

# Phase RC2.5.5b-1: course code charset. Deliberately narrow -- this value is
# a lookup key, not prose, and anything that could collide after trimming or
# case-folding would weaken the resolver's "exactly one match" guarantee.
_CODE_RE = re.compile(r"^[A-Z0-9_-]+$")

# Phase RC2.5.4c-x-5a: discovery keywords.
#
# These are matched as SUBSTRINGS of a customer's free text by
# catalogue_service.match_keyword, so they are short discovery phrases, not
# prose. The ceilings are set from what production actually holds (max 6
# keywords per course, longest single keyword 20 characters) with headroom,
# rather than invented: a course needing more than ten discovery terms is
# almost certainly trying to match on prose, which makes every OTHER course
# harder to reach.
MAX_KEYWORDS = 10
MAX_KEYWORD_LEN = 60

# Letters, digits, spaces, ampersand and hyphen. Wide enough for every value
# in production ("word processing", "tally prime", "dca fast track",
# "corporate accounting") and narrow enough to keep punctuation, quotes and
# angle brackets out of a field that is rendered to a tenant admin and fed to
# the AI. Deliberately NOT the code charset: a keyword is a human phrase.
_KEYWORD_RE = re.compile(r"^[a-z0-9 &-]+$")

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


def _clean_keywords(raw, errors):
    """Comma-separated discovery phrases -> an ordered, de-duplicated list.

    Returns None for a blank submission, which the merge treats as "remove
    the key" (see _merge_attributes). Normalisation is strip + lowercase
    because catalogue_service.match_keyword lowercases the customer's text
    and then tests `keyword in text`: a stored "Web Design" would simply
    never match anything, silently.

    Order is preserved rather than sorted -- the tenant authored it, and
    match_keyword's longest-wins rule makes order irrelevant to matching, so
    reordering would only make the admin's own field look shuffled.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    out = []
    for part in text.split(","):
        kw = part.strip().lower()
        if not kw:                       # "a,,b" and trailing commas
            continue
        if kw in out:                    # de-duplicate, keep first position
            continue
        if len(kw) > MAX_KEYWORD_LEN:
            errors.append(
                f"Keyword \"{kw[:30]}...\" is too long "
                f"({MAX_KEYWORD_LEN} characters or fewer).")
            continue
        if not _KEYWORD_RE.match(kw):
            errors.append(
                f"Keyword \"{kw}\" may use only letters, digits, spaces, "
                "'&' and '-'.")
            continue
        out.append(kw)

    if len(out) > MAX_KEYWORDS:
        errors.append(f"Use at most {MAX_KEYWORDS} keywords.")
        return None
    return out or None


def _clean_official_name(raw, errors):
    """The awarding body's formal course name, or None for a blank field.

    Whitespace is collapsed rather than merely stripped: these values are
    pasted from fee cards and prospectuses, where a stray double space or a
    newline is common, and the value is rendered into the AI prompt by
    knowledge_service._flatten_attrs as a single line.

    This is DESCRIPTIVE metadata, never identity. Course identity is
    commercial.code, which get_course(), resolve_legacy_name() and the payment
    resolver all key on; nothing here touches any of them.
    """
    if raw is None:
        return None
    text = " ".join(str(raw).split())
    if not text:
        return None
    if len(text) > MAX_OFFICIAL_NAME_LEN:
        errors.append(
            f"Official name must be {MAX_OFFICIAL_NAME_LEN} characters or fewer.")
        return None
    return text


def _clean_eligibility(raw, errors):
    """One value from the CLOSED ELIGIBILITY_VALUES vocabulary, or None.

    Rejected rather than dropped when unknown: silently discarding it would
    leave an admin looking at a saved course whose eligibility never appeared,
    and the value goes to the AI as an entry-requirement claim.

    Matched case-insensitively on the trimmed input but STORED in the
    vocabulary's own canonical casing, so the fourteen values already authored
    keep their exact byte shape.
    """
    if raw is None:
        return None
    text = " ".join(str(raw).split())
    if not text:
        return None
    for allowed in ELIGIBILITY_VALUES:
        if text.lower() == allowed.lower():
            return allowed
    errors.append(
        f"Eligibility must be one of: {', '.join(ELIGIBILITY_VALUES)}.")
    return None


def _clean_categories(values, errors):
    """Goal categories -> a de-duplicated list drawn from a CLOSED vocabulary.

    catalogue_service.CATEGORIES is the single source of truth and is imported
    lazily so this module keeps its existing import surface. A value outside
    it is REJECTED rather than dropped: courses_for_category() would return
    nothing for it, so silently accepting one would leave an admin looking at
    a saved category that puts their course in no menu at all.
    """
    if values is None:
        return None
    if isinstance(values, str):
        # A scalar arrives when a form sends one value, or from a plain dict
        # in a unit test. Same comma semantics as keywords so both entry
        # points behave identically.
        values = [v for v in values.split(",")]
    try:
        from app.services.catalogue_service import CATEGORIES
    except Exception:                                  # pragma: no cover
        logger.exception("[knowledge_admin] category vocabulary unavailable")
        return None

    out = []
    for v in values:
        cat = str(v or "").strip().lower()
        if not cat:
            continue
        if cat not in CATEGORIES:
            errors.append(
                f"Category \"{cat}\" is not one of: {', '.join(CATEGORIES)}.")
            continue
        if cat not in out:
            out.append(cat)
    return out or None


def validate_payload(form):
    """Validate one create/edit submission. Returns (cleaned, errors).

    Never raises and never touches the database. All checks are server-side;
    nothing here trusts HTML validation. `errors` is a list of human-readable
    strings -- empty means valid.
    """
    errors = []
    get = form.get if hasattr(form, "get") else (lambda k, d=None: form.get(k, d))

    # Phase RC2.5.4c-x-5a: categories arrive from a <select multiple>, so the
    # form carries SEVERAL values under one name. request.form is a
    # MultiDict and .get() returns only the FIRST -- reading it that way
    # would silently store one category and drop the rest. getlist() is used
    # when the form offers it, with a scalar fallback for a plain dict (unit
    # tests, and any future non-MultiDict caller).
    getlist = getattr(form, "getlist", None)

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

    # Phase RC2.5.5b-1: stable course code.
    #
    # This is the join key the tenant payment resolver matches on. Title is
    # not usable for that: it is free text a tenant admin may reword at any
    # time, and the deterministic bot flow needs a key that survives a rename.
    # Uppercased on the way in so "pgdca" and "PGDCA" are the same course --
    # case drift must never produce two rows that both look like a match.
    code = (get("code") or "").strip().upper() or None
    if code:
        if len(code) > MAX_CODE_LEN:
            errors.append(f"Course code must be {MAX_CODE_LEN} characters or fewer.")
        elif not _CODE_RE.match(code):
            errors.append("Course code may use only letters, digits, hyphen "
                          "and underscore.")

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

    # Phase RC2.5.4c-x-5a. Both are TOP-LEVEL attributes, not commercial ones:
    # that is where catalogue_service._record_from_row already reads them
    # from, and where all sixteen authored courses already store them.
    keywords = _clean_keywords(get("keywords"), errors)
    raw_categories = (getlist("categories") if getlist is not None
                      else get("categories"))
    categories = _clean_categories(raw_categories, errors)

    # Phase RC2.5.4c-x-6a. Also TOP-LEVEL, alongside keywords/categories --
    # that is where all sixteen authored courses store them and where the
    # generic flattener picks them up for the prompt. Both are single-valued,
    # so neither needs getlist().
    official_name = _clean_official_name(get("official_name"), errors)
    eligibility = _clean_eligibility(get("eligibility"), errors)

    cleaned = {
        "title": title,
        "kind": kind,
        "body": body,
        "sort_order": sort_order,
        "duration": duration,
        "currency": currency,
        "base_price": base_price,
        "code": code,
        "payment_url": payment_url,
        "components": components,
        "keywords": keywords,
        "categories": categories,
        "official_name": official_name,
        "eligibility": eligibility,
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

    # Phase RC2.5.4c-x-5a: discovery keywords and goal categories.
    #
    # Both were READ by catalogue_service and written by nobody. All sixteen
    # authored courses carry them (from RC2.5.5c-2's data-only commit) and
    # they survived edits only incidentally, via the dict(existing) copy
    # above -- exactly the accident normal_total_fee lived on before
    # RC2.5.4c-x-2. A course created through this form got neither, so it was
    # unreachable by free text (match_keyword) and appeared in no goal
    # recommendation (courses_for_category). That is what this fixes.
    #
    # POP ON BLANK, and deliberately so -- the opposite of the price guard
    # three blocks below. Blanking a price would silently delete a
    # customer-facing money value the admin never saw a field for; blanking
    # keywords is a legible act with a visible effect, and leaving stale
    # keywords un-clearable would be the worse failure. It is the same
    # reasoning `code` records for itself: a stale value that keeps matching
    # is worse than no value. The asymmetry is intentional, not an
    # inconsistency, and both halves are pinned by tests.
    if cleaned["keywords"] is not None:
        attrs["keywords"] = list(cleaned["keywords"])
    else:
        attrs.pop("keywords", None)

    if cleaned["categories"] is not None:
        attrs["categories"] = list(cleaned["categories"])
    else:
        attrs.pop("categories", None)

    # Phase RC2.5.4c-x-6a: the awarding body's formal name and the entry
    # requirement. Both are AI-ONLY: no deterministic customer path reads
    # either -- official_name has no reader in app/ at all, and eligibility's
    # only source mentions are prompt guardrails -- so they reach a customer
    # solely through knowledge_service._flatten_attrs, which is untouched.
    #
    # Same pop-on-blank as keywords/categories, and for the same reason: both
    # are legible fields with a visible effect, and an un-clearable stale
    # eligibility would be the worse failure -- it is an entry-requirement
    # claim the AI may repeat. The RC2.5.4c-x-2 price guard below stays
    # asymmetric and untouched.
    #
    # DCA and DGSTP have no eligibility in production and are deliberately
    # left that way: this phase creates the capability, it does not populate
    # existing records.
    if cleaned["official_name"] is not None:
        attrs["official_name"] = cleaned["official_name"]
    else:
        attrs.pop("official_name", None)

    if cleaned["eligibility"] is not None:
        attrs["eligibility"] = cleaned["eligibility"]
    else:
        attrs.pop("eligibility", None)

    if cleaned["currency"] is not None:
        commercial["currency"] = cleaned["currency"]
    else:
        commercial.pop("currency", None)

    # Phase RC2.5.4c-x-2: the customer price is written to BOTH keys.
    #
    # Two fields hold one course's customer-facing total. This module wrote
    # only commercial.base_price; commercial.normal_total_fee had NO writer
    # anywhere in the application -- its sixteen production values came from
    # RC2.5.5c-2, a data-only commit that shipped no runnable writer. The
    # catalogue read path prefers normal_total_fee (RC2.5.5c-3) and falls
    # back to base_price only when it is absent (RC2.5.4c-x). So an edit
    # moved the admin's number while the customer kept being quoted the old
    # one -- PGDCA carried 19540 and 16000 simultaneously for ~17h.
    #
    # Writing both here makes the two fields incapable of diverging on any
    # row an admin touches. The value is the SUBMITTED price and nothing
    # else: never derived from the fee components (validate_payload keeps
    # those independent and test_base_price_is_never_derived_from_components
    # pins it), and never taken from either stored field, which would ignore
    # what the admin just typed.
    #
    # THE BLANK BRANCH IS DELIBERATELY ASYMMETRIC.
    #
    # A blank price pops base_price, as it always has. It must NOT pop
    # normal_total_fee. That field has never been touched by this merge, and
    # popping it would silently delete the customer-facing price from the
    # sixteen courses RC2.5.5c-2 authored -- a blank form field erasing what
    # a business charges. The asymmetry is the safety guard, not an
    # oversight: see test_blank_price_preserves_normal_total_fee and mutant
    # M5, which exists solely to keep this branch honest.
    #
    # The RC2.5.4c-x read fallback stays. It is what still serves a row
    # written before this phase (FST01) until its next edit, and it is what
    # makes this change reversible. Retiring it, and removing base_price
    # from storage, is a separate later decision.
    if cleaned["base_price"] is not None:
        commercial["base_price"] = cleaned["base_price"]
        commercial["normal_total_fee"] = cleaned["base_price"]
    else:
        commercial.pop("base_price", None)
        # normal_total_fee is deliberately NOT popped -- see above.

    # An empty submission CLEARS the code, exactly like payment_url. A stale
    # code left behind after an admin blanked the field would keep matching in
    # the resolver, which is the one outcome worse than not matching at all.
    if cleaned["code"] is not None:
        commercial["code"] = cleaned["code"]
    else:
        commercial.pop("code", None)

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
