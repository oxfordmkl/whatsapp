"""
Phase 1.6.6 — CTA handler layer.

Owns every call-to-action the conversation offers: Demo, Fees, Visit, Call and
Enrol/Admission. The router dispatches to `handle_cta()` and holds no
CTA-specific business logic of its own.

Two responsibilities live here, deliberately:
  * the CTA reply builders (migrated out of router.py), and
  * the side effects each CTA performs — stage transitions, CRM status updates
    and analytics events — reproduced EXACTLY as the legacy router performed
    them, so numeric/keyword replies and button taps are indistinguishable to
    CRM, analytics, memory, the follow-up scheduler and the State Engine.

Business facts rule: every institute detail comes from business_profile.py.
There are NO business literals in this module — a regression test enforces it.
"""
import threading

from flask import current_app

from app.bot.business_profile import (
    ADDRESS, COUNSELLOR_HOURS, INSTITUTE_NAME, LOCALITY,
    MAPS_URL, OFFICE_HOURS, PHONE, WEBSITE,
)
from app.bot.constants import (
    COURSE_FEES, COURSE_PAYMENT_LINKS, FEES_VALUE_LINES, FULL_FEE_TABLE,
    RUTRONIX_LABEL, TRUST_LINES, pick,
)
from app.services.crm_service import update_lead_status
from app.services.log_service import log_lead_event_in_thread

# ── CTA keys (mirror navigation.CTA_KEYS) ────────────────────────────────────
CTA_DEMO = "DEMO"
CTA_FEES = "FEES"
CTA_VISIT = "VISIT"
CTA_CALL = "CALL"
CTA_ENROLL = "ENROLL"


# ── Reply builders (migrated from router.py) ─────────────────────────────────

def visit_reply() -> tuple[str, str]:
    """🏢 Visit Institute.

    Phase 1.6.6 Maps enhancement: now carries the institute name, the canonical
    address, the Google Maps link and the phone number — all sourced from the
    Business Profile, which is the only place those values exist.
    """
    text = (
        "🏢 *Office Visit — Always Welcome!*\n\n"
        f"📍 *{INSTITUTE_NAME}*\n"
        f"{ADDRESS}\n\n"
        f"🗺️ Google Maps:\n{MAPS_URL}\n\n"
        f"⏰ Office Hours: {OFFICE_HOURS}\n"
        f"📞 {PHONE}\n\n"
        "Eppol varananu convenient?\n"
        "Morning / Afternoon / Evening? 😊"
    )
    return text, "COURSE"


def call_reply(name: str) -> tuple[str, None]:
    text = (
        f"😊 Sure {name}!\n\n"
        "Nigalkkayi Oru nalla counselorne connect cheyyam.\n"
        f"📞 *{PHONE}* — direct vilikkaamo!\n\n"
        f"⏰ Available: {COUNSELLOR_HOURS}\n"
        f"📍 {INSTITUTE_NAME}, {LOCALITY}\n\n"
        "Ivideyum message cheyyoo — ready aanu! 🙌"
    )
    return text, None


def demo_time_reply():
    """Free Demo slot picker.

    Phase 1.6.7: the builder owns the slot definitions and the screen body, so
    the reply now carries SLOT:* reply buttons alongside the original numbered
    text (which keeps the legacy numeric affordance working).
    """
    from app.bot.screens import demo_slots_screen
    screen = demo_slots_screen()
    return screen.body, screen.as_buttons()


def fees_reply(course: str) -> tuple[str, str]:
    if course and course in COURSE_FEES:
        fee, duration = COURSE_FEES[course]
        text = (
            f"💰 *{course} — Fee Details*\n\n"
            f"Fee: {fee} | Duration: {duration}\n\n"
            f"{pick(FEES_VALUE_LINES)}\n"
            f"{pick(TRUST_LINES)}\n\n"
            "Demo kaanumbo full clarity varum.\n"
            "Book cheyyatte? 🎓"
        )
        return text, "FEES"
    return (FULL_FEE_TABLE +
            "\n\nExact course select cheythal EMI/monthly idea paranjutharam."), "FEES"



