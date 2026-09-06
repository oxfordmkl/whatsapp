"""Phase RC2.5.5c-3: the tenant-owned course catalogue for the runtime.

WHY THIS EXISTS
----------------
The deterministic flow read Oxford's hardcoded catalogue -- ALL_COURSES,
COURSE_FEES, GOAL_COURSES, KEYWORD_TO_COURSE, FULL_FEE_TABLE. A second tenant's
customer therefore browsed Oxford's courses at Oxford's prices. RC2.5.5c-2
authored the tenant-owned catalogue; this module is the read path for it.

Same shape as payment_link_service and tenant_identity_service before it: the
tenant-owned source already existed, and the deterministic path simply did not
read it.

FAIL-SAFE, NOT FAIL-CLOSED
---------------------------
Deliberately the opposite polarity from payment_link_service, and the contrast
is the point:

  * a missing payment URL must produce NO link, because a wrong link takes
    money to the wrong account;
  * a missing catalogue must still produce A CATALOGUE, because the
    alternative is a bot that cannot answer "what courses do you offer".

So every failure here -- no tenant, empty catalogue, DB error, malformed JSON
-- degrades to the platform default catalogue built from app.bot.constants.
That keeps an unconfigured tenant working exactly as it does today.

The ONE exception is a lookup for a SPECIFIC course that does not exist or is
inactive: that returns None so the caller can take its not-found path. It must
never silently substitute a different course.

IDENTITY IS commercial.code, NEVER POSITION OR TITLE
-----------------------------------------------------
Menu position shifts whenever a course is added, removed or reordered, and
titles get reworded. Both were used as identity before this phase. `code` is
stable by construction (RC2.5.5b-1) and is what payment resolution already
keys on, so the two agree for free.

Persisted ConversationState._course holds a DISPLAY NAME from the old
catalogue -- 48 live conversations at the time of writing. No migration is
authorised, so resolve_legacy_name() maps those names to codes explicitly.

PAYMENT IS NOT THIS MODULE'S BUSINESS
--------------------------------------
CourseRecord carries no payment URL and this module never reads one. Payment
stays exactly where RC2.5.5b put it: resolve_payment_url(tenant_id, code),
fail-closed. Keeping the two apart is what stops a catalogue bug from becoming
a money bug.
"""
import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

KIND = "course"
_CODE_RE = re.compile(r"^[A-Z0-9_-]+$")
_MAX_CODE_LEN = 32

# The four goal categories the navigation taxonomy already uses. Not extended
# here -- RC2.5.5c-2 assigned every course within this set.
CATEGORIES = ("job", "business", "accounting", "basic")

# Terms that legitimately name more than one course. The customer is asked to
# choose; nothing is picked for them. Kept EXPLICIT rather than inferred: a
# heuristic that guessed at ambiguity would eventually guess wrong, and the
# failure mode is quoting the wrong price for the wrong course.
AMBIGUOUS_TERMS = {
    "dca": ("DCA", "DCA-REGULAR"),
}

# Old display names -> stable code, for conversations persisted before c-3.
# ONLY unambiguous identities appear here.
#
# "GST & Payroll" resolves to DGSTP so an in-flight conversation still works,
# but everything the customer then sees is DGSTP's current content. The
# Payroll claim is NOT source-backed (RC2.5.5c-2) and is never reproduced.
LEGACY_NAME_TO_CODE = {
    "pgdca": "PGDCA",
    "aidm digital marketing": "AIDM",
    "sap financial accounting": "PDCFA",
    "python programming": "PYTHON",
    "gst & payroll": "DGSTP",
    "dca fast track": "DCA",
    "computer teacher training": "CTTC",
    "corporate business accounting": "CORPORATE-ACCOUNTING",
    "word processing & data entry": "CWPDE",
    "professional web designing": "PDWD",
}


@dataclass(frozen=True)
class CourseRecord:
    """One customer-facing course. Deliberately carries NO payment URL."""
    code: str
    title: str
    body: str = ""
    duration: str = ""
    categories: tuple = ()
    keywords: tuple = ()
    normal_total_fee: object = None
    registration_fee: object = None
    net_tuition_fee: object = None
    exam_fee: object = None
    emi_available: bool = False
    offers: tuple = ()
    sort_order: int = 0
    is_default: bool = False        # True => came from the platform fallback


@dataclass(frozen=True)
class Match:
    """Result of matching free text against the catalogue."""
    kind: str                       # "exact" | "ambiguous" | "none"
    course: object = None           # CourseRecord when kind == "exact"
    candidates: tuple = field(default_factory=tuple)


def normalise_code(code):
    """Canonical course code, or None when it cannot be a key."""
    if code is None:
        return None
    text = str(code).strip().upper()
    if not text or len(text) > _MAX_CODE_LEN or not _CODE_RE.match(text):
        return None
    return text


