import threading
from datetime import datetime
from flask import current_app
from app.state import get_or_create_state
from app.bot.constants import (
    ALL_COURSES, COURSE_FEES, KEYWORD_TO_COURSE, GOAL_COURSES,
    OFFER_MENU, COURSE_PAYMENT_LINKS, FULL_FEE_TABLE,
    DEMO_CTA, COURSE_CLOSE, URGENCY_LINES, TRUST_LINES, FEES_VALUE_LINES, pick
)
from app.bot.objections import detect_objection, handle_objection
from app.services.ai_service import gemini_reply, smart_fallback
from app.services.crm_service import update_lead_status
from app.services.log_service import log_lead_event_in_thread

VISIT_WORDS   = {"visit", "office", "varam", "neritt", "address", "location",
                 "map", "route", "varanam", "edukkam"}
CALL_WORDS    = {"call me", "call", "counselor", "talk to counselor",
                 "office number", "vilikku", "phone"}
GREETING_WORDS = {"hi", "hello", "hai", "hii", "hey", "namaskaram",
                  "നമസ്കാരം", "hy", "helo", "helloo"}

# Words that open a question (first token signals interrogative intent)
_QUESTION_STARTERS = frozenset({
    "is", "does", "will", "can", "how", "what", "when",
    "why", "which", "are", "has", "have", "should", "would",
})
# Words that, when co-occurring with a course keyword in a longer message,
# indicate the message is a question rather than a bare course name lookup.
_QUESTION_SIGNALS = frozenset({
    "fee", "fees", "high", "cost", "price", "expensive", "worth",
    "better", "vs", "compare", "placement", "job", "salary",
    "difficult", "easy", "eligible", "difference", "good", "best",
})


def _is_question(low: str) -> bool:
    """Return True when the message appears to be a question, not a bare keyword lookup."""
    if "?" in low:
        return True
    tokens = low.split()
    if tokens and tokens[0] in _QUESTION_STARTERS:
        return True
    return len(tokens) > 3 and bool(_QUESTION_SIGNALS.intersection(tokens))


def _state(phone: str, name: str, tenant_id: str = None):
    """Load or create DB-backed state. Returns a StateProxy that auto-saves."""
    return get_or_create_state(phone, name, tenant_id=tenant_id)


def msg_welcome(name: str) -> tuple[str, str]:
    text = (
        f"👋 നമസ്കാരം *{name}*!\n\n"
        "*The Oxford Computers*-ലേക്ക് സ്വാഗതം! 🎓\n"
        "Kerala Govt Certified • AI-Enabled Courses\n\n"
        "Ningalude lakshyam enthanu? 🤔\n\n"
        "1️⃣ Job Oriented — IT / Software career\n"
        "2️⃣ Business / Freelance\n"
        "3️⃣ Basic Computer / Office Job\n"
        "4️⃣ Accounting / Tax\n"
        "5️⃣ Not sure — help me choose\n\n"
        "Number reply cheyyoo! 📝"
    )
    return text, "GOAL"


def msg_website_lead() -> tuple[str, str]:
    text = (
        "Super 👍 ningal correct place-il aanu 😊\n\n"
        "Ningalk best course suggest cheyyan njan help cheyyam\n\n"
        "Ningalude main goal entha? 👇\n\n"
        "1️⃣ Job oriented (fast job)\n"
        "2️⃣ Business / Freelance\n"
        "3️⃣ Basic computer / office job\n"
        "4️⃣ Accounting / GST\n"
        "5️⃣ Not sure"
    )
    return text, "GOAL"


def msg_goal_courses(goal: str, name: str) -> tuple[str, str]:
    courses = GOAL_COURSES.get(goal, GOAL_COURSES["job"])
    lines = [f"📚 *{name}-kku best ആയ courses:*\n"]
    for i, (_, display, dur, fee) in enumerate(courses, 1):
        lines.append(f"{i}️⃣ {display}\n   ⏱ {dur} | 💰 {fee}")
    lines.append("\nNumber reply cheyyoo! 🎓")
    return "\n".join(lines), None


def msg_course_detail(course_idx: str) -> tuple[str, str]:
    c_name, card = ALL_COURSES[course_idx]
    text = (
        f"✅ *{c_name}* — nalla choice aanu! 🎯\n\n"
        f"{card}\n\n"
        f"{pick(TRUST_LINES)}\n"
        f"{pick(COURSE_CLOSE)}\n\n"
        f"{pick(DEMO_CTA)}"
    )
    return text, "COURSE"


