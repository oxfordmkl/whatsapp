"""
Phase 1.6.7 — Demo booking handler layer.

Owns the whole demo-booking conversation: slot selection, date capture and the
booking confirmation, together with the CRM record each step writes. The router
dispatches here and holds no booking-specific logic.

Slot definitions are NOT declared here — the builder (app/bot/screens.py) owns
which slots exist and what they are called; this layer consumes them via
`screens.slot_label()`, so the button a user taps and the `batch_time` written to
CRM can never drift apart.

Behaviour parity: stage transitions, the `batch_time` string and the CRM
"Demo Booked: ..." status are byte-identical to the legacy router, so a button
tap and a numeric reply produce the same records. No new analytics event is
emitted — the phase brief requires analytics to remain identical (see the
delivery notes if a DEMO_BOOKED event is wanted later).

All institute facts come from business_profile.py; this module contains no
business literals.
"""
import threading

from app.bot.business_profile import INSTITUTE_NAME, LOCALITY, PHONE, WEBSITE
from app.bot.screens import DEMO_SLOTS, slot_label
from app.services.crm_service import update_lead_status

# Legacy numeric affordance at the demo_time_ask stage → slot key.
# Ordered exactly as DEMO_SLOTS so the two can never disagree.
LEGACY_SLOT_NUMBERS = {
    str(i + 1): key for i, (key, _title, _label) in enumerate(DEMO_SLOTS)
}


# ── Reply builders ───────────────────────────────────────────────────────────

def date_ask_reply(batch_time: str) -> tuple[str, None]:
    text = (
        f"✅ *{batch_time}* batch confirmed!\n\n"
        "Preferred date ഏതാണ്?\n"
        "(Example: Tomorrow, Monday, May 5)\n\n"
        "Date reply cheyyoo! 📅"
    )
    return text, None


def booked_reply(course: str, batch_time: str, date: str) -> tuple[str, str]:
    text = (
        "🎉 *Demo Class Booked Successfully!*\n\n"
        f"📚 Course: {course or 'Course of your choice'}\n"
        f"⏰ Time: {batch_time}\n"
        f"📅 Date: {date}\n"
        f"📍 {INSTITUTE_NAME}, {LOCALITY}\n\n"
        "Naaḷe ഞങ്ങൾ WhatsApp-ൽ confirm ചെയ്യും! ✅\n"
        f"📞 {PHONE} | 🌐 {WEBSITE}"
    )
    return text, "AFTER_BOOKING"


# ── Handlers ─────────────────────────────────────────────────────────────────

def handle_slot(slot_key: str, st) -> tuple[str, None] | None:
    """Record the chosen demo slot and ask for a date.

    Returns None for an unknown slot so the caller falls through to legacy.
    Accepted from ANY stage: slot ids are absolute, so tapping one on an older
    message re-enters the booking flow instead of misfiring.
    """
    label = slot_label((slot_key or "").lower())
    if label is None:
        return None
    st["batch_time"] = label
    st["stage"] = "demo_date_ask"
    return date_ask_reply(label)


def handle_slot_number(low: str, st) -> tuple[str, None] | None:
    """Legacy numeric slot reply at the demo_time_ask stage."""
    slot_key = LEGACY_SLOT_NUMBERS.get(low)
    if slot_key is None:
        return None
    return handle_slot(slot_key, st)


def handle_date(raw: str, st, phone: str, tenant_id=None) -> tuple[str, str]:
    """Capture the date, write the CRM booking record and confirm."""
    course = st.get("course") or ""
    batch_time = st.get("batch_time", "")
    st["stage"] = "demo_booked"
    status = f"Demo Booked: {course} | {batch_time} | {raw}"
    threading.Thread(
        target=update_lead_status, args=(phone, status, "", tenant_id)
    ).start()
    return booked_reply(course, batch_time, raw)
