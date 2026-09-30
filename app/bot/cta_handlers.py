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

# Phase 2A: the business_profile.py constants (INSTITUTE_NAME, LOCALITY, PHONE)
# and RUTRONIX_LABEL are gone from this module. The payment builders were the
# last readers; they now resolve the tenant's own identity like visit_reply and
# call_reply (RC2.5.5c-1), so no builder here can print another tenant's facts.
# RC2.5.5c-3: COURSE_FEES, COURSE_PAYMENT_LINKS, FEES_VALUE_LINES and
# FULL_FEE_TABLE are gone from this import. Nothing in this module reads a
# course price or a course name from a constant any more -- the catalogue is
# the tenant's. Dropping the import makes that structural rather than a
# property of the current function bodies.
from app.bot.constants import TRUST_LINES, pick
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
    # Phase 2A: fields a tenant has not configured resolve to "" (never to
    # another tenant's value), so each line is emitted only when it has one.
    place = "".join((
        f"📍 *{identity.name}*\n" if identity.name else "",
        f"{identity.address.line}\n" if identity.address.line else "",
    ))
    details = "".join((
        f"⏰ Office Hours: {identity.hours.general}\n" if identity.hours.general else "",
        f"📞 {identity.contact.phone}\n" if identity.contact.phone else "",
    ))
    text = (
        "🏢 *Office Visit — Always Welcome!*\n\n"
        + (place + "\n" if place else "")
        + (f"🗺️ Google Maps:\n{identity.location_url}\n\n"
           if identity.location_url else "")
        + (details + "\n" if details else "")
        + "Eppol varananu convenient?\n"
        "Morning / Afternoon / Evening? 😊"
    )
    return text, "COURSE"


