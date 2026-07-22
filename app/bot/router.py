import threading
from datetime import datetime
from flask import current_app
from app.state import get_or_create_state
from app.bot.constants import (
    ALL_COURSES, COURSE_FEES, KEYWORD_TO_COURSE, GOAL_COURSES,
    OFFER_MENU, COURSE_PAYMENT_LINKS, FULL_FEE_TABLE,
    DEMO_CTA, COURSE_CLOSE, URGENCY_LINES, TRUST_LINES, FEES_VALUE_LINES, pick,
    RUTRONIX_FULL, PSC_NOTE, NORKA_NOTE, LEARNING_MODES,
)
from app.bot.objections import detect_objection, handle_objection
from app.bot.cta_handlers import (
    handle_cta, payment_link_reply,
    CTA_DEMO, CTA_FEES, CTA_VISIT, CTA_CALL, CTA_ENROLL,
)
from app.bot.offer_handlers import (
    offer_menu_reply, handle_offer_number, handle_payment, handle_pay_intent,
)
from app.bot.booking_handlers import handle_date, handle_slot_number
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


def _category_destination(screens, category: str, st):
    """Phase 1.6.4 — resolve a chosen career category to its screen.

    Sets the same canonical stage/goal the legacy numeric goal handler sets, so
    analytics, pipeline and follow-up logic see an identical conversation shape.
    """
    if category == "unsure":
        st["goal"] = ""
        st["stage"] = "not_sure"
        return screens.help_me_choose()

    screen = screens.course_list(category)
    if screen is None:                      # no mapping → nearest valid menu
        st["stage"] = "goal_selection"
        return screens.category_menu()

    st["goal"] = category
    st["stage"] = "course_recommendation"
    return screen


def _course_destination(screens, course_index: str, st, phone: str, tenant_id):
    """Phase 1.6.5 — resolve a chosen course to its Course Details screen.

    Reproduces the legacy course-selection side effects EXACTLY — course, stage,
    the CRM "Viewed:" status update and the COURSE_VIEWED analytics event — so
    CRM and analytics are identical whether the user tapped a course row or
    replied with a legacy number.

    Returns None for an unknown index so the caller falls through to legacy.
    """
    screen = screens.course_details(course_index)
    if screen is None:
        return None

    c_name = ALL_COURSES[course_index][0]
    st["course"] = c_name
    st["stage"] = "course_viewed"
    threading.Thread(
        target=update_lead_status,
        args=(phone, f"Viewed: {c_name}", "", tenant_id),
    ).start()
    _app = current_app._get_current_object()
    threading.Thread(
        target=log_lead_event_in_thread,
        kwargs=dict(app=_app, phone=phone, event_type="COURSE_VIEWED",
                    event_data=c_name, tenant_id=tenant_id),
        daemon=True,
    ).start()
    return screen


def _nearest_menu(screens, action, name: str, st):
    """Phase 1.6.4 — resolve NAV:BACK to the NEAREST VALID menu.

    Rather than always dropping to the Main Menu, walk one level up the
    hierarchy (COURSE → LIST → CATEGORY → MENU) and return the first screen that
    can actually be rendered. When the id carries no target, the current stage
    tells us where the user is.
    """
    target = action.target_screen
    goal = st.get("goal") or ""

    if target == "MENU":
        st["stage"] = "goal_selection"
        return screens.main_menu(name)

    if target == "CATEGORY":
        st["stage"] = "goal_selection"
        return screens.category_menu()

    if target in ("LIST", "COURSE"):
        # Prefer the category named in the id, else the one already chosen.
        category = action.target_arg or goal
        screen = screens.course_list(category) if category else None
        if screen is not None:
            st["goal"] = category
            st["stage"] = "course_recommendation"
            return screen
        st["stage"] = "goal_selection"      # nearest valid level up
        return screens.category_menu()

    # No explicit target — infer the parent level from where the user is now.
    stage = st.get("stage") or ""
    if stage == "course_viewed":            # in course details → back to its list
        screen = screens.course_list(goal) if goal else None
        if screen is not None:
            st["stage"] = "course_recommendation"
            return screen
        st["stage"] = "goal_selection"
        return screens.category_menu()

    if stage in ("course_recommendation", "not_sure"):  # in a list → categories
        st["stage"] = "goal_selection"
        return screens.category_menu()

    st["stage"] = "goal_selection"
    return screens.main_menu(name)


