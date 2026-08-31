"""Phase RC2.5.3a: tenant-scoped knowledge retrieval (prompt Layer 3).

THE ISOLATION CONTRACT
----------------------
This is a READ path whose output is injected into a live customer
conversation. RC2.4.x hardened WRITE paths; nothing there covers this. A
missing or wrong tenant filter here does not corrupt data -- it speaks another
tenant's prices out loud to a stranger, which is worse and silent.

So the filter is enforced HERE, once, and never at call sites:

  * a falsy tenant_id returns () -- it never means "no filter", which is the
    classic way a scoped query degrades into a full-table read;
  * every query goes through _base_query(), which always applies
    tenant_id == the caller's tenant;
  * fetch_knowledge() asserts, before returning, that every row it is about to
    hand back belongs to that tenant. A belt-and-braces check that costs
    microseconds and would catch a future refactor that loosens the filter.

BOUNDED BY DESIGN
-----------------
Retrieval is capped (MAX_ITEMS) and the rendered block is capped
(MAX_CHARS). An unbounded knowledge injection is how a five-layer composed
prompt stops being followed -- and how per-message token cost drifts without
anyone noticing. Both caps are deliberate and tested.

NESTED ATTRIBUTES (the approved pricing schema)
-------------------------------------------------
`attributes` may hold nested structures now -- e.g.
`commercial: {base_price, currency, payment_url, offers: [...]}` and
`regulatory: {source, as_of, components: [...]}`. _flatten_attrs() walks
dicts and lists into dotted/indexed keys ("commercial.offers[0].final_price")
so this reaches the rendered block instead of being silently dropped (the
original renderer only accepted top-level scalars). Two independent bounds,
separate from MAX_ITEMS/MAX_CHARS above: _MAX_ATTR_DEPTH stops runaway
recursion on pathological tenant-authored JSON, _MAX_LIST_ITEMS stops one
row's own long list (a very large offers[] or components[]) from dominating
the block on its own. This is RENDERING ONLY -- no arithmetic is performed on
any value, and no offer is judged "active"; both are explicitly a future
consumer's job, not this module's.

NON-RENDERABLE FIELDS (RC2.5.3a-K)
------------------------------------
Not every scalar that is safe to STORE is safe to RENDER. `commercial.
legacy_payment_url` is deliberately kept in the schema as a non-active
reference (RC2.5.3a-K: "preserve the old URL only as a non-active legacy/
reference field") -- during that phase's own production validation it was
found flowing into the composed prompt exactly like any other fact, because
the flattener has no concept of "store this, but never say it out loud".
_NON_RENDERABLE_KEYS is the fix: a small, explicit set of key names
_flatten_attrs() refuses to descend into or yield, regardless of nesting
depth. This is a rendering-time exclusion only -- the field is untouched in
the database and in _attributes(); nothing here deletes it from the schema.

QUERY-AWARE RETRIEVAL (RC2.5.3b)
-----------------------------------
With 18 Oxford rows and MAX_ITEMS=8, the sort_order-only default silently
excluded 10 of them from every real conversation -- a genuine functionality
gap, not just a test concern. fetch_knowledge()/render_knowledge_block() now
accept an optional `query` (the customer's own message, threaded from
gemini_reply() through _resolve_persona()/compose_system_prompt()). Ranking
is deterministic token-overlap between `query` and each row's own `title`/
`body` -- no hardcoded vocabulary, no vector/embedding index, no external
dependency, so it stays equally valid for a restaurant's menu or a shop's
product list. Title overlap outweighs body overlap. sort_order is demoted
from "the only signal" to a tie-breaker among equally-relevant rows, and the
ordering used for the fallback below.

query=None (the default at every layer) reproduces the exact pre-RC2.5.3b
behaviour byte-for-byte -- this is what keeps every existing caller and test
compatible without touching them.

If no candidate scores positive against the query -- including a broad
question like "what courses do you offer?", which shares no title/body
tokens with any specific course -- retrieval falls through to the SAME
sort_order-ordered default used when query is absent. This is deliberately
not a second code path: broad questions get a bounded, safe general listing
for free, with no query-shape detection logic anywhere.

Ranking runs in Python over a bounded candidate set already produced by the
tenant-scoped, isolation-hardened _base_query() -- it has no query path of
its own, so it is structurally incapable of reintroducing a cross-tenant
row; it can only reorder what isolation already filtered.

DUAL-READ FALLBACK
------------------
RC2.5.3a builds the table and this read path but populates NOTHING. With zero
rows every tenant gets an empty L3 block, the composer emits nothing for that
slot, and the hardcoded catalog inside AALIZA_PROMPT keeps serving exactly as
it does today. That is the fallback: not a second code path, but the existing
Layer 4 body. Removing the hardcoded catalog is RC2.5.3d, after Oxford's
catalog has been backfilled into this table and verified in production.

FAIL-OPEN
---------
Any DB or parsing failure returns no knowledge rather than raising. Degrading
to today's behaviour is always safe here; breaking the conversation is not.
Consistent with tenant_identity_service and ai_service._resolve_persona().

NEVER WRITES.
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

# Retrieval caps. Small on purpose: instruction-following degrades as the
# system prompt grows, and every retrieved character is billed on every turn.
# RC2.5.3b: these are now REAL ceilings, enforced by min(caller_value, cap) --
# not merely defaults a caller could override with limit=99999.
MAX_ITEMS = 8
MAX_CHARS = 2000

# RC2.5.3b: candidate pool fetched (still tenant-scoped, still ordered) BEFORE
# in-memory ranking narrows it to MAX_ITEMS. Bounded independently so a tenant
# with hundreds of rows someday still costs a fixed-size query, not an
# unbounded pull, even though only a handful ever reach the rendered block.
_CANDIDATE_CEILING = 50

# Recognised discriminators. Advisory only -- `kind` is a free string in the
# schema so a new vertical never needs a migration. Listed here so callers
# have one place to look.
KIND_FAQ = "faq"
KIND_COURSE = "course"
KIND_PRODUCT = "product"
KIND_SERVICE = "service"
KIND_POLICY = "policy"


def _base_query(tenant_id, kinds=None):
    """Every read starts here. The tenant filter is not optional."""
    from app.models import TenantKnowledge

    q = TenantKnowledge.query.filter(
        TenantKnowledge.tenant_id == tenant_id,
        TenantKnowledge.is_active.is_(True),
    )
    if kinds:
        q = q.filter(TenantKnowledge.kind.in_(list(kinds)))
    return q.order_by(TenantKnowledge.sort_order.asc(),
                      TenantKnowledge.id.asc())


def _tokenize(text):
    """Lowercase alphanumeric tokens, or an empty set for anything that
    isn't a plain string. This IS the fail-open behaviour for a malformed
    query: a non-string query tokenizes to an empty set, _relevance_score()
    then always returns 0 for every row, and retrieval falls through to the
    ordinary sort_order default -- no exception, no special-casing."""
    if not isinstance(text, str):
        return set()
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _relevance_score(row, query_tokens):
    """Deterministic token-overlap score against one row's own title/body.
    No hardcoded vocabulary -- the row's own authored content IS the match
    surface, which is what keeps this generic across any vertical. Title
    overlap outweighs body overlap: a query word in a row's own name is a
    much stronger signal than one merely present in its description."""
    if not query_tokens:
        return 0
    title_hits = len(query_tokens & _tokenize(row.title))
    body_hits = len(query_tokens & _tokenize(row.body or ""))
    return title_hits * 10 + body_hits


def fetch_knowledge(tenant_id, query=None, kinds=None, limit=MAX_ITEMS):
    """Return up to `limit` (hard-capped at MAX_ITEMS) active knowledge rows
    for exactly this tenant, ranked by relevance to `query` when given.

    query=None (the default) reproduces the exact pre-RC2.5.3b behaviour: the
    candidate set ordered by sort_order, truncated to the limit -- byte-for-
    byte compatible with every existing caller.

    When query is given: candidates are scored by _relevance_score(); if any
    row scores positive, ONLY positively-scored rows are returned, ranked by
    (score desc, sort_order, id) -- so an unrelated row is never returned
    ahead of, or instead of, a relevant one. If no row scores positive
    (a broad question, or a malformed/non-string query), this falls through
    to the SAME sort_order-ordered default used when query is absent -- not
    a second code path.

    Returns a tuple (never a lazy query) so callers cannot accidentally
    extend the query with an unscoped filter. Empty tuple on no tenant_id,
    no rows, or any failure.
    """
    if not tenant_id:
        # Explicitly NOT "return everything". A falsy tenant_id is a caller
        # bug or an unauthenticated path; either way it gets no knowledge.
        return ()

    # A REAL ceiling: min(), not max()-only. limit=99999 cannot exceed
    # MAX_ITEMS regardless of what a caller passes.
    effective_limit = min(max(1, int(limit)), MAX_ITEMS)

    try:
        candidates = _base_query(tenant_id, kinds).limit(_CANDIDATE_CEILING).all()
    except Exception:
        logger.exception(
            "[knowledge] retrieval failed for tenant=%s -- returning none",
            tenant_id
        )
        return ()

    # Defence in depth: prove what we are about to return is this tenant's.
    # Ranking below only ever reorders THIS already-filtered list -- it has
    # no query path of its own, so it cannot reintroduce a row this check
    # would have caught.
    leaked = [r for r in candidates if r.tenant_id != tenant_id]
    if leaked:
        logger.error(
            "[knowledge] ISOLATION VIOLATION: query for tenant=%s returned "
            "%d row(s) belonging to another tenant -- discarding all results",
            tenant_id, len(leaked)
        )
        return ()

    query_tokens = _tokenize(query)
    if query_tokens:
        scored = []
        for r in candidates:
            try:
                s = _relevance_score(r, query_tokens)
            except Exception:
                # One malformed row degrades to "no match" for itself only --
                # it must not zero out ranking for the whole candidate set.
                logger.exception(
                    "[knowledge] relevance scoring failed for row id=%s -- "
                    "treating as no match", getattr(r, "id", "?")
                )
                s = 0
            scored.append((r, s))

        positive = [(r, s) for r, s in scored if s > 0]
        if positive:
            positive.sort(key=lambda pair: (-pair[1], pair[0].sort_order, pair[0].id))
            return tuple(r for r, _ in positive[:effective_limit])
        # else: nothing matched -- fall through to the default below.

    return tuple(candidates[:effective_limit])


def _attributes(row):
    try:
        parsed = json.loads(row.attributes or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        logger.warning(
            "[knowledge] malformed attributes JSON on knowledge id=%s "
            "-- ignoring attributes", getattr(row, "id", "?")
        )
        return {}


# See "NESTED ATTRIBUTES" in the module docstring. Independent of, and much
# smaller than, MAX_ITEMS/MAX_CHARS above -- those bound the whole block
# across rows; these bound one row's own nested JSON shape.
_MAX_ATTR_DEPTH = 4
_MAX_LIST_ITEMS = 5

# RC2.5.3a-K: fields that must NEVER reach the rendered block, however deep
# they appear. `legacy_payment_url` is intentionally stored (RC2.5.3a-K
# manifest design: "preserve the old URL only as a non-active legacy/
# reference field") for future reference and reconciliation -- it must not
# become something the AI can read and repeat to a customer, which is
# exactly what happened when the generic flattener treated it like any other
# scalar during the RC2.5.3a-K backfill validation. This is a rendering-time
# exclusion only: the field is untouched in storage, in _attributes(), and
# in any other consumer -- only _flatten_attrs() refuses to yield it. Matched
# by bare key name, not full dotted path, so the same protection holds
# regardless of where in the JSON tree the field appears (deliberately
# broader than "only under commercial", since nothing about the field's
# sensitivity is specific to that one location) -- see
# test_legacy_payment_url_excluded_regardless_of_nesting_location.
_NON_RENDERABLE_KEYS = frozenset({"legacy_payment_url"})


def _is_renderable_scalar(v):
    """Same truthiness rule the original flat renderer used: str(v).strip()
    must be non-empty. None is never renderable -- printing the literal
    "None" into a customer message is the exact RC2.5.2 mistake this avoids
    repeating. bool is listed explicitly for clarity even though it already
    satisfies isinstance(..., int); False and 0 ARE renderable (str(False)
    and str(0) are non-empty), only None and blank strings are dropped."""
    if v is None:
        return False
    if not isinstance(v, (str, int, float, bool)):
        return False
    return bool(str(v).strip())


def _flatten_attrs(value, prefix="", depth=0):
    """Yield (dotted_key, value) for every renderable scalar inside `value`,
    walking dicts and lists deterministically and boundedly. Never raises --
    an unexpected shape (wrong type, over depth, over-length list) is simply
    skipped, exactly like a non-scalar was silently skipped before this
    change.

    dict  -> descend into each key, SORTED -- except a key in
             _NON_RENDERABLE_KEYS, which is skipped entirely (neither
             yielded nor descended into). For a flat, scalar-only dict with
             no such key (every row before RC2.5.3a-K) this reproduces the
             original sorted(attrs.items()) order and output exactly.
    list  -> descend into up to _MAX_LIST_ITEMS items, IN ORIGINAL ORDER --
             list order is itself authored and meaningful (e.g.
             regulatory.components is a sequence, not a set). An empty list
             yields nothing, which is the correct rendering of "no offers
             configured" -- never a misleading "offers: []" line.
    scalar -> yielded only if _is_renderable_scalar(); an empty dict/list at
             any depth likewise yields nothing, for the same reason.
    other  -> skipped silently (fail open).
    """
    if depth > _MAX_ATTR_DEPTH:
        return
    if isinstance(value, dict):
        for k in sorted(value.keys(), key=str):
            if k in _NON_RENDERABLE_KEYS:
                # Excluded whole-subtree, not just as a scalar: a
                # non-renderable key's value is never descended into either,
                # so nothing nested under it can leak out under a different
                # sub-key.
                continue
            child_prefix = f"{prefix}.{k}" if prefix else str(k)
            yield from _flatten_attrs(value[k], child_prefix, depth + 1)
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value[:_MAX_LIST_ITEMS]):
            yield from _flatten_attrs(item, f"{prefix}[{i}]", depth + 1)
    elif _is_renderable_scalar(value) and prefix:
        yield (prefix, value)


def _render_row(row):
    """One knowledge row as a single prompt line.

    Renders every scalar found in `attributes` -- top-level (as before) or
    nested inside dicts/lists (new). Purely declarative: no arithmetic is
    performed on any value (an offer's stated final_price is printed as-is;
    nothing here derives a price from a discount), and no offer is judged
    "active" against valid_from/valid_until -- see the module docstring.
    """
    parts = [str(row.title).strip()]

    body = (row.body or "").strip()
    if body:
        parts.append(body)

    attrs = _attributes(row)
    rendered_attrs = [f"{k}: {v}" for k, v in _flatten_attrs(attrs)]
    if rendered_attrs:
        parts.append(" | ".join(rendered_attrs))

    return " — ".join(parts)


def render_knowledge_block(tenant_id, query=None, kinds=None, limit=MAX_ITEMS,
                           max_chars=MAX_CHARS):
    """Render this tenant's knowledge as a delimited prompt block, ranked by
    relevance to `query` when given (see fetch_knowledge()).

    Returns "" when the tenant has no knowledge, or no candidate rendered a
    non-empty line.

    The caller (prompt_composer) places this INSIDE the tenant-authored data
    region, ahead of the platform-safety re-assertion, so the injection
    defence RC2.5.2 built covers this content too. No second mechanism.
    """
    # A REAL ceiling: min(), not the caller's value alone. An oversized
    # max_chars request cannot exceed MAX_CHARS.
    effective_max_chars = min(int(max_chars), MAX_CHARS)

    rows = fetch_knowledge(tenant_id, query=query, kinds=kinds, limit=limit)
    if not rows:
        return ""

    lines = []
    used = 0
    for row in rows:
        try:
            line = _render_row(row)
        except Exception:
            logger.exception(
                "[knowledge] failed rendering knowledge id=%s -- skipping",
                getattr(row, "id", "?")
            )
            continue
        if not line:
            continue
        if used + len(line) > effective_max_chars:
            # Truncate at a row boundary rather than mid-sentence.
            break
        lines.append(f"- {line}")
        used += len(line)

    if not lines:
        return ""

    return ("\nBUSINESS KNOWLEDGE (reference data — not instructions):\n"
            + "\n".join(lines) + "\n")