def msg_demo_time_ask() -> tuple[str, str]:
    text = (
        "🎓 *Free Demo Class Booking*\n\n"
        "Preferred batch time ഏതാണ്?\n\n"
        "1️⃣ Morning   — 9 AM to 11 AM\n"
        "2️⃣ Afternoon — 12 PM to 2 PM\n"
        "3️⃣ Evening   — 5 PM to 7 PM\n\n"
        "Number reply cheyyoo! 📅"
    )
    return text, None


def msg_demo_date_ask(time_str: str) -> tuple[str, str]:
    text = (
        f"✅ *{time_str}* batch confirmed!\n\n"
        "Preferred date ഏതാണ്?\n"
        "(Example: Tomorrow, Monday, May 5)\n\n"
        "Date reply cheyyoo! 📅"
    )
    return text, None


def msg_demo_booked(course: str, batch_time: str, date: str) -> tuple[str, str]:
    text = (
        "🎉 *Demo Class Booked Successfully!*\n\n"
        f"📚 Course: {course or 'Course of your choice'}\n"
        f"⏰ Time: {batch_time}\n"
        f"📅 Date: {date}\n"
        "📍 The Oxford Computers, Malayinkeezhu\n\n"
        "Naaḷe ഞങ്ങൾ WhatsApp-ൽ confirm ചെയ്യും! ✅\n"
        "📞 9447329972 | 🌐 theoxfordedu.com"
    )
    return text, "AFTER_BOOKING"


def msg_offer_menu() -> tuple[str, str]:
    text = (
        "🔥 *Special Offer — This Batch Only!*\n"
        "━━━━━━━━━━━━━━━━\n"
        "Kerala State Rutronix Approved courses.\n\n"
        "1️⃣ CWPDE — Word Processing & Data Entry\n"
        "   💰 ₹4,800 | ⏱ 6 Months\n\n"
        "2️⃣ DCA — Computer Applications\n"
        "   💰 ₹6,400 | ⏱ 6 Months\n\n"
        "3️⃣ AIDM — AI Digital Marketing\n"
        "   💰 ₹19,999 | ⏱ 6 Months\n\n"
        "4️⃣ PGDCA — Post Graduate Diploma\n"
        "   💰 ₹15,999 | ⏱ 12 Months\n"
        "━━━━━━━━━━━━━━━━\n"
        f"⚠️ {pick(URGENCY_LINES)}\n\n"
        "Seat reserve cheyyan course number reply cheyyoo.\n"
        "Unsure aanenkil *DEMO* reply cheyyoo 🎓"
    )
    return text, "OFFER"


def msg_payment_link(code, full_name, price, dur, link) -> tuple[str, str]:
    text = (
        f"✅ *{code} — Nalla choice aanu!* 🎯\n\n"
        f"📚 {full_name}\n"
        f"⏱ Duration: {dur}\n"
        f"🎓 Kerala State Rutronix Approved\n"
        f"💰 Fee: *{price}*\n\n"
        "✅ Government certified receipt kittum\n"
        "✅ Seat confirm aayi confirmation varum\n"
        f"📍 Oxford Computers, Malayinkeezhu\n\n"
        f"👇 *Secure Payment Link:*\n{link}\n\n"
        "Payment kazhinju *Transaction ID* ivideyum reply cheyyuka 📩\n"
        "(Example: T2504281234)\n\n"
        "Any doubt undenkil call cheyyoo: 📞 9447329972"
    )
    return text, None


def msg_payment_confirmed(txn: str, course: str, name: str) -> tuple[str, str]:
    text = (
        "🎉 *Payment Received — Seat Confirmed!*\n\n"
        f"✅ Transaction ID: {txn}\n"
        f"📚 Course: {course}\n"
        f"👤 Name: {name}\n\n"
        "Welcome to *The Oxford Computers*! 🎓\n\n"
        "📞 9447329972 — batch details ariyaan\n"
        "📍 Malayinkeezhu, Thiruvananthapuram\n\n"
        "Kaanaan kaathirikkunnu! 😊"
    )
    return text, "AFTER_BOOKING"