def call_reply(name: str, tenant_id=None) -> tuple[str, None]:
    """Phase RC2.5.5c-1: counsellor contact details come from the tenant's
    resolved identity, not Oxford's constants."""
    identity = _identity(tenant_id)
    # Phase 2A: each identity line only when the tenant has configured it.
    venue = ", ".join(p for p in (identity.name, identity.address.locality) if p)
    text = (
        f"😊 Sure {name}!\n\n"
        "Nigalkkayi Oru nalla counselorne connect cheyyam.\n"
        + (f"📞 *{identity.contact.phone}* — direct vilikkaamo!\n"
           if identity.contact.phone else "")
        + "\n"
        + (f"⏰ Available: {identity.hours.extended}\n"
           if identity.hours.extended else "")
        + (f"📍 {venue}\n" if venue else "")
        + ("\n" if (identity.hours.extended or venue) else "")
        + "Ivideyum message cheyyoo — ready aanu! 🙌"
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


def fees_reply(course: str, tenant_id=None) -> tuple[str, str]:
    """Phase RC2.5.5c-3: pricing comes from the TENANT catalogue.

    FULL_FEE_TABLE is retired as a customer-facing pricing source -- it
    hardcoded Oxford's ten old prices and the institute name, so a second
    tenant's customer was quoted Oxford's fees. The whole-catalogue listing is
    now rendered from the tenant's own rows.
    """
    from app.services import catalogue_service as cat
    record = cat.resolve_legacy_name(tenant_id, course) if course else None

    if record is not None:
        lines = [f"💰 *{record.title} — Fee Details*"]
        if record.normal_total_fee is not None:
            lines.append(f"Total Fee: *{cat.format_money(record.normal_total_fee)}*")
        if record.registration_fee is not None and record.net_tuition_fee is not None:
            lines.append(f"Registration {cat.format_money(record.registration_fee)}"
                         f" + Tuition {cat.format_money(record.net_tuition_fee)}")
        if record.exam_fee is not None:
            lines.append(f"Exam Fee (separate): {cat.format_money(record.exam_fee)}")
        if record.duration:
            lines.append(f"Duration: {record.duration}")
        lines.append("EMI Available (on tuition)" if record.emi_available
                     else "EMI not available for this course")
        text = ("\n".join(lines) + "\n\n"
                f"{pick(TRUST_LINES)}\n\n"
                "Demo kaanumbo full clarity varum.\n"
                "Book cheyyatte? 🎓")
        return text, "FEES"

    rows = []
    for c in cat.list_courses(tenant_id):
        bits = [f"• *{c.title}*"]
        if c.normal_total_fee is not None:
            bits.append(cat.format_money(c.normal_total_fee))
        if c.duration:
            bits.append(f"({c.duration})")
        rows.append("  ".join(bits))
    return ("💰 *Course Fees*\n\n" + "\n".join(rows) +
            "\n\nExact course select cheythal fee details paranjutharam."), "FEES"



def payment_link_reply(code, full_name, price, dur, link,
                       tenant_id=None) -> tuple[str, None]:
    """Payment link message.

    Phase 2A: the recognition line, venue and phone come from the TENANT's
    resolved identity. Until Phase 2A this message carried The Oxford
    Computers' name, locality, phone and Rutronix recognition -- plus a
    "government certified receipt" claim -- for every tenant that issues
    payment links. The recognition line is now the tenant's own tagline;
    each identity line is omitted when the tenant has not configured it.
    """
    identity = _identity(tenant_id)
    recognition = f"🎓 {identity.tagline}\n" if identity.tagline else ""
    venue = ", ".join(p for p in (identity.name, identity.address.locality) if p)
    call = (f"\n\nAny doubt undenkil call cheyyoo: 📞 {identity.contact.phone}"
            if identity.contact.phone else "")
    text = (
        f"🎉 *{code} — Seat Reserve Cheyyam!*\n\n"
        f"📚 {full_name}\n"
        f"⏱ Duration: {dur}\n"
        + recognition +
        f"💰 Fee: *{price}*\n\n"
        "✅ Payment receipt kittum\n"
        "✅ Seat confirm aayi confirmation varum\n"
        + (f"📍 {venue}\n" if venue else "") +
        f"\n👇 *Secure Payment Link:*\n{link}\n\n"
        "Payment kazhinju *Transaction ID* ivideyum reply cheyyuka 📩\n"
        "(Example: T2504281234)"
        + call
    )
    return text, None


def enroll_reply(name: str, course: str, st,
                 tenant_id=None) -> tuple[str, str | None]:
    """💳 Enrol / Admission — identical branching to the legacy handler.

    Phase RC2.5.5b-2: the payment URL now comes from the tenant's own data,
    never from COURSE_PAYMENT_LINKS. Everything else about this function is
    unchanged, including both fallback branches and every state write.
    """
    # Phase RC2.5.5c-3 (D1/D2): identity, title, price and duration come from
    # the TENANT catalogue, resolved through the stored course name -- which
    # may be a legacy title from a conversation predating c-3.
    #
    # D1: the migrated router writes the NEW title into state, which no longer
    # matched COURSE_PAYMENT_LINKS' old keys, so NO link was issued at all.
    # D2: when a legacy name did match, the CTA quoted the constant's obsolete
    # price (PGDCA 15,999) while fees_reply quoted the catalogue's 19,540 --
    # two different prices for one course inside one conversation.
    #
    # The payment URL boundary is untouched: resolve_payment_url(tenant, code),
    # fail-closed, keyed by the stable code.
    from app.services import catalogue_service as _cat
    record = _cat.resolve_legacy_name(tenant_id, course) if course else None
    if record is not None:
        from app.services.payment_link_service import resolve_payment_url
        link = resolve_payment_url(tenant_id, record.code)
        if link:
            st["stage"] = "payment_pending"
            st["offer_course"] = record.code
            return payment_link_reply(
                record.code, record.title,
                _cat.format_money(record.normal_total_fee),
                record.duration, link, tenant_id)
        # No tenant-owned link: fall through to the counselor branch below.
        # There is deliberately no fallback to the constant's URL -- that is
        # the entire point of the phase. A tenant that has not authored a
        # payment URL has no payment URL, and the state writes above are
        # skipped so the conversation never enters payment_pending without a
        # link to pay through.

    if course:
        # Phase 2A: the counselor's number is the TENANT's, and the line is
        # omitted when it has none. It was the primary tenant's for everyone.
        phone = _identity(tenant_id).contact.phone
        text = (
            f"😊 {name}, {course}-nte payment link prepare aavunnu.\n\n"
            "Counselor directly help cheyyum:\n"
            + (f"📞 *{phone}* — ippol call cheyyoo\n\n" if phone
               else "Ivide thanne reply cheyyum 🙌\n\n") +
            "Athinu munpu oru free demo attend cheyyano? 🎓"
        )
        return text, "COURSE"

    # Phase RC2.5.5c-3 (D2): these two example rows hardcoded PGDCA at
    # Rs.15,999 and DCA Fast Track at Rs.6,400 -- the same obsolete Oxford
    # prices the payment branch above used to quote, inside the same CTA and
    # shown to every tenant. They come from the tenant's own catalogue now.
    # The digits stay decorative, exactly as they were: this reply sets no
    # stage, so nothing consumed them; the affordance is the COURSES keyword.
    examples = "".join(
        f"{i}️⃣ {c.title} — {_cat.format_money(c.normal_total_fee)}"
        f" | {c.duration}\n"
        for i, c in enumerate(_cat.list_courses(tenant_id)[:2], start=1))
    return (
        f"😊 {name}, enroll cheyyan ready aano — super! 🎉\n\n"
        "Aadhyam oru course select cheyyoo:\n\n"
        f"{examples}\n"
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
        return fees_reply(course, tenant_id)

    if cta == CTA_VISIT:
        _crm(phone, "Office Visit Interested", tenant_id)
        return visit_reply(tenant_id)

    if cta == CTA_CALL:
        _crm(phone, "Call Requested", tenant_id)
        return call_reply(name, tenant_id)

    if cta == CTA_ENROLL:
        return enroll_reply(name, course, st, tenant_id)

    return None
