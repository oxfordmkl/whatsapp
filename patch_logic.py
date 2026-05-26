import re

with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

new_func = """def _get_smart_reply_internal(msg_text, name, phone, is_new_lead):
    msg_lower = msg_text.lower().strip()
    
    if phone not in conversation_state:
        conversation_state[phone] = {
            "name": name, "stage": "new", "course": "Not Selected",
            "last_msg": datetime.now().isoformat(), "last_text": msg_text
        }
        
    state = conversation_state[phone]
    current_stage = state.get("stage", "new")
    course = state.get("course", "Not Selected")

    # Helpers
    is_offer_intent = any(w in msg_lower for w in ["offer", "discount", "special price", "offer undo", "offer unda"])
    is_help_choose = any(w in msg_lower for w in ["which course", "best course", "course select", "help me choose", "not sure", "confused", "entha course", "ethu course"])
    is_job_goal = msg_lower in ["1", "job", "job oriented", "career", "software job", "it job"]
    is_business_goal = msg_lower in ["2", "business", "freelance", "own business"]
    is_basic_goal = msg_lower in ["3", "basic", "basic computer", "office job"]
    is_accounting_goal = msg_lower in ["4", "accounting", "tax", "gst", "finance"]
    is_not_sure_goal = msg_lower in ["5", "not sure", "confused"]

    # a) EXIT
    if msg_lower == "exit":
        state["stage"] = "active"
        return (
            f"👋 നന്ദി {name}!\\n\\n"
            "The Oxford Computers-ൽ നിന്ന് വിളിക്കാം\\n"
            "📞 9447329972\\n\\n"
            "🌐 theoxfordedu.com\\n"
            "വീണ്ടും message cheyyoo! 😊"
        ), "NO_BUTTONS"

    # GREETING OR VERY SHORT NEW LEAD
    greetings = ["hi", "hello", "hai", "hii", "നമസ്കാരം", "hey"]
    if msg_lower in greetings or (is_new_lead and len(msg_lower) <= 2 and not msg_lower.isdigit()):
        state["stage"] = "goal_selection"
        return get_welcome_message(name), "BUTTONS_GOAL"

    # b) CURRENT STAGE HANDLERS (for specific stage inputs like numbers, dates)
    if current_stage == "demo_time_selection" and msg_lower in ["1", "2", "3"]:
        times = {"1": "Morning", "2": "Afternoon", "3": "Evening"}
        selected_time = times[msg_lower]
        state["batch_time"] = selected_time
        state["stage"] = "demo_date_selection"
        return (
            f"✅ {selected_time} confirmed!\\n\\n"
            "Preferred date ഏതാണ്?\\n"
            "(Example: Tomorrow, Monday, April 30)\\n\\n"
            "Date reply cheyyoo! 📅"
        ), "NO_BUTTONS"

    if current_stage == "demo_date_selection" and not is_offer_intent and not is_help_choose:
        user_date = msg_text.strip()
        state["stage"] = "demo_booked"
        batch_time = state.get("batch_time", "")
        status_msg = f"Demo Booked: {course} {batch_time} {user_date}"
        threading.Thread(target=update_lead_status, args=(phone, status_msg)).start()
        return (
            "🎉 *Demo Class Booked!*\\n\\n"
            f"📚 Course: {course}\\n"
            f"⏰ Time: {batch_time}\\n"
            f"📅 Date: {user_date}\\n"
            "📍 The Oxford Computers, Malayinkeezhu\\n\\n"
            "നാളെ ഞങ്ങൾ WhatsApp-ൽ confirm ചെയ്യും!\\n"
            "📞 9447329972 | 🌐 theoxfordedu.com"
        ), "BUTTONS_AFTER_DEMO"

    if current_stage == "offer_selection" and msg_lower in ["1", "2", "3", "4"]:
        offer_map = {
            "1": ("CWPDE", "Certificate in Word Processing & Data Entry", "₹4,800", "6 Months", "https://rzp.io/rzp/xkWdKtd"),
            "2": ("DCA", "Diploma in Computer Applications", "₹6,400", "6 Months", "https://rzp.io/rzp/mJPPtM9x"),
            "3": ("AIDM", "AI-Driven Digital Marketing", "₹19,999", "6 Months", "https://rzp.io/rzp/vF76sj7Y"),
            "4": ("PGDCA", "Post Graduate Diploma in Computer Applications", "₹15,999", "12 Months", "https://rzp.io/rzp/KAQ2C7t"),
        }
        code, full_name, price, dur, link = offer_map[msg_lower]
        state["stage"] = "payment_sent"
        state["offer_course"] = code
        return (
            f"✅ *{code} - Great Choice!*\\n\\n"
            f"📚 {full_name}\\n"
            f"⏱ Duration: {dur}\\n"
            f"🎓 Kerala State Rutronix Approved\\n"
            f"💰 Special Price: *{price}*\\n\\n"
            "✅ Government certified\\n"
            "✅ Receipt after payment\\n"
            f"📍 Oxford Computers, Malayinkeezhu\\n\\n"
            f"👇 *Secure Payment Link:*\\n{link}\\n\\n"
            "Payment ശേഷം Transaction ID\\n"
            "ഇവിടെ reply cheyyoo 📩\\n"
            "(Example: T2504281234)"
        ), "NO_BUTTONS"

    if current_stage == "payment_sent":
        user_txn = msg_text.strip()
        state["stage"] = "enrolled"
        offer_course = state.get("offer_course", "Unknown")
        note_timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M]")
        note_msg = f"{note_timestamp} Payment: {user_txn} Course: {offer_course}"
        threading.Thread(target=update_lead_status, args=(phone, f"Payment Received: {user_txn}", note_msg)).start()
        return (
            "🎉 *Payment Received!*\\n\\n"
            f"✅ Transaction ID: {user_txn}\\n"
            f"📚 Course: {offer_course}\\n"
            f"👤 Name: {name}\\n\\n"
            "Seat confirmed! Welcome to\\n"
            "*The Oxford Computers* 🎓\\n\\n"
            "📞 9447329972 - batch details\\n"
            "📍 Malayinkeezhu, Thiruvananthapuram\\n\\n"
            "കാണാൻ കാത്തിരിക്കുന്നു! 😊"
        ), "BUTTONS_AFTER_DEMO"

    if current_stage == "course_recommendation" and msg_lower in ["1", "2", "3", "4"]:
        goal = state.get("goal", "job")
        courses = GOAL_COURSES.get(goal, GOAL_COURSES["job"])
        idx = int(msg_lower) - 1
        if 0 <= idx < len(courses):
            course_key, display, dur, fee = courses[idx]
            state["stage"] = "course_viewed"
            state["course"] = course_key
            threading.Thread(target=update_lead_status, args=(phone, f"Viewed: {course_key}")).start()
            course_msg_map = {
                "PGDCA": _PGDCA_MSG, "AIDM Digital Marketing": _AIDM_MSG,
                "SAP Financial Accounting": _SAP_MSG, "Python Programming": _PYTHON_MSG,
                "GST & Payroll": _GST_MSG, "DCA Fast Track": _DCA_MSG,
                "Computer Teacher Training": _TEACHER_MSG,
                "Corporate Business Accounting": _ACCOUNTING_MSG,
                "Word Processing & Data Entry": _WORD_MSG,
                "Professional Diploma in Web Designing": _WEB_MSG,
            }
            detail = course_msg_map.get(course_key, "")
            return (
                f"✅ *{display}* - Great choice!\\n\\n"
                f"{detail}\\n"
                f"💰 Fee: {fee} | ⏱ {dur}\\n"
                f"🎓 Kerala State Rutronix Approved\\n\\n"
                f"Free demo class book cheyyatte? 🎓"
            ), "BUTTONS_COURSE"

    # c) INTENT PHRASES (offer/fees/demo/visit/call/help_choose/courses)
    if is_offer_intent:
        state["stage"] = "offer_selection"
        return (
            "🔥 *Today's Special Offer!*\\n"
            "━━━━━━━━━━━━━━━━\\n"
            "🎓 Kerala State Rutronix Approved\\n\\n"
            "1️⃣ CWPDE - ₹4,800 (6M)\\n"
            "2️⃣ DCA - ₹6,400 (6M)\\n"
            "3️⃣ AIDM - ₹19,999 (6M)\\n"
            "4️⃣ PGDCA - ₹15,999 (12M)\\n"
            "━━━━━━━━━━━━━━━━\\n"
            "⚡ Limited seats! Book now!\\n\\n"
            "Number reply cheyyoo 💳"
        ), "BUTTONS_OFFER"

    if msg_lower == "pay":
        if current_stage == "offer_selection":
            return "Please reply with 1, 2, 3, or 4 to select the course you want to enroll in! 💳", "NO_BUTTONS"
        return "To make a payment, please type *OFFER* to see available payment options! 💳", "BUTTONS_OFFER"

    if any(w in msg_lower for w in ["fees", "fee", "price"]):
        # Do not reset stage to active, keep previous stage
        if course and course != "Not Selected" and course in COURSE_FEES:
            fee, duration = COURSE_FEES[course]
            return (
                f"💰 *{course} - Fee Details*\\n\\n"
                f"📋 Fee: {fee}\\n"
                f"⏱ Duration: {duration}\\n"
                f"🎓 Kerala State Rutronix Approved\\n\\n"
                "📊 EMI available - monthly installments!\\n\\n"
                "Ithu one-time investment aanu.\\n"
                "Job kittiyal 1-2 months-il recover cheyyam! 💪\\n\\n"
                "Demo kaanan varamo, atho seat reserve cheyyano?"
            ), "BUTTONS_FEES"
        return FULL_FEE_TABLE, "BUTTONS_FEES"

    if msg_lower in ["demo", "free class", "free demo"]:
        state["stage"] = "demo_time_selection"
        return (
            "🎓 *Free Demo Class Booking*\\n\\n"
            "Preferred batch time ഏത്?\\n\\n"
            "1️⃣ Morning - 9 AM to 11 AM\\n"
            "2️⃣ Afternoon - 12 PM to 2 PM\\n"
            "3️⃣ Evening - 5 PM to 7 PM\\n\\n"
            "Number reply cheyyoo! 📅"
        ), "NO_BUTTONS"

    if any(kw in msg_lower for kw in VISIT_KEYWORDS):
        state["stage"] = "visit_interested"
        threading.Thread(target=update_lead_status, args=(phone, "Office Visit Interested")).start()
        return (
            f"🏢 *Office Visit - Welcome {name}!*\\n\\n"
            "📍 *The Oxford Computers*\\n"
            "   Malayinkeezhu Junction\\n"
            "   Thiruvananthapuram, Kerala\\n\\n"
            "⏰ Office Hours: 9 AM – 7 PM (Mon-Sat)\\n"
            "📞 9447329972\\n\\n"
            "Eppol varananu convenient?\\n"
            "Morning / Afternoon / Evening? 😊"
        ), "BUTTONS_COURSE"

    if any(kw in msg_lower for kw in HANDOFF_KEYWORDS):
        state["stage"] = "call_requested"
        threading.Thread(target=update_lead_status, args=(phone, "Call Requested")).start()
        return (
            f"😊 Of course {name}!\\n\\n"
            "Oru experienced counselor connect cheyyam.\\n"
            "📞 *9447329972* - vilikku!\\n\\n"
            "⏰ Available: 9 AM – 7 PM (Mon-Sat)\\n"
            "📍 Oxford Computers, Malayinkeezhu\\n\\n"
            "Allenkil ividé message cheyyoo,\\n"
            "njan help cheyyam! 🙌"
        ), "NO_BUTTONS"

    if is_help_choose or is_not_sure_goal:
        state["stage"] = "not_sure"
        if gemini_client:
            # We pass return_fallback=False so we can handle failure gracefully
            # without triggering the generic fallback which says "10 courses und!"
            reply = get_gemini_reply(
                f"Student {name} is unsure which course to pick. Ask about their qualification, interest, and career goal. Then suggest the best course.", name, return_fallback=False
            )
            if reply:
                return reply, "BUTTONS_GOAL"
        return (
            f"{name}, no problem! 😊\\n\\n"
            "Ningalude qualification enthanu?\\n"
            "Eppol enthu cheyyunnu?\\n"
            "Ethu type job aanu interest?\\n\\n"
            "Reply cheyyoo - best course recommend cheyyam! 🎓"
        ), "BUTTONS_GOAL"

    if msg_lower in ["course", "courses", "padikkanam", "study"]:
        state["stage"] = "goal_selection"
        return get_welcome_message(name), "BUTTONS_GOAL"

    # d) GOAL WORDS
    if is_job_goal:
        goal = "job"
        courses = GOAL_COURSES[goal]
        state["stage"] = "course_recommendation"
        state["goal"] = goal
        lines = ["📚 *നിങ്ങൾക്ക് best ആയ courses:*\\n"]
        for i, (_, display, dur, fee) in enumerate(courses, 1):
            lines.append(f"{i}️⃣ {display} - {dur} - {fee}")
        lines.append("\\nCourse number reply cheyyoo! 🎓")
        return "\\n".join(lines), "NO_BUTTONS"

    if is_business_goal:
        goal = "business"
        courses = GOAL_COURSES[goal]
        state["stage"] = "course_recommendation"
        state["goal"] = goal
        lines = ["📚 *നിങ്ങൾക്ക് best ആയ courses:*\\n"]
        for i, (_, display, dur, fee) in enumerate(courses, 1):
            lines.append(f"{i}️⃣ {display} - {dur} - {fee}")
        lines.append("\\nCourse number reply cheyyoo! 🎓")
        return "\\n".join(lines), "NO_BUTTONS"

    if is_basic_goal:
        goal = "basic"
        courses = GOAL_COURSES[goal]
        state["stage"] = "course_recommendation"
        state["goal"] = goal
        lines = ["📚 *നിങ്ങൾക്ക് best ആയ courses:*\\n"]
        for i, (_, display, dur, fee) in enumerate(courses, 1):
            lines.append(f"{i}️⃣ {display} - {dur} - {fee}")
        lines.append("\\nCourse number reply cheyyoo! 🎓")
        return "\\n".join(lines), "NO_BUTTONS"

    if is_accounting_goal:
        goal = "accounting"
        courses = GOAL_COURSES[goal]
        state["stage"] = "course_recommendation"
        state["goal"] = goal
        lines = ["📚 *നിങ്ങൾക്ക് best ആയ courses:*\\n"]
        for i, (_, display, dur, fee) in enumerate(courses, 1):
            lines.append(f"{i}️⃣ {display} - {dur} - {fee}")
        lines.append("\\nCourse number reply cheyyoo! 🎓")
        return "\\n".join(lines), "NO_BUTTONS"

    # e) COURSE KEYWORDS
    if msg_lower in ["timing", "batch"]:
        return (
            "⏰ *Batch Timings*\\n\\n"
            "🌅 Morning: 9 AM – 11 AM\\n"
            "☀️ Afternoon: 12 PM – 2 PM\\n"
            "🌆 Evening: 5 PM – 7 PM\\n\\n"
            "Weekend batches available! 📅\\n"
            "Preferred timing reply cheyyoo!"
        ), "BUTTONS_COURSE"

    if msg_lower == "certificate":
        return (
            "🏆 *Government Certified Certificate*\\n\\n"
            "✅ Kerala State Rutronix approved\\n"
            "✅ Job interviews-ൽ valid\\n\\n"
            "Real government-backed certification! 💪"
        ), "BUTTONS_COURSE"

    if msg_lower in ["placement", "job assistance", "placement support"]:
        return (
            "💼 *Placement Support*\\n\\n"
            "✅ 100% placement assistance\\n"
            "✅ Resume preparation & Interview coaching\\n\\n"
            "Students Kerala & Gulf-ൽ working! 🌟"
        ), "BUTTONS_COURSE"

    course_kw_map = {
        "pgdca": _PGDCA_MSG, "pgd": _PGDCA_MSG,
        "aidm": _AIDM_MSG, "sap": _SAP_MSG,
        "python": _PYTHON_MSG, "gst": _GST_MSG, "tally": _GST_MSG,
        "dca": _DCA_MSG, "teacher": _TEACHER_MSG,
        "accounting": _ACCOUNTING_MSG, "web": _WEB_MSG,
    }
    if msg_lower in course_kw_map:
        state["stage"] = "course_viewed"
        kw_to_course = {
            "pgdca": "PGDCA", "pgd": "PGDCA", "aidm": "AIDM Digital Marketing",
            "sap": "SAP Financial Accounting", "python": "Python Programming",
            "gst": "GST & Payroll", "tally": "GST & Payroll",
            "dca": "DCA Fast Track", "teacher": "Computer Teacher Training",
            "accounting": "Corporate Business Accounting", "web": "Professional Diploma in Web Designing",
        }
        cname = kw_to_course.get(msg_lower, "Not Selected")
        state["course"] = cname
        fee, dur = COURSE_FEES.get(cname, ("", ""))
        return course_kw_map[msg_lower] + f"\\n\\n💰 Fee: {fee} | ⏱ {dur}\\n🎓 Kerala State Rutronix Approved\\n\\nFree demo class book cheyyatte? 🎓", "BUTTONS_COURSE"

    if msg_lower in [str(i) for i in range(1, 11)]:
        courses_dict = {
            "1": ("PGDCA", _PGDCA_MSG),
            "2": ("AIDM Digital Marketing", _AIDM_MSG),
            "3": ("SAP Financial Accounting", _SAP_MSG),
            "4": ("Python Programming", _PYTHON_MSG),
            "5": ("GST & Payroll", _GST_MSG),
            "6": ("DCA Fast Track", _DCA_MSG),
            "7": ("Computer Teacher Training", _TEACHER_MSG),
            "8": ("Corporate Business Accounting", _ACCOUNTING_MSG),
            "9": ("Word Processing & Data Entry", _WORD_MSG),
            "10": ("Professional Diploma in Web Designing", _WEB_MSG),
        }
        info = courses_dict.get(msg_lower)
        if info:
            cname, cdetail = info
            state["stage"] = "course_viewed"
            state["course"] = cname
            threading.Thread(target=update_lead_status, args=(phone, f"Viewed: {cname}")).start()
            fee, dur = COURSE_FEES.get(cname, ("", ""))
            return f"✅ *{cname}* - Great choice!\\n\\n{cdetail}\\n\\n💰 Fee: {fee} | ⏱ {dur}\\n🎓 Kerala State Rutronix Approved\\n\\nFree demo class book cheyyatte? 🎓", "BUTTONS_COURSE"

    # f) GEMINI FALLBACK
    if gemini_client:
        try:
            return get_gemini_reply(msg_text, name), "BUTTONS_COURSE"
        except Exception:
            return get_smart_fallback(name, msg_text), "BUTTONS_COURSE"
    return get_smart_fallback(name, msg_text), "BUTTONS_COURSE"
"""

start_marker = "def _get_smart_reply_internal("
end_marker = "def get_welcome_message("
start_idx = content.find(start_marker)
end_idx = content.find(end_marker)

if start_idx != -1 and end_idx != -1:
    content = content[:start_idx] + new_func + "\n" + content[end_idx:]
    with open('app.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Patched correctly.")
else:
    print("Could not find markers")