def msg_visit() -> tuple[str, str]:
    text = (
        "🏢 *Office Visit — Always Welcome!*\n\n"
        "📍 *The Oxford Computers*\n"
        "   Malayinkeezhu Junction\n"
        "   Thiruvananthapuram, Kerala\n\n"
        "⏰ Office Hours: 9 AM – 5 PM (Mon–Sat)\n"
        "📞 9447329972\n\n"
        "Eppol varananu convenient?\n"
        "Morning / Afternoon / Evening? 😊"
    )
    return text, "COURSE"


def msg_call_us(name: str) -> tuple[str, str]:
    text = (
        f"😊 Sure {name}!\n\n"
        "Nigalkkayi Oru nalla counselorne connect cheyyam.\n"
        "📞 *9447329972* — direct vilikkaamo!\n\n"
        "⏰ Available: 9 AM – 7 PM (Mon–Sat)\n"
        "📍 Oxford Computers, Malayinkeezhu\n\n"
        "Ivideyum message cheyyoo — ready aanu! 🙌"
    )
    return text, None


def msg_exit(name: str) -> tuple[str, str]:
    text = (
        f"👋 Nandi {name}! Oru nalla divasam nerunnu! 😊\n\n"
        "The Oxford Computers — always here for you.\n"
        "📞 9447329972 | 🌐 theoxfordedu.com\n\n"
        "Thiriche message cheyyoo — happy to help!"
    )
    return text, None


