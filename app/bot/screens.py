"""
Phase 1.6.2 — Interactive SCREEN GENERATION (isolated, UNWIRED).

Single responsibility: build the *content* of an interactive screen. It decides
what the user sees and which navigation ids each choice carries — nothing else.

Explicitly NOT in this module (per the approved separation):
  - no action parsing            → app/bot/navigation.py
  - no sending / HTTP / payloads → app/services/whatsapp_service.py
  - no state, DB, CRM, analytics, routing

Screen type policy (approved):
  * List Messages ONLY where choices naturally exceed the 3-button limit:
    Main Menu, Category Menu, Course List.
  * Reply Buttons for conversion-critical screens: Course Details
    (🎓 Free Demo / 💰 Fee Details / 🏢 Visit Institute).
    Those three actions consume the entire 3-button budget, so secondary
    navigation on that screen falls back to a text command ("MENU"), which is
    the documented platform-limit fallback.

Every id is produced through app/bot/navigation.py's builders, so ids always
match the parser's grammar and can never collide with legacy numeric replies.

Phase 6.2 status: imported by nobody in production. Router behaviour unchanged.
"""
from dataclasses import dataclass, field

from app.bot.business_profile import INSTITUTE_NAME, PHONE
from app.bot.constants import (
    ALL_COURSES, GOAL_COURSES, RUTRONIX_FULL, RUTRONIX_LABEL,
)
from app.bot.navigation import (
    back_id, category_id, course_id, cta_id, menu_id, slot_id,
)

# Screen kinds
KIND_LIST = "list"
KIND_BUTTONS = "buttons"

# Text-command fallback used where the 3-button budget is exhausted.
MENU_HINT = "💬 Mukhya menu-yilekku mടങ്ങാൻ *MENU* type cheyyoo"

# Meta caps a List row title at 24 characters. Several GOAL_COURSES display
# labels are longer, so the builder fits them here (presentation is the
# builder's job); the full name is always shown on the Course Details card.
ROW_TITLE_MAX = 24


def _fit_title(text: str) -> str:
    """Shorten a course label to the List row-title limit, preferring a clean cut.

    "PGDCA — Post Graduate Diploma" → "PGDCA"   (take the part before the dash)
    "AI-Driven Digital Marketing"   → "AI-Driven Digital Mark…"
    """
    text = (text or "").strip()
    if len(text) <= ROW_TITLE_MAX:
        return text
    head = text.split(" — ")[0].strip()
    if head and len(head) <= ROW_TITLE_MAX:
        return head
    return text[:ROW_TITLE_MAX - 1].rstrip() + "…"


@dataclass(frozen=True)
class Row:
    """One selectable row inside a List Message section."""
    id: str
    title: str
    description: str = ""


@dataclass(frozen=True)
class Section:
    """A titled group of rows inside a List Message."""
    title: str
    rows: tuple = ()


@dataclass(frozen=True)
class Button:
    """One reply button."""
    id: str
    title: str


@dataclass(frozen=True)
class Screen:
    """A renderable screen, transport-agnostic.

    kind == KIND_LIST    → use sections + list_button_label
    kind == KIND_BUTTONS → use buttons (max 3)
    """
    kind: str
    body: str
    buttons: tuple = ()
    sections: tuple = ()
    list_button_label: str = ""
    header: str = ""
    footer: str = ""
    # Phase 1.6.9 — legacy rendering of this same screen, used by the transport
    # layer when List Messages are unavailable. Declaring both representations
    # here is what lets WA_LIST_MESSAGES stay a pure transport decision: no
    # business logic ever inspects the flag.
    fallback_body: str = ""
    fallback_preset: object = None

    def as_buttons(self) -> list:
        """Plain-dict view of reply buttons for the transport layer.

        This is how Course Details hands its CTA definitions to the router —
        the builder owns which CTAs a screen offers, not the router.
        """
        return [{"id": b.id, "title": b.title} for b in self.buttons]

    def as_sections(self) -> list:
        """Plain-dict view of sections for the transport layer."""
        return [
            {
                "title": s.title,
                "rows": [
                    {"id": r.id, "title": r.title, "description": r.description}
                    for r in s.rows
                ],
            }
            for s in self.sections
        ]


