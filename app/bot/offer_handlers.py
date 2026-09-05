"""
Phase 1.6.8 — Offer / admission handler layer.

Owns the offer conversation: the offer menu, offer selection (by code or by the
legacy number), and the payment confirmation that closes an admission. The
router dispatches here and holds no offer-specific logic.

Offer definitions are NOT restated here — `constants.OFFER_MENU` remains the
single catalogue, and this module derives a code index from it, so an offer's
price/link can never disagree between the numeric and the OFR:* path.

Behaviour parity: stage transitions, `offer_course`, and the CRM
"Payment Received: …" status (with its timestamped note) are byte-identical to
the legacy router. Selecting an offer performs NO CRM write and NO analytics
event — exactly as before; this phase adds neither.

All institute facts come from business_profile.py; no business literals here.
"""
import threading
from datetime import datetime

from app.bot.business_profile import CITY, INSTITUTE_NAME, LOCALITY, PHONE
# RC2.5.5c-3: COURSE_PAYMENT_LINKS is gone. OFFER_MENU stays -- but only as
# the offer SET and its 1-2-3-4 positions; its title/price/duration columns
# are obsolete and are no longer read.
from app.bot.constants import OFFER_MENU, RUTRONIX_LABEL, URGENCY_LINES, pick
from app.bot.cta_handlers import payment_link_reply
from app.services.crm_service import update_lead_status

# Offer code → full catalogue entry, derived from the one catalogue.
OFFERS_BY_CODE = {entry[0]: entry for entry in OFFER_MENU.values()}


# ── Reply builders ───────────────────────────────────────────────────────────

def offer_menu_reply(tenant_id=None) -> tuple[str, str]:
    """Phase RC2.5.5c-3 (D2): these four rows hardcoded Oxford's old prices --
    4,800 / 6,400 / 19,999 / 15,999 -- shown to every tenant's customer and
    already obsolete against the tenant catalogue. OFFER_MENU still defines
    WHICH courses are on offer and their menu position; title, price and
    duration now come from the tenant's own catalogue row."""
    from app.services import catalogue_service as _cat
    rows = []
    for digit, entry in sorted(OFFER_MENU.items()):
        record = _cat.get_course(tenant_id, entry[0])
        if record is None:
            continue
        rows.append(f"{digit}️⃣ {record.title}\n"
                    f"   💰 {_cat.format_money(record.normal_total_fee)}"
                    f" | ⏱ {record.duration}\n")

    if not rows:
        # No course in THIS tenant's catalogue is on offer. Before the D2 fix
        # this branch could not be reached -- every tenant was answered with
        # Oxford's four courses at Oxford's prices, which is the defect. Say
        # so and point at the real catalogue rather than asking for a course
        # number under an empty list.
        return (
            "🔥 *Special Offer*\n"
            "━━━━━━━━━━━━━━━━\n"
            f"{RUTRONIX_LABEL} courses.\n\n"
            "Ippol offer batch onnum active alla.\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "Full course list kaanan *COURSES* reply cheyyoo 📚\n"
            "Unsure aanenkil *DEMO* reply cheyyoo 🎓"
        ), "OFFER"

    text = (
        "🔥 *Special Offer — This Batch Only!*\n"
        "━━━━━━━━━━━━━━━━\n"
        f"{RUTRONIX_LABEL} courses.\n\n"
        + "\n".join(rows) +
        "━━━━━━━━━━━━━━━━\n"
        f"⚠️ {pick(URGENCY_LINES)}\n\n"
        "Seat reserve cheyyan course number reply cheyyoo.\n"
        "Unsure aanenkil *DEMO* reply cheyyoo 🎓"
    )
    return text, "OFFER"


def payment_confirmed_reply(txn: str, course: str, name: str) -> tuple[str, str]:
    text = (
        "🎉 *Payment Received — Seat Confirmed!*\n\n"
        f"✅ Transaction ID: {txn}\n"
        f"📚 Course: {course}\n"
        f"👤 Name: {name}\n\n"
        f"Welcome to *{INSTITUTE_NAME}*! 🎓\n\n"
        f"📞 {PHONE} — batch details ariyaan\n"
        f"📍 {LOCALITY}, {CITY}\n\n"
        "Kaanaan kaathirikkunnu! 😊"
    )
    return text, "AFTER_BOOKING"


# ── Handlers ─────────────────────────────────────────────────────────────────

def handle_offer(code: str, st, tenant_id=None) -> tuple[str, None] | None:
    """Select an offer by its code and issue the payment link.

    Returns None for an unknown code so the caller falls through to legacy.
    No CRM write and no analytics event — identical to the legacy selection.

    Phase RC2.5.5b-2: the URL is resolved from the tenant's own data. When the
    tenant has no link for this code the offer is NOT issued -- returning None
    lets the caller fall through exactly as it does for an unknown code, and
    the state writes are skipped so no conversation enters payment_pending
    without a link. There is no fallback to the catalogue's URL.
    """
    entry = OFFERS_BY_CODE.get((code or "").upper())
    if entry is None:
        return None
    offer_code = entry[0]
    # Phase RC2.5.5c-3 (D2): OFFER_MENU defines the offer SET only. Title,
    # price and duration come from the tenant catalogue -- the constant's
    # price column is obsolete (PGDCA 15,999 vs the catalogue's 19,540).
    from app.services import catalogue_service as _cat
    record = _cat.get_course(tenant_id, offer_code)
    if record is None:
        return None
    from app.services.payment_link_service import resolve_payment_url
    link = resolve_payment_url(tenant_id, offer_code)
    if not link:
        return None
    st["offer_course"] = offer_code
    st["stage"] = "payment_pending"
    return payment_link_reply(
        record.code, record.title,
        _cat.format_money(record.normal_total_fee), record.duration, link)


def handle_pay_intent(st, tenant_id=None) -> tuple[str, str | None]:
    """"pay" / "enrol" / "seat" keyword intent.

    Issues the payment link when the chosen course has one, else opens the offer
    menu — identical branching and state writes to the legacy router.

    Phase RC2.5.5b-2: the URL comes from the tenant's own data. Without one the
    caller falls through to the offer menu, exactly as a course with no link
    already did -- never to the catalogue's URL.
    """
    course = st.get("course") or ""
    # Phase RC2.5.5c-3 (D1/D2): resolved through the tenant catalogue, so a
    # new-style title still finds its course and the price shown is current.
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
                record.duration, link)
    st["stage"] = "offer_menu"
    return offer_menu_reply(tenant_id)


def handle_offer_number(low: str, st, tenant_id=None) -> tuple[str, None] | None:
    """Legacy numeric offer reply at the offer_menu stage."""
    entry = OFFER_MENU.get(low)
    if entry is None:
        return None
    return handle_offer(entry[0], st, tenant_id)


def handle_payment(raw: str, name: str, st, phone: str,
                   tenant_id=None) -> tuple[str, str]:
    """Record the transaction id, write the CRM record and confirm the seat."""
    txn = raw
    offer = st.get("offer_course", "Unknown")
    st["stage"] = "enrolled"
    ts = datetime.now().strftime("[%Y-%m-%d %H:%M]")
    note = f"{ts} Payment: {txn} Course: {offer}"
    threading.Thread(
        target=update_lead_status,
        args=(phone, f"Payment Received: {txn}", note, tenant_id),
    ).start()
    return payment_confirmed_reply(txn, offer, name)