def _try_navigation(raw: str, name: str, st,
                    phone: str = "", tenant_id=None) -> tuple[str, str | None] | None:
    """Phase 1.6.3 — resolve NAV:* navigation ids only.

    Returns (text, preset) when the message is a wired navigation action, else
    None — and None means the caller falls through to the existing legacy router
    completely unchanged.

    Scope for this phase: NAV:MENU and NAV:BACK only. CAT:/CRS:/ACT:/SLOT:/OFR:
    deliberately fall through; they are wired in later phases.

    Responsibilities kept strictly separate:
      - navigation.parse_action() decides WHAT was asked
      - screens.*                 builds the screen content
      - whatsapp_service          renders it
    This function contains no screen text, no payloads and no UI formatting.

    Fail-open: any error returns None so legacy handling always runs. Legacy
    numeric replies and existing interactive ids can never reach this branch,
    because parse_action() only claims ids containing the "NS:VALUE" separator.
    """
    try:
        from app.bot.navigation import (
            parse_action, KIND_MENU, KIND_BACK, KIND_CATEGORY, KIND_COURSE,
            KIND_CTA, KIND_SLOT, KIND_OFFER,
        )

        action = parse_action(raw)
        if action is None or action.kind not in (
            KIND_MENU, KIND_BACK, KIND_CATEGORY, KIND_COURSE, KIND_CTA,
            KIND_SLOT, KIND_OFFER,
        ):
            return None

        from app.bot import screens
        from app.services.whatsapp_service import render_list_text

        if action.kind == KIND_CTA:
            # CTA business logic lives in the handler layer, never here.
            from app.bot.cta_handlers import handle_cta
            return handle_cta(action.value, name, st, phone, tenant_id)

        if action.kind == KIND_SLOT:
            # Booking logic lives in the booking handler layer, never here.
            from app.bot.booking_handlers import handle_slot
            return handle_slot(action.value, st)

        if action.kind == KIND_OFFER:
            # Offer logic lives in the offer handler layer, never here.
            from app.bot.offer_handlers import handle_offer
            return handle_offer(action.value, st)

        if action.kind == KIND_COURSE:
            screen = _course_destination(screens, action.value, st, phone, tenant_id)
            if screen is None:
                return None          # unknown course id → legacy handling
        elif action.kind == KIND_CATEGORY:
            screen = _category_destination(screens, action.value, st)
        elif action.kind == KIND_BACK:
            screen = _nearest_menu(screens, action, name, st)
        else:  # KIND_MENU
            screen = screens.main_menu(name)
            st["stage"] = "goal_selection"

        if screen.kind == screens.KIND_BUTTONS:
            # Phase 1.6.6: the CTA definitions come from the builder, not the
            # router — transport accepts the explicit button list.
            return screen.body, screen.as_buttons()
        return render_list_text(screen.body, screen.as_sections()), None
    except Exception as exc:  # pragma: no cover - defensive
        import logging
        logging.getLogger(__name__).warning(
            "navigation resolve failed — falling back to legacy router: %s", exc
        )
        return None