def payment_link_reply(code, full_name, price, dur, link) -> tuple[str, None]:
    text = (
        f"🎉 *{code} — Seat Reserve Cheyyam!*\n\n"
        f"📚 {full_name}\n"
        f"⏱ Duration: {dur}\n"
        f"🎓 {RUTRONIX_LABEL}\n"
        f"💰 Fee: *{price}*\n\n"
        "✅ Government certified receipt kittum\n"
        "✅ Seat confirm aayi confirmation varum\n"
        f"📍 {INSTITUTE_NAME}, {LOCALITY}\n\n"
        f"👇 *Secure Payment Link:*\n{link}\n\n"
        "Payment kazhinju *Transaction ID* ivideyum reply cheyyuka 📩\n"
        "(Example: T2504281234)\n\n"
        f"Any doubt undenkil call cheyyoo: 📞 {PHONE}"
    )
    return text, None


def enroll_reply(name: str, course: str, st) -> tuple[str, str | None]:
    """💳 Enrol / Admission — identical branching to the legacy handler."""
    if course and course in COURSE_PAYMENT_LINKS:
        code, full_name, price, dur, link = COURSE_PAYMENT_LINKS[course]
        st["stage"] = "payment_pending"
        st["offer_course"] = code
        return payment_link_reply(code, full_name, price, dur, link)

    if course:
        text = (
            f"😊 {name}, {course}-nte payment link prepare aavunnu.\n\n"
            "Counselor directly help cheyyum:\n"
            f"📞 *{PHONE}* — ippol call cheyyoo\n\n"
            "Athinu munpu oru free demo attend cheyyano? 🎓"
        )
        return text, "COURSE"

    return (
        f"😊 {name}, enroll cheyyan ready aano — super! 🎉\n\n"
        "Aadhyam oru course select cheyyoo:\n\n"
        "1️⃣ PGDCA — ₹15,999 | 12 Months\n"
        "2️⃣ DCA Fast Track — ₹6,400 | 6 Months\n\n"
        "Full list kaanan: *COURSES* reply cheyyoo 📚"
    ), "GOAL"


# ── Side-effect helpers (same threads/args the legacy router used) ───────────

def _crm(phone: str, status: str, tenant_id) -> None:
    threading.Thread(
        target=update_lead_status, args=(phone, status, "", tenant_id)
    ).start()


def _event(phone: str, event_type: str, tenant_id, event_data=None) -> None:
    _app = current_app._get_current_object()
    kwargs = dict(app=_app, phone=phone, event_type=event_type, tenant_id=tenant_id)
    if event_data is not None:
        kwargs["event_data"] = event_data
    threading.Thread(target=log_lead_event_in_thread, kwargs=kwargs, daemon=True).start()


# ── Dispatcher ───────────────────────────────────────────────────────────────

def handle_cta(cta: str, name: str, st, phone: str,
               tenant_id=None) -> tuple[str, str | None] | None:
    """Run a CTA and return its reply, or None when `cta` is not handled here.

    Side effects mirror the legacy router exactly, so a button tap and the
    equivalent typed keyword produce identical CRM and analytics records.
    """
    cta = (cta or "").upper()
    course = st.get("course") or ""

    if cta == CTA_DEMO:
        st["stage"] = "demo_time_ask"
        _event(phone, "DEMO_REQUESTED", tenant_id)
        return demo_time_reply()

    if cta == CTA_FEES:
        _event(phone, "FEES_REQUESTED", tenant_id, event_data=course or None)
        return fees_reply(course)

    if cta == CTA_VISIT:
        _crm(phone, "Office Visit Interested", tenant_id)
        return visit_reply()

    if cta == CTA_CALL:
        _crm(phone, "Call Requested", tenant_id)
        return call_reply(name)

    if cta == CTA_ENROLL:
        return enroll_reply(name, course, st)

    return None
