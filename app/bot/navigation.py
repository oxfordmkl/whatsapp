"""
Phase 1.6.1 — Interactive navigation ACTION RESOLUTION (isolated, UNWIRED).

Single responsibility: turn an interactive reply id (button_reply.id or
list_reply.id, delivered by the webhook as the message text) into a structured
Action. It resolves *what the user asked for* — nothing else.

Explicitly NOT in this module (by design, per the approved architecture):
  - no message text, no button/list payloads, no screen rendering
    (that lives in the separate builder module, Phase 6.2+)
  - no state mutation, no DB, no CRM/analytics side effects
  - no routing decisions

WHY THIS EXISTS — the stale-menu fix
------------------------------------
Legacy interactive ids are bare positional digits ("1", "2", ...) whose meaning
depends on the CURRENT conversation stage, so tapping an old menu silently
selects the wrong branch. This grammar makes every id **absolute and
self-describing**: an id means the same thing no matter which screen it came
from or how old that screen is. Stale taps therefore resolve correctly *by
construction*, with no navigation stack and no extra state.

Back targets are encoded IN the id (e.g. "NAV:BACK:LIST:accounting"), so the
"go back" destination travels with the button instead of being remembered.

ID GRAMMAR
----------
    NAV:MENU                     → main menu
    NAV:BACK                     → back, destination unspecified (caller decides)
    NAV:BACK:MENU                → back to main menu
    NAV:BACK:LIST:<category>     → back to that category's course list
    CAT:<category>               → career category chosen
    CRS:<course_index>           → course chosen
    ACT:<DEMO|FEES|VISIT|ENROLL|CALL>
    SLOT:<morning|afternoon|evening>
    OFR:<code>                   → offer/payment option

BACKWARD COMPATIBILITY (permanent contract)
-------------------------------------------
parse_action() returns None for ANYTHING that is not this grammar — including
bare numerics ("1"), keywords ("DEMO"), and free text. Callers must treat None
as "not a navigation id" and fall through to the existing legacy handling, which
is never removed. This module can therefore never break typed numeric replies or
buttons emitted by older builds.

Fail-safe: parse_action() never raises. Malformed input returns None.
"""
from dataclasses import dataclass

# ── Action kinds ──────────────────────────────────────────────────────────────
KIND_MENU = "menu"
KIND_BACK = "back"
KIND_CATEGORY = "category"
KIND_COURSE = "course"
KIND_CTA = "cta"
KIND_SLOT = "slot"
KIND_OFFER = "offer"

# ── Namespace prefixes ────────────────────────────────────────────────────────
NS_NAV = "NAV"
NS_CATEGORY = "CAT"
NS_COURSE = "CRS"
NS_CTA = "ACT"
NS_SLOT = "SLOT"
NS_OFFER = "OFR"

SEP = ":"

# ── Vocabularies ──────────────────────────────────────────────────────────────
# CATEGORY_KEYS mirrors constants.GOAL_COURSES keys plus "unsure" (the
# "help me choose" path). A drift guard test asserts they stay in sync; the
# mapping from key → courses belongs to the builder, not here.
CATEGORY_KEYS = frozenset({"job", "business", "basic", "accounting", "unsure"})

CTA_KEYS = frozenset({"DEMO", "FEES", "VISIT", "ENROLL", "CALL"})

SLOT_KEYS = frozenset({"morning", "afternoon", "evening"})

# Destinations a NAV:BACK id may name. "" means unspecified.
BACK_SCREENS = frozenset({"MENU", "CATEGORY", "LIST", "COURSE"})


@dataclass(frozen=True)
class Action:
    """A resolved navigation intent.

    kind          one of the KIND_* constants
    value         category key / course index / CTA key / slot key / offer code
    target_screen for KIND_BACK only: destination screen token ("" if unspecified)
    target_arg    for KIND_BACK only: destination argument, e.g. the category
    raw           the original id, for logging/telemetry
    """
    kind: str
    value: str = ""
    target_screen: str = ""
    target_arg: str = ""
    raw: str = ""


def parse_action(raw) -> Action | None:
    """Resolve an interactive reply id into an Action, or None.

    None means "not a navigation id" — the caller must fall through to legacy
    handling. Never raises.
    """
    try:
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        # No separator → bare numeric / keyword / free text → legacy path.
        if SEP not in text:
            return None

        head, _, rest = text.partition(SEP)
        namespace = head.strip().upper()
        rest = rest.strip()
        if not rest:
            # "NAV:" style with an empty payload is malformed, except NAV:BACK
            # which is handled below via its own branch (it always has a rest).
            return None

        if namespace == NS_NAV:
            return _parse_nav(rest, text)

        if namespace == NS_CATEGORY:
            key = rest.lower()
            return Action(kind=KIND_CATEGORY, value=key, raw=text) \
                if key in CATEGORY_KEYS else None

        if namespace == NS_COURSE:
            # The course index is validated against ALL_COURSES by the caller;
            # navigation stays free of business data.
            return Action(kind=KIND_COURSE, value=rest, raw=text)

        if namespace == NS_CTA:
            key = rest.upper()
            return Action(kind=KIND_CTA, value=key, raw=text) \
                if key in CTA_KEYS else None

        if namespace == NS_SLOT:
            key = rest.lower()
            return Action(kind=KIND_SLOT, value=key, raw=text) \
                if key in SLOT_KEYS else None

        if namespace == NS_OFFER:
            return Action(kind=KIND_OFFER, value=rest.upper(), raw=text)

        return None
    except Exception:
        return None


def _parse_nav(rest: str, raw: str) -> Action | None:
    """Resolve the NAV namespace: MENU, BACK, BACK:<screen>[:<arg>]."""
    verb, _, target = rest.partition(SEP)
    verb = verb.strip().upper()

    if verb == "MENU":
        return Action(kind=KIND_MENU, raw=raw)

    if verb == "BACK":
        target = target.strip()
        if not target:
            # Destination unspecified — the caller decides a sensible default.
            return Action(kind=KIND_BACK, raw=raw)
        screen, _, arg = target.partition(SEP)
        screen = screen.strip().upper()
        if screen not in BACK_SCREENS:
            return None
        return Action(
            kind=KIND_BACK,
            target_screen=screen,
            target_arg=arg.strip().lower(),
            raw=raw,
        )

    return None


def is_navigation_id(raw) -> bool:
    """True when `raw` is a navigation id this module owns.

    Convenience for callers that only need the yes/no decision before falling
    through to legacy handling.
    """
    return parse_action(raw) is not None


# ── Id builders (string construction only — no message rendering) ─────────────
# Provided so the builder module and tests construct ids through ONE definition
# of the grammar rather than hand-writing strings.

def menu_id() -> str:
    return f"{NS_NAV}{SEP}MENU"


def back_id(screen: str = "", arg: str = "") -> str:
    if not screen:
        return f"{NS_NAV}{SEP}BACK"
    if arg:
        return f"{NS_NAV}{SEP}BACK{SEP}{screen.upper()}{SEP}{arg.lower()}"
    return f"{NS_NAV}{SEP}BACK{SEP}{screen.upper()}"


def category_id(key: str) -> str:
    return f"{NS_CATEGORY}{SEP}{key.lower()}"


def course_id(index: str) -> str:
    return f"{NS_COURSE}{SEP}{index}"


def cta_id(key: str) -> str:
    return f"{NS_CTA}{SEP}{key.upper()}"


def slot_id(key: str) -> str:
    return f"{NS_SLOT}{SEP}{key.lower()}"


def offer_id(code: str) -> str:
    return f"{NS_OFFER}{SEP}{code.upper()}"