# ── Category presentation (labels only; course data lives in constants) ──────
_CATEGORY_ROWS = (
    ("job",        "💼 Job / IT Career",           "Software, IT, Government jobs"),
    ("business",   "🚀 Business / Freelance",       "Digital Marketing, Web Design"),
    ("basic",      "💻 Basic Computer",             "DCA, Data Entry, Office Skills"),
    ("accounting", "📊 Accounting & Tax",           "SAP, GST, Corporate Accounting"),
    ("unsure",     "🤔 Which Course is Best for Me?", "Personalised recommendation"),
)


def _category_section() -> Section:
    return Section(
        title="📌 Choose Your Career Path",
        rows=tuple(
            Row(id=category_id(key), title=title, description=desc)
            for key, title, desc in _CATEGORY_ROWS
        ),
    )


def legacy_main_menu_reply(name: str, tenant_id=None) -> tuple[str, str]:
    """The pre-Phase-6 welcome menu, verbatim (migrated from router.msg_welcome).

    This is the Main Menu's legacy representation: plain text plus the "GOAL"
    reply-button preset. The transport layer renders it whenever List Messages
    are unavailable, so behaviour with the flag OFF is byte-identical to before.

    Phase RC2.5.4c-x-6c1: `tenant_id` is accepted and NOT read. This screen
    still renders the platform's hardcoded institute name and recognition
    wording, which is a separate, unresolved defect (x-6c). Threading the
    parameter first, with output byte-identical, is what lets a later phase
    resolve identity here from the tenant rather than from a constant --
    without that phase also having to change every call chain at the same
    time. Accepting it does not make this screen tenant-aware.
    """
    text = (
        f"👋 നമസ്കാരം *{name}*!\n\n"
        f"*{INSTITUTE_NAME}*-ലേക്ക് സ്വാഗതം! 🎓\n"
        f"{RUTRONIX_FULL} • AI-Enabled Courses\n\n"
        "Ningalude lakshyam enthanu? 🤔\n\n"
        "1️⃣ Job Oriented — IT / Software career\n"
        "2️⃣ Business / Freelance\n"
        "3️⃣ Basic Computer / Office Job\n"
        "4️⃣ Accounting / Tax\n"
        "5️⃣ Not sure — help me choose\n\n"
        "Number reply cheyyoo! 📝"
    )
    return text, "GOAL"


def main_menu(name: str = "", tenant_id=None) -> Screen:
    """Main Menu — a List Message, with the legacy menu as its fallback.

    Phase 7.0A Refinement 1: Greeting shortened to 5-6 lines.
    Career categories remain the primary section.
    Architecture, IDs, routing and fallback are completely unchanged.

    Phase RC2.5.4c-x-6c1: `tenant_id` is accepted, forwarded to the legacy
    fallback, and NOT read -- see legacy_main_menu_reply. `name` is the
    CUSTOMER's name and is unrelated to it. Output is byte-identical with the
    parameter present, absent or None; that is this phase's whole contract.
    """
    hi = f" *{name}*" if name else ""
    body = (
        f"👋 Hi{hi}!\n\n"
        f"ഞാൻ Oxford Nova — *{INSTITUTE_NAME}*-ലെ AI Admission Counsellor 😊\n\n"
        "നിങ്ങളുടെ career goal-നു best course കണ്ടെത്താൻ ഞാൻ സഹായിക്കാം.\n\n"
        "👇 ആദ്യം ഒരു Career Path തിരഞ്ഞെടുക്കൂ."
    )
    legacy_body, legacy_preset = legacy_main_menu_reply(name, tenant_id)
    quick = Section(
        title="⚡ Need Immediate Help?",
        rows=(
            Row(id=cta_id("DEMO"),  title="🎓 Book Free Demo",   description="Zero commitment — attend & decide"),
            Row(id=cta_id("FEES"),  title="💰 Course Fees",      description="Full fee list + EMI options"),
            Row(id=cta_id("VISIT"), title="📍 Visit Institute",  description="Address, map & office hours"),
        ),
    )
    return Screen(
        kind=KIND_LIST,
        body=body,
        sections=(_category_section(), quick),
        list_button_label="📋 Select",
        fallback_body=legacy_body,
        fallback_preset=legacy_preset,
    )