def smart_reply(msg_text: str, name: str, phone: str, is_new_lead: bool, tenant_id: str = None) -> tuple[str, str | None]:
    raw = msg_text.strip()
    low = raw.lower()

    from app.services.log_service import resolve_tenant_id
    tenant_id = resolve_tenant_id(tenant_id)

    st = _state(phone, name, tenant_id=tenant_id)
    st["last_msg"]  = datetime.now().isoformat()
    st["last_text"] = raw
    stage   = st["stage"]
    course  = st["course"]

    if "course details" in low or "want course details" in low:
        st["stage"] = "goal_selection"
        st["course"] = ""
        return msg_website_lead()

    if is_new_lead:
        st["stage"] = "goal_selection"
        return msg_welcome(name)

    if low == "exit":
        st["stage"] = "done"
        return msg_exit(name)

    if low in GREETING_WORDS and stage in ("new", "done", "enrolled"):
        st["stage"] = "goal_selection"
        return msg_welcome(name)

    objection = detect_objection(low)
    if objection:
        return handle_objection(objection, name, st)

    if low in {"demo", "free demo", "free class", "book demo"}:
        st["stage"] = "demo_time_ask"
        # ── Phase 6A: DEMO_REQUESTED event ──
        _app = current_app._get_current_object()
        threading.Thread(
            target=log_lead_event_in_thread,
            kwargs=dict(app=_app, phone=phone, event_type="DEMO_REQUESTED", tenant_id=tenant_id),
            daemon=True,
        ).start()
        return msg_demo_time_ask()

    if low in {"enroll_now", "enrol_now", "pay_now"}:
        if course and course in COURSE_PAYMENT_LINKS:
            code, full_name, price, dur, link = COURSE_PAYMENT_LINKS[course]
            st["stage"] = "payment_pending"
            st["offer_course"] = code
            return msg_payment_link(code, full_name, price, dur, link)
        elif course:
            text = (
                f"😊 {name}, {course}-nte payment link prepare aavunnu.\n\n"
                "Counselor directly help cheyyum:\n"
                "📞 *9447329972* — ippol call cheyyoo\n\n"
                "Athinu munpu oru free demo attend cheyyano? 🎓"
            )
            return text, "COURSE"
        else:
            return (
                f"😊 {name}, aadhyam oru course select cheyyoo!\n\n"
                "Course list kaanan *COURSES* reply cheyyoo.\n"
                "Athil ningalkku best option njan suggest cheyyam 🎓"
            ), "GOAL"

    if low in {"offer", "today offer", "offer undo", "discount"} or ("offer" in low and "discount" in low):
        st["stage"] = "offer_menu"
        return msg_offer_menu()

    if low in {"pay", "payment", "enrol", "enroll", "seat", "fees pay", "reserve seat"}:
        if course and course in COURSE_PAYMENT_LINKS:
            code, full_name, price, dur, link = COURSE_PAYMENT_LINKS[course]
            st["stage"] = "payment_pending"
            st["offer_course"] = code
            return msg_payment_link(code, full_name, price, dur, link)
        st["stage"] = "offer_menu"
        return msg_offer_menu()

    if low in {"fees", "fee", "price", "cost", "ethra", "how much"}:
        # ── Phase 6A: FEES_REQUESTED event ──
        _app = current_app._get_current_object()
        threading.Thread(
            target=log_lead_event_in_thread,
            kwargs=dict(app=_app, phone=phone, event_type="FEES_REQUESTED",
                        event_data=course or None, tenant_id=tenant_id),
            daemon=True,
        ).start()
        if course and course in COURSE_FEES:
            f, d = COURSE_FEES[course]
            text = (
                f"💰 *{course} — Fee Details*\n\n"
                f"Fee: {f} | Duration: {d}\n\n"
                f"{pick(FEES_VALUE_LINES)}\n"
                f"{pick(TRUST_LINES)}\n\n"
                "Demo kaanumbo full clarity varum.\n"
                "Book cheyyatte? 🎓"
            )
            return text, "FEES"
        return (FULL_FEE_TABLE + "\n\nExact course select cheythal EMI/monthly idea paranjutharam."), "FEES"

    if low in {"courses", "course", "list", "all courses", "padikkaan", "study"}:
        if stage == "goal_selection":
            return (
                f"😊 {name}, oru number reply cheyyoo!\n\n"
                "1️⃣ Job | 2️⃣ Business | 3️⃣ Basic\n"
                "4️⃣ Accounting | 5️⃣ Not sure\n\n"
                "Ningalude goal-ku best course recommend cheyyam! 🎓"
            ), "GOAL"
        st["stage"] = "goal_selection"
        return msg_welcome(name)

    if any(w in low for w in VISIT_WORDS):
        threading.Thread(target=update_lead_status, args=(phone, "Office Visit Interested", "", tenant_id)).start()
        return msg_visit()

    if any(w in low for w in CALL_WORDS):
        threading.Thread(target=update_lead_status, args=(phone, "Call Requested", "", tenant_id)).start()
        return msg_call_us(name)

    if "certificate" in low or "certific" in low:
        text = (
            "🏆 *Government Certified Certificate*\n\n"
            "✅ Kerala State Rutronix Approved\n"
            "✅ Valid for job applications\n"
            "✅ Accepted for higher studies\n\n"
            "Real government-backed certification! 💪"
        )
        return text, "COURSE"

    if low in {"placement", "job assistance", "placement support", "job guarantee"}:
        text = (
            "💼 *Placement Support*\n\n"
            "✅ 100% placement assistance\n"
            "✅ Resume preparation & interview coaching\n"
            "✅ Job referral network\n\n"
            "Students Kerala & Gulf-il working aanu! 🌍\n\n"
            "(Note: We provide placement *assistance*,\n"
            "not a job guarantee — but our track record is strong! 💪)"
        )
        # ── Phase 6A: PLACEMENT_ASKED event ──
        _app = current_app._get_current_object()
        threading.Thread(
            target=log_lead_event_in_thread,
            kwargs=dict(app=_app, phone=phone, event_type="PLACEMENT_ASKED", tenant_id=tenant_id),
            daemon=True,
        ).start()
        return text, "COURSE"

    if low in {"timing", "batch", "time", "schedule", "class time"}:
        text = (
            "⏰ *Batch Timings*\n\n"
            "🌅 Morning:   9 AM – 11 AM\n"
            "☀️  Afternoon: 12 PM – 2 PM\n"
            "🌆 Evening:   5 PM – 7 PM\n\n"
            "Weekend batches also available! 📅\n"
            "Preferred time parañju — book cheyyam!"
        )
        return text, "COURSE"

    if stage == "goal_selection":
        goal_map = {"1": "job", "2": "business", "3": "basic", "4": "accounting"}
        if low in goal_map:
            goal = goal_map[low]
            st["goal"]  = goal
            st["stage"] = "course_recommendation"
            return msg_goal_courses(goal, name)

        if low == "5":
            st["stage"] = "not_sure"
            ai = gemini_reply(
                f"Student {name} is not sure which course to choose. "
                "Ask one friendly question about their qualification and career goal to recommend the right course.",
                name,
            )
            return (ai or smart_fallback(name, low)), "GOAL"

        if low.isdigit():
            return (
                f"Please reply with a number between 1 and 5, {name}! 😊\n\n"
                "1️⃣ Job | 2️⃣ Business | 3️⃣ Basic\n"
                "4️⃣ Accounting | 5️⃣ Not sure"
            ), "GOAL"

    if stage == "course_recommendation":
        goal   = st.get("goal", "job")
        crecs  = GOAL_COURSES.get(goal, GOAL_COURSES["job"])
        if low.isdigit():
            idx = int(low) - 1
            if 0 <= idx < len(crecs):
                c_idx, c_display, c_dur, c_fee = crecs[idx]
                c_name = ALL_COURSES[c_idx][0]
                st["course"] = c_name
                st["stage"]  = "course_viewed"
                threading.Thread(target=update_lead_status, args=(phone, f"Viewed: {c_name}", "", tenant_id)).start()
                # ── Phase 6A: COURSE_VIEWED event (goal menu path) ──
                _app = current_app._get_current_object()
                threading.Thread(
                    target=log_lead_event_in_thread,
                    kwargs=dict(app=_app, phone=phone, event_type="COURSE_VIEWED",
                                event_data=c_name, tenant_id=tenant_id),
                    daemon=True,
                ).start()
                return msg_course_detail(c_idx)

    if stage == "demo_time_ask":
        times = {"1": "Morning (9–11 AM)", "2": "Afternoon (12–2 PM)", "3": "Evening (5–7 PM)"}
        if low in times:
            st["batch_time"] = times[low]
            st["stage"] = "demo_date_ask"
            return msg_demo_date_ask(times[low])

    if stage == "demo_date_ask":
        date_text = raw
        st["stage"] = "demo_booked"
        bt = st.get("batch_time", "")
        status = f"Demo Booked: {course} | {bt} | {date_text}"
        threading.Thread(target=update_lead_status, args=(phone, status, "", tenant_id)).start()
        return msg_demo_booked(course, bt, date_text)

    if stage == "offer_menu":
        if low in OFFER_MENU:
            code, full_name, price, dur, link = OFFER_MENU[low]
            st["offer_course"] = code
            st["stage"] = "payment_pending"
            return msg_payment_link(code, full_name, price, dur, link)

    if stage == "payment_pending":
        txn = raw
        offer = st.get("offer_course", "Unknown")
        st["stage"] = "enrolled"
        ts = datetime.now().strftime("[%Y-%m-%d %H:%M]")
        note = f"{ts} Payment: {txn} Course: {offer}"
        threading.Thread(target=update_lead_status, args=(phone, f"Payment Received: {txn}", note, tenant_id)).start()
        return msg_payment_confirmed(txn, offer, name)

    for kw, idx in KEYWORD_TO_COURSE.items():
        if kw in low:
            c_name = ALL_COURSES[idx][0]
            st["course"] = c_name
            st["stage"]  = "course_viewed"
            # ── Phase 6A: COURSE_VIEWED event (keyword path) ──
            _app = current_app._get_current_object()
            threading.Thread(
                target=log_lead_event_in_thread,
                kwargs=dict(app=_app, phone=phone, event_type="COURSE_VIEWED",
                            event_data=c_name, tenant_id=tenant_id),
                daemon=True,
            ).start()
            # Phase 1.1: if the message is a question, answer it conversationally.
            # Bare keywords (e.g. "pgdca", "dca") keep the deterministic fast-path.
            if _is_question(low):
                _, card = ALL_COURSES[idx]
                ai = gemini_reply(raw, name, context=f"Course details:\n{card}")
                if ai:
                    return ai, "COURSE"
            return msg_course_detail(idx)

    if low in ALL_COURSES:
        c_name = ALL_COURSES[low][0]
        st["course"] = c_name
        st["stage"]  = "course_viewed"
        threading.Thread(target=update_lead_status, args=(phone, f"Viewed: {c_name}", "", tenant_id)).start()
        # ── Phase 6A: COURSE_VIEWED event (direct match path) ──
        _app = current_app._get_current_object()
        threading.Thread(
            target=log_lead_event_in_thread,
            kwargs=dict(app=_app, phone=phone, event_type="COURSE_VIEWED",
                        event_data=c_name, tenant_id=tenant_id),
            daemon=True,
        ).start()
        return msg_course_detail(low)

    ai = gemini_reply(raw, name)
    if ai:
        return ai, "COURSE"

    return smart_fallback(name, raw), "COURSE"
