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

# RC2.5.5c-1: only the PAYMENT builders still read these module-level
# constants. Every identity-only builder (visit_reply, call_reply) now resolves
# the tenant's own identity instead. Retiring the last three is blocked on the
# payment paths, which this phase is not authorised to touch.
from app.bot.business_profile import INSTITUTE_NAME, LOCALITY, PHONE
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

def _identity(tenant_id):
    """This tenant's resolved business identity.

    Lazy import for the same reason the payment resolver uses one: several
    suites build a synthetic `app.services` module and inject only the members
    they stub, so a module-level import of a real sibling breaks collection in
    files unrelated to identity.

    resolve_business_identity() never raises and falls back per field to the
    platform defaults, which are derived from business_profile.py -- so an
    unconfigured tenant resolves to exactly the values these builders used to
    read directly, by construction.
    """
    from app.services.tenant_identity_service import resolve_business_identity
    return resolve_business_identity(tenant_id)


def visit_reply(tenant_id=None) -> tuple[str, str]:
    """🏢 Visit Institute.

    Phase 1.6.6 Maps enhancement: carries the institute name, the canonical
    address, the Google Maps link and the phone number.

    Phase RC2.5.5c-1: those values now come from the TENANT's resolved
    business identity rather than the module-level Oxford constants. A second
    tenant's customer was previously told to visit Oxford's office and call
    Oxford's phone number.

    Fail-SAFE, not fail-closed: resolve_business_identity() never raises and
    falls back per field to platform defaults. That is the right polarity
    here -- this is address/contact CONTENT, not a payment instruction, so
    degrading to a default beats showing the customer nothing. The payment
    paths in this module keep the opposite, fail-closed contract.
    """
    identity = _identity(tenant_id)
    text = (
        "🏢 *Office Visit — Always Welcome!*\n\n"
        f"📍 *{identity.name}*\n"
        f"{identity.address.line}\n\n"
        f"🗺️ Google Maps:\n{identity.location_url}\n\n"
        f"⏰ Office Hours: {identity.hours.general}\n"
        f"📞 {identity.contact.phone}\n\n"
        "Eppol varananu convenient?\n"
        "Morning / Afternoon / Evening? 😊"
    )
    return text, "COURSE"


def call_reply(name: str, tenant_id=None) -> tuple[str, None]:
    """Phase RC2.5.5c-1: counsellor contact details come from the tenant's
    resolved identity, not Oxford's constants."""
    identity = _identity(tenant_id)
    text = (
        f"😊 Sure {name}!\n\n"
        "Nigalkkayi Oru nalla counselorne connect cheyyam.\n"
        f"📞 *{identity.contact.phone}* — direct vilikkaamo!\n\n"
        f"⏰ Available: {identity.hours.extended}\n"
        f"📍 {identity.name}, {identity.address.locality}\n\n"
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


def enroll_reply(name: str, course: str, st,
                 tenant_id=None) -> tuple[str, str | None]:
    """💳 Enrol / Admission — identical branching to the legacy handler.

    Phase RC2.5.5b-2: the payment URL now comes from the tenant's own data,
    never from COURSE_PAYMENT_LINKS. Everything else about this function is
    unchanged, including both fallback branches and every state write.
    """
    if course and course in COURSE_PAYMENT_LINKS:
        # Index [4] -- the URL -- is deliberately NOT unpacked. The constant
        # remains the CATALOGUE (display copy and the name -> code index);
        # it is no longer a source of payment links.
        code, full_name, price, dur = COURSE_PAYMENT_LINKS[course][:4]
        # Imported lazily, matching the idiom already used in this
        # module and in router.py. Several suites build a synthetic
        # `app.services` module and inject only the members they stub,
        # so a module-level import of a real sibling breaks collection
        # in files that have nothing to do with payments.
        from app.services.payment_link_service import resolve_payment_url
        link = resolve_payment_url(tenant_id, code)
        if link:
            st["stage"] = "payment_pending"
            st["offer_course"] = code
            return payment_link_reply(code, full_name, price, dur, link)
        # No tenant-owned link: fall through to the counselor branch below.
        # There is deliberately no fallback to the constant's URL -- that is
        # the entire point of the phase. A tenant that has not authored a
        # payment URL has no payment URL, and the state writes above are
        # skipped so the conversation never enters payment_pending without a
        # link to pay through.

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
        return visit_reply(tenant_id)

    if cta == CTA_CALL:
        _crm(phone, "Call Requested", tenant_id)
        return call_reply(name, tenant_id)

    if cta == CTA_ENROLL:
        return enroll_reply(name, course, st, tenant_id)

    return None