def category_menu() -> Screen:
    """Career Category menu — a List Message.

    Reached when the user goes back from a Course List.
    Phase 7.0 UX Polish: copy updated only.
    """
    body = (
        "🎯 *Career Categories*\n\n"
        "Ningalude career goal ethaanu?\n"
        "Athinanu best course recommend cheyyam 👇"
    )
    nav = Section(
        title="Navigation",
        rows=(Row(id=menu_id(), title="🏠 Main Menu", description="Back to the beginning"),),
    )
    return Screen(
        kind=KIND_LIST,
        body=body,
        sections=(_category_section(), nav),
        list_button_label="📋 Select",
    )


def _catalogue():
    """Lazy import: several suites stub app.services with only the members
    they need, so a module-level import breaks unrelated collection."""
    from app.services import catalogue_service
    return catalogue_service


def _identity(tenant_id):
    from app.services.tenant_identity_service import resolve_business_identity
    return resolve_business_identity(tenant_id)


def course_list(category: str, tenant_id=None) -> Screen | None:
    """Course List for a career category — a List Message.

    Returns None for a category with no course mapping (e.g. "unsure", which the
    router handles conversationally). Includes Back + Main Menu rows, so the
    destination travels with the button and needs no navigation stack.
    Phase 7.0 UX Polish: copy updated only. IDs and routing unchanged.
    """
    # Phase RC2.5.5c-3: the tenant's own catalogue, not Oxford's constants.
    cat = _catalogue()
    recommendations = cat.courses_for_category(tenant_id, category)
    if not recommendations:
        return None

    body = (
        "✨ *Recommended Courses*\n\n"
        "Ningalude goal-ku best match aayi\n"
        "ivayil ninnum select cheyyoo 👇\n"
        "Full details + fee — oru tap mathram!"
    )
    # Rows are keyed by the STABLE code, never by menu position -- a course
    # added, removed or reordered must not change what a tapped row means.
    course_rows = tuple(
        Row(
            id=course_id(c.code),
            title=_fit_title(c.title),
            description=f"⏱ {c.duration}  💰 {cat.format_money(c.normal_total_fee)}",
        )
        for c in recommendations
    )
    nav = Section(
        title="Navigation",
        rows=(
            Row(id=back_id("CATEGORY"), title="⬅️ All Categories", description="Different career path choose cheyyam"),
            Row(id=menu_id(),           title="🏠 Main Menu",      description="Back to the beginning"),
        ),
    )
    return Screen(
        kind=KIND_LIST,
        body=body,
        sections=(Section(title="📚 Courses for You", rows=course_rows), nav),
        list_button_label="📋 Select",
    )


# ── Demo slots — the builder owns these definitions ─────────────────────────
# (key, button title, canonical batch_time label)
# The batch_time label is byte-identical to the legacy numeric map, so CRM
# records are the same whether the user tapped a button or replied "1".
DEMO_SLOTS = (
    ("morning",   "🌅 Morning 9-11AM",  "Morning (9–11 AM)"),
    ("afternoon", "☀️ Afternoon 12-2PM", "Afternoon (12–2 PM)"),
    ("evening",   "🌆 Evening 5-7PM",   "Evening (5–7 PM)"),
)


def slot_label(slot_key: str) -> str | None:
    """Canonical batch_time label for a slot key, or None if unknown."""
    for key, _title, label in DEMO_SLOTS:
        if key == slot_key:
            return label
    return None


def demo_slots_screen() -> Screen:
    """Free Demo booking — Reply Buttons (conversion-critical).

    The numbered lines are kept verbatim so legacy numeric replies ("1"/"2"/"3")
    remain a valid affordance alongside the buttons.
    Phase 7.0 UX Polish: copy updated only. Slot IDs and buttons unchanged.
    """
    body = (
        "🎓 *Free Demo Class — Seat Reserve Cheyyam!*\n\n"
        "Demo completely FREE aanu 😊\n"
        "Attend cheythathe decision edukkanda.\n\n"
        "Eppo varanam? Ningalude convenient time select cheyyoo 👇\n\n"
        "1️⃣ Morning   — 9 AM to 11 AM\n"
        "2️⃣ Afternoon — 12 PM to 2 PM\n"
        "3️⃣ Evening   — 5 PM to 7 PM\n\n"
        "Button tap cheyyoo athava number reply cheyyoo! 📅"
    )
    return Screen(
        kind=KIND_BUTTONS,
        body=body,
        buttons=tuple(
            Button(id=slot_id(key), title=title) for key, title, _label in DEMO_SLOTS
        ),
    )