def _money(components, kind):
    for c in components or ():
        if isinstance(c, dict) and c.get("type") == kind:
            return c.get("amount")
    return None


def _record_from_row(row):
    """Build a CourseRecord from a TenantKnowledge row, or None."""
    try:
        attrs = json.loads(row.attributes or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(attrs, dict):
        return None
    commercial = attrs.get("commercial")
    if not isinstance(commercial, dict):
        return None
    code = normalise_code(commercial.get("code"))
    if not code:
        # A course with no stable code is unaddressable. Skipping it is
        # correct: the alternative is falling back to position or title,
        # which is the defect this phase removes.
        return None
    regulatory = attrs.get("regulatory") or {}
    components = regulatory.get("components") if isinstance(regulatory, dict) else None
    cats = attrs.get("categories")
    kws = attrs.get("keywords")
    offers = commercial.get("offers")
    return CourseRecord(
        code=code,
        title=(row.title or code),
        body=(row.body or ""),
        duration=str(attrs.get("duration") or ""),
        categories=tuple(c for c in (cats or ()) if isinstance(c, str)),
        keywords=tuple(k for k in (kws or ()) if isinstance(k, str)),
        normal_total_fee=commercial.get("normal_total_fee"),
        registration_fee=_money(components, "registration_fee"),
        net_tuition_fee=_money(components, "net_tuition_fee"),
        exam_fee=_money(components, "exam_fee"),
        emi_available=bool(commercial.get("emi_available")),
        offers=tuple(o for o in (offers or ()) if isinstance(o, dict)),
        sort_order=int(getattr(row, "sort_order", 0) or 0),
    )


def _default_catalogue():
    """The platform default catalogue, from app.bot.constants.

    Used for an unconfigured tenant, an empty catalogue, or a lookup failure.
    Imported lazily so this module stays importable in the stubbed harnesses
    and so constants are touched only on the fallback path.
    """
    try:
        from app.bot.constants import ALL_COURSES, COURSE_FEES, GOAL_COURSES, KEYWORD_TO_COURSE
    except Exception:
        logger.exception("[catalogue] default catalogue unavailable")
        return ()

    cats_by_index = {}
    for goal, entries in GOAL_COURSES.items():
        for idx, _label, _dur, _fee in entries:
            cats_by_index.setdefault(idx, []).append(goal)
    kws_by_index = {}
    for kw, idx in KEYWORD_TO_COURSE.items():
        kws_by_index.setdefault(idx, []).append(kw)

    out = []
    for order, (idx, (name, card)) in enumerate(
            sorted(ALL_COURSES.items(), key=lambda kv: int(kv[0])), start=1):
        fee, duration = COURSE_FEES.get(name, ("", ""))
        code = normalise_code(idx) or f"D{idx}"
        out.append(CourseRecord(
            code=code, title=name, body=card, duration=duration,
            categories=tuple(cats_by_index.get(idx, ())),
            keywords=tuple(kws_by_index.get(idx, ())),
            normal_total_fee=fee, emi_available=False,
            sort_order=order, is_default=True))
    return tuple(out)


def _tenant_rows(tenant_id):
    """Active course rows for this tenant, ordered. Raises on DB failure."""
    from app.models import TenantKnowledge
    return (TenantKnowledge.query
            .filter(TenantKnowledge.tenant_id == tenant_id,
                    TenantKnowledge.kind == KIND,
                    TenantKnowledge.is_active.is_(True))
            .order_by(TenantKnowledge.sort_order.asc(),
                      TenantKnowledge.id.asc())
            .all())


def list_courses(tenant_id):
    """This tenant's active catalogue, ordered. Never empty, never raises.

    Falls back to the platform default for a falsy tenant, an empty catalogue
    or any failure -- see the module docstring on why this is fail-SAFE.
    """
    if not tenant_id:
        # NOT an Oxford guess: the default catalogue is the platform's, and
        # tenant resolution has already happened upstream. Reaching here with
        # no tenant is a caller bug, logged as one.
        logger.error("[catalogue] no tenant_id -- serving platform default")
        return _default_catalogue()

    try:
        rows = _tenant_rows(tenant_id)
    except Exception:
        logger.exception(
            "[catalogue] lookup failed for tenant=%s -- platform default", tenant_id)
        return _default_catalogue()

    leaked = [r for r in rows if r.tenant_id != tenant_id]
    if leaked:
        logger.error(
            "[catalogue] ISOLATION VIOLATION: tenant=%s query returned %d "
            "foreign row(s) -- serving platform default", tenant_id, len(leaked))
        return _default_catalogue()

    records = tuple(r for r in (_record_from_row(x) for x in rows) if r)
    if not records:
        logger.info("[catalogue] tenant=%s has no usable courses -- default", tenant_id)
        return _default_catalogue()
    return records


def get_course(tenant_id, code):
    """One course by stable code, or None.

    None means "no such active course" and the caller must take its not-found
    path. It must NEVER substitute another course.
    """
    key = normalise_code(code)
    if not key:
        return None
    for c in list_courses(tenant_id):
        if c.code == key:
            return c
    return None


def courses_for_category(tenant_id, category):
    """Active courses in one goal category, in catalogue order."""
    key = (category or "").strip().lower()
    if key not in CATEGORIES:
        return ()
    return tuple(c for c in list_courses(tenant_id) if key in c.categories)


def resolve_legacy_name(tenant_id, name):
    """Map a persisted course DISPLAY NAME to a live CourseRecord, or None.

    Handles both the current titles and the pre-c-3 names still sitting in
    ConversationState. Never guesses: an unrecognised name returns None and
    the caller falls back to the menu.
    """
    text = (name or "").strip()
    if not text:
        return None

    courses = list_courses(tenant_id)
    lowered = text.lower()

    for c in courses:                                   # exact current title
        if c.title.lower() == lowered:
            return c
    code = normalise_code(text)                          # the code itself
    if code:
        for c in courses:
            if c.code == code:
                return c
    mapped = LEGACY_NAME_TO_CODE.get(lowered)            # explicit legacy alias
    if mapped:
        for c in courses:
            if c.code == mapped:
                return c
    return None


def match_keyword(tenant_id, text):
    """Match free text against catalogue keywords.

    Returns Match(kind="exact"|"ambiguous"|"none"). A term naming more than
    one course is reported AMBIGUOUS with its candidates so the caller can ask
    the customer; nothing is chosen for them.
    """
    low = (text or "").strip().lower()
    if not low:
        return Match(kind="none")

    courses = list_courses(tenant_id)
    by_code = {c.code: c for c in courses}

    for term, codes in AMBIGUOUS_TERMS.items():
        if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", low):
            present = tuple(by_code[c] for c in codes if c in by_code)
            if len(present) > 1:
                specific = [c for c in courses
                            for kw in c.keywords
                            if len(kw) > len(term) and kw in low]
                if not specific:
                    return Match(kind="ambiguous", candidates=present)

    hits = []
    for c in courses:
        for kw in c.keywords:
            if kw and kw in low:
                hits.append((len(kw), c))
                break
    if not hits:
        return Match(kind="none")
    hits.sort(key=lambda p: -p[0])            # longest keyword wins
    best = hits[0]
    tied = [c for n, c in hits if n == best[0]]
    if len(tied) > 1:
        return Match(kind="ambiguous", candidates=tuple(tied))
    return Match(kind="exact", course=best[1])


def format_money(value):
    """Render a catalogue amount for a customer.

    Tenant rows store integers (19540); the platform default catalogue stores
    already-formatted strings ("Rs.15,999"). One helper so the two cannot
    drift into two different-looking prices.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return f"₹{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def catalogue_index(tenant_id):
    """A concise one-line-per-course index of the WHOLE active catalogue.

    Consumed by prompt_composer so the AI knows every course a tenant sells.
    Deliberately identity + headline commercials only -- no bodies -- so the
    generic knowledge retrieval cap (knowledge_service.MAX_ITEMS) stays
    untouched and detailed bodies remain query-aware.

    Never contains a payment URL of any kind.

    Provenance-blind by design: prefer catalogue_index_with_provenance() when
    the caller needs to know WHOSE catalogue this is. Kept as-is so existing
    callers are unaffected.
    """
    return catalogue_index_with_provenance(tenant_id)[0]


def catalogue_index_with_provenance(tenant_id):
    """(entries, is_default_catalogue) -- the index plus WHOSE it is.

    Phase RC2.5.5c-5. `is_default` already existed on every CourseRecord but
    was never surfaced past this module, so prompt_composer had no way to
    tell a tenant's own catalogue from the platform fallback and labelled
    both "authoritative". Provenance is returned EXPLICITLY here rather than
    left to be inferred from the rendered text -- inferring it from titles,
    prices or codes is exactly the guessing this phase removes.

    One lookup serves both values, so making the prompt provenance-aware
    costs no extra query.
    """
    courses = list_courses(tenant_id)
    is_default = bool(courses) and all(c.is_default for c in courses)
    out = []
    for c in courses:
        bits = [c.code, c.title]
        if c.duration:
            bits.append(c.duration)
        if c.normal_total_fee is not None:
            # Phase RC2.5.5c-6a (F3). This interpolated the raw stored value,
            # the only one of eighteen fee-formatting sites in app/ that did
            # not go through format_money(). Tenant rows store an int and the
            # platform default stores a pre-formatted string, so the AI saw
            # "Total fee 19540" for every real tenant while only the fallback
            # looked right. format_money() passes the string through
            # unchanged, so the default catalogue's output does not move.
            bits.append(f"Total fee {format_money(c.normal_total_fee)}")
        bits.append("EMI available" if c.emi_available else "No EMI")
        out.append(" | ".join(str(b) for b in bits))
    return tuple(out), is_default