def msg_welcome(name: str) -> tuple[str, str]:
    text = (
        f"👋 നമസ്കാരം *{name}*!\n\n"
        "*The Oxford Computers*-ലേക്ക് സ്വാഗതം! 🎓\n"
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






def msg_exit(name: str) -> tuple[str, str]:
    text = (
        f"👋 Nandi {name}! Oru nalla divasam nerunnu! 😊\n\n"
        "The Oxford Computers — always here for you.\n"
        "📞 9447329972 | 🌐 theoxfordedu.com\n\n"
        "Thiriche message cheyyoo — happy to help!"
    )
    return text, None


def smart_reply(msg_text: str, name: str, phone: str, is_new_lead: bool, tenant_id: str = None, wa_message_id: str = None) -> tuple[str, str | None]:
    raw = msg_text.strip()
    low = raw.lower()

    from app.services.log_service import resolve_tenant_id
    tenant_id = resolve_tenant_id(tenant_id)

    st = _state(phone, name, tenant_id=tenant_id)
    st["last_msg"]  = datetime.now().isoformat()
    st["last_text"] = raw
    stage   = st["stage"]
    course  = st["course"]

    # ── Phase 1.6.3: navigation resolver, BEFORE every legacy handler ────────
    # Returns None for anything that is not a wired NAV:* action — including all
    # legacy numeric replies and existing interactive ids — so the legacy router
    # below runs completely unchanged.
    _nav = _try_navigation(raw, name, st, phone, tenant_id)
    if _nav is not None:
        return _nav

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

    # ── CTA keyword flows — logic lives in the CTA handler layer ────────────
    if low in {"demo", "free demo", "free class", "book demo"}:
        return handle_cta(CTA_DEMO, name, st, phone, tenant_id)

    if low in {"enroll_now", "enrol_now", "pay_now"}:
        return handle_cta(CTA_ENROLL, name, st, phone, tenant_id)

    if low in {"offer", "today offer", "offer undo", "discount"} or ("offer" in low and "discount" in low):
        st["stage"] = "offer_menu"
        return offer_menu_reply()

    if low in {"pay", "payment", "enrol", "enroll", "seat", "fees pay", "reserve seat"}:
        return handle_pay_intent(st)

    if low in {"fees", "fee", "price", "cost", "ethra", "how much"}:
        return handle_cta(CTA_FEES, name, st, phone, tenant_id)

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
        return handle_cta(CTA_VISIT, name, st, phone, tenant_id)

    if any(w in low for w in CALL_WORDS):
        return handle_cta(CTA_CALL, name, st, phone, tenant_id)

    if "certificate" in low or "certific" in low:
        text = (
            "🏆 *Government Recognised Certificate*\n\n"
            f"✅ {RUTRONIX_FULL}\n"
            "✅ Valid for government & private job applications\n"
            "✅ Accepted for higher studies & skill upgradation\n\n"
            f"📋 {PSC_NOTE}\n"
            f"🌐 {NORKA_NOTE}\n\n"
            "Ithu real government-backed certification aanu 💪\n"
            "Demo kaanumbo full clarity varum — book cheyyatte? 🎓"
        )
        return text, "COURSE"

    if low in {"placement", "job assistance", "placement support", "job guarantee"}:
        text = (
            "💼 *Placement Support*\n\n"
            "✅ Dedicated placement assistance — resume to offer letter\n"
            "✅ Interview coaching & referral network\n"
            "✅ Students Kerala & Gulf-il working aanu 🌍\n\n"
            "Njangal honest aanu — placement *support* tharum,\n"
            "nalla track record und 💪\n\n"
            "Demo kaanumbo full idea varum — book cheyyatte? 🎓"
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
            f"📱 {LEARNING_MODES}\n\n"
            "Ningalude schedule-ku best time parayoo —\n"
            "njan demo book cheyyam! 🎓"
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

    # ── Booking stages — logic lives in the booking handler layer ───────────
    if stage == "demo_time_ask":
        _slot = handle_slot_number(low, st)
        if _slot is not None:
            return _slot

    if stage == "demo_date_ask":
        return handle_date(raw, st, phone, tenant_id)

    # ── Offer stages — logic lives in the offer handler layer ──────────────
    if stage == "offer_menu":
        _offer = handle_offer_number(low, st)
        if _offer is not None:
            return _offer

    if stage == "payment_pending":
        return handle_payment(raw, name, st, phone, tenant_id)

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
                from app.context.assembler import ContextAssembler
                context = ContextAssembler.assemble(
                    tenant_id=tenant_id,
                    phone=phone,
                    wa_message_id=wa_message_id,
                    course_context=f"Course details:\n{card}",
                )
                ai = gemini_reply(raw, name, context=context)
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

    # Phase 1.3B: context assembly delegated to ContextAssembler.
    from app.context.assembler import ContextAssembler
    context = ContextAssembler.assemble(
        tenant_id=tenant_id,
        phone=phone,
        wa_message_id=wa_message_id,
    )
    ai = gemini_reply(raw, name, context=context)
    if ai:
        return ai, "COURSE"

    return smart_fallback(name, raw), "COURSE"