def help_me_choose() -> Screen:
    """"Help Me Choose" destination — a List Message.

    The `unsure` category has no course mapping, so this screen asks the two
    qualifying questions instead of dead-ending, and still offers a way back.
    Phase 7.0 UX Polish: copy updated only. IDs and routing unchanged.
    """
    body = (
        "🤔 *Ethu course best aayi suit aaakum?*\n\n"
        "Paravayilla — njan personally help cheyyam! 😊\n\n"
        "Randu chodyam mathram:\n"
        "1️⃣ Ningal +2 / Degree / Working — ethu stage-ilaanu?\n"
        "2️⃣ Main goal — IT job, business, government job, alle office skill?\n\n"
        "Ivideyum reply cheyyoo —\n"
        "ningalude situation-ku perfect course recommend cheyyam 🎓"
    )
    nav = Section(
        title="Navigation",
        rows=(
            Row(id=back_id("CATEGORY"), title="⬅️ All Categories", description="Career categories browse cheyyam"),
            Row(id=menu_id(),           title="🏠 Main Menu",      description="Back to the beginning"),
        ),
    )
    return Screen(
        kind=KIND_LIST,
        body=body,
        sections=(nav,),
        list_button_label="📋 Select",
    )


def course_details(course_code: str, tenant_id=None) -> Screen | None:
    """Course Details — Reply Buttons (conversion-critical screen).

    The three primary actions use the full 3-button budget, so main-menu
    navigation is offered as a text command (MENU_HINT) — the documented
    fallback for WhatsApp's reply-button limit.

    Returns None for an unknown or inactive course code -- the caller takes
    its not-found path. It must never fall back to a different course.

    Phase RC2.5.5c-3: content, pricing and EMI come from the tenant catalogue,
    and the institute facts from the tenant's resolved identity.
    """
    cat = _catalogue()
    course = cat.get_course(tenant_id, course_code)
    if course is None:
        return None

    identity = _identity(tenant_id)
    lines = [f"📚 *{course.title}*"]
    if course.body:
        lines.append(course.body)
    if course.duration:
        lines.append(f"⏱ Duration: {course.duration}")
    if course.normal_total_fee is not None:
        lines.append(f"💰 Course Fee: *{cat.format_money(course.normal_total_fee)}*")
        if course.registration_fee is not None and course.net_tuition_fee is not None:
            lines.append(
                f"   (Registration {cat.format_money(course.registration_fee)}"
                f" + Tuition {cat.format_money(course.net_tuition_fee)})")
    if course.exam_fee is not None:
        lines.append(f"📝 Exam Fee (separate): {cat.format_money(course.exam_fee)}")
    for offer in course.offers:
        label = offer.get("name") or "Special Offer"
        price = offer.get("price")
        if price is not None:
            lines.append(f"🔥 {label}: {cat.format_money(price)}")
    # EMI is a per-course fact now, not a blanket claim.
    if course.emi_available:
        lines.append("✅ EMI Available (on tuition)")

    body = (
        "\n".join(lines)
        + "\n\n━━━━━━━━━━━━━━━━\n"
        + f"🏫 *{identity.name}*\n"
        + f"📞 {identity.contact.phone} · 🌐 {identity.contact.website}\n"
        + f"\n{MENU_HINT}"
    )
    return Screen(
        kind=KIND_BUTTONS,
        body=body,
        buttons=(
            Button(id=cta_id("DEMO"),  title="🎓 Free Demo"),
            Button(id=cta_id("FEES"),  title="💰 Fee Details"),
            Button(id=cta_id("VISIT"), title="📍 Visit Institute"),
        ),
    )
