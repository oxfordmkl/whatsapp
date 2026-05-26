import re

with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Update Word Processing Duration
content = content.replace("3 Months — ₹4,800", "6 Months — ₹4,800")
content = content.replace("⏱ Duration: 3 Months\n🎓 Certificate: Rutronix\n📋 Key Modules: Touch Typing, MS Word Processing, Data Entry Techniques, Basic DTP, Office Document Management\n💡 Best for: Data entry professionals and beginners\n\nDemo class-naayi *DEMO* reply cheyyoo! 🙌", "⏱ Duration: 6 Months\n🎓 Certificate: Rutronix\n📋 Key Modules: Touch Typing, MS Word Processing, Data Entry Techniques, Basic DTP, Office Document Management\n💡 Best for: Data entry professionals and beginners\n\nDemo class-naayi *DEMO* reply cheyyoo! 🙌")

# Replace _WORD_MSG duration if it exists separately
_word_msg_start = content.find("_WORD_MSG = (")
if _word_msg_start != -1:
    _word_msg_end = content.find(")", _word_msg_start)
    old_word_msg = content[_word_msg_start:_word_msg_end+1]
    new_word_msg = old_word_msg.replace("3 Months", "6 Months")
    content = content.replace(old_word_msg, new_word_msg)

# Update INSTITUTE_INFO
content = content.replace("Word Processing & Data Entry — 3 Months", "Word Processing & Data Entry — 6 Months")

# Define the new Smart Reply Engine
new_smart_reply = """# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SMART REPLY ENGINE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Goal → Course mapping (relative numbering)
GOAL_COURSES = {
    "job": [
        ("PGDCA", "PGDCA — Post Graduate Diploma", "12 Months", "₹15,999"),
        ("Python Programming", "Python Programming", "3 Months", "₹4,499"),
        ("Professional Diploma in Web Designing", "Web Designing", "6 Months", "₹5,999"),
        ("DCA Fast Track", "DCA — Fast Track Diploma", "6 Months", "₹6,400"),
    ],
    "business": [
        ("AIDM Digital Marketing", "AI-Driven Digital Marketing", "6 Months", "₹19,999"),
        ("Professional Diploma in Web Designing", "Web Designing", "6 Months", "₹5,999"),
        ("Python Programming", "Python Programming", "3 Months", "₹4,499"),
    ],
    "basic": [
        ("DCA Fast Track", "DCA — Fast Track Diploma", "6 Months", "₹6,400"),
        ("Word Processing & Data Entry", "Word Processing & Data Entry", "6 Months", "₹4,800"),
        ("Computer Teacher Training", "Computer Teacher Training", "1 Year", "₹7,999"),
    ],
    "accounting": [
        ("SAP Financial Accounting", "SAP Financial Accounting", "4-6 Months", "₹11,999"),
        ("GST & Payroll", "GST & Payroll Diploma", "6 Months", "₹5,499"),
        ("Corporate Business Accounting", "Corporate Business Accounting", "1 Year", "₹7,999"),
    ],
}

FULL_FEE_TABLE = (
    "💰 *Course Fees — The Oxford Computers*\\n"
    "━━━━━━━━━━━━━━━━\\n"
    "1. PGDCA — ₹15,999 (12M)\\n"
    "2. AIDM Digital Marketing — ₹19,999 (6M)\\n"
    "3. SAP Accounting — ₹11,999 (4-6M)\\n"
    "4. Python — ₹4,499 (3M)\\n"
    "5. GST & Payroll — ₹5,499 (6M)\\n"
    "6. DCA Fast Track — ₹6,400 (6M)\\n"
    "7. Teacher Training — ₹7,999 (1Y)\\n"
    "8. Business Accounting — ₹7,999 (1Y)\\n"
    "9. Data Entry — ₹4,800 (6M)\\n"
    "10. Web Designing — ₹5,999 (6M)\\n"
    "━━━━━━━━━━━━━━━━\\n"
    "🎓 All courses Kerala State Rutronix Approved\\n"
    "📊 EMI facility available!\\n\\n"
    "Ithu one-time investment aanu.\\n"
    "Job kittiyal 1-2 months-il recover cheyyam! 💪\\n\\n"
    "Demo kaanan varamo, atho seat reserve cheyyano?"
)

COURSE_FEES = {
    "PGDCA": ("₹15,999", "12 Months"),
    "AIDM Digital Marketing": ("₹19,999", "6 Months"),
    "SAP Financial Accounting": ("₹11,999", "4-6 Months"),
    "Python Programming": ("₹4,499", "3 Months"),
    "GST & Payroll": ("₹5,499", "6 Months"),
    "DCA Fast Track": ("₹6,400", "6 Months"),
    "Computer Teacher Training": ("₹7,999", "1 Year"),
    "Corporate Business Accounting": ("₹7,999", "1 Year"),
    "Word Processing & Data Entry": ("₹4,800", "6 Months"),
    "Professional Diploma in Web Designing": ("₹5,999", "6 Months"),
}

VISIT_KEYWORDS = ["visit", "office", "varam", "neritt", "address", "location", "എവിടെ", "വരാം"]
HANDOFF_KEYWORDS = ["call me", "counselor", "confused", "doubt", "office number", "talk to counselor", "വിളിക്കൂ", "സംശയം"]

def get_smart_reply(msg_text, name, phone, is_new_lead):
    reply, btn_preset = _get_smart_reply_internal(msg_text, name, phone, is_new_lead)
    if msg_text.lower().strip() == "exit":
        btn_preset = "NO_BUTTONS"
    return reply, btn_preset

def _get_smart_reply_internal(msg_text, name, phone, is_new_lead):
    msg_lower = msg_text.lower().strip()
    
    if phone not in conversation_state:
        conversation_state[phone] = {
            "name": name, "stage": "new", "course": "Not Selected",
            "last_msg": datetime.now().isoformat(), "last_text": msg_text
        }
        
    state = conversation_state[phone]
    current_stage = state.get("stage", "new")
    course = state.get("course", "Not Selected")

    # 1. New lead check: ALWAYS show goal selection
    if is_new_lead:
        state["stage"] = "goal_selection"
        return get_welcome_message(name), "BUTTONS_GOAL"

    # 2. KEYWORD INTERCEPTS
    if msg_lower == "exit":
        state["stage"] = "active"
        return (
            f"👋 നന്ദി {name}!\\n\\n"
            "The Oxford Computers-ൽ നിന്ന് വിളിക്കാം\\n"
            "📞 9447329972\\n\\n"
            "🌐 theoxfordedu.com\\n"
            "വീണ്ടും message cheyyoo! 😊"
        ), "NO_BUTTONS"

    if msg_lower in ["demo", "free class", "free demo"]:
        state["stage"] = "demo_time_selection"
        return (
            "🎓 *Free Demo Class Booking*\\n\\n"
            "Preferred batch time ഏത്?\\n\\n"
            "1️⃣ Morning — 9 AM to 11 AM\\n"
            "2️⃣ Afternoon — 12 PM to 2 PM\\n"
            "3️⃣ Evening — 5 PM to 7 PM\\n\\n"
            "Number reply cheyyoo! 📅"
        ), "NO_BUTTONS"

    if msg_lower == "offer":
        state["stage"] = "offer_selection"
        return (
            "🔥 *Today's Special Offer!*\\n"
            "━━━━━━━━━━━━━━━━\\n"
            "🎓 Kerala State Rutronix Approved\\n\\n"
            "1️⃣ CWPDE — ₹4,800 (6M)\\n"
            "2️⃣ DCA — ₹6,400 (6M)\\n"
            "3️⃣ AIDM — ₹19,999 (6M)\\n"
            "4️⃣ PGDCA — ₹15,999 (12M)\\n"
            "━━━━━━━━━━━━━━━━\\n"
            "⚡ Limited seats! Book now!\\n\\n"
            "Number reply cheyyoo 💳"
        ), "BUTTONS_OFFER"

    if msg_lower == "pay":
        if current_stage == "offer_selection":
            return "Please reply with 1, 2, 3, or 4 to select the course you want to enroll in! 💳", "NO_BUTTONS"
        return "To make a payment, please type *OFFER* to see available payment options! 💳", "BUTTONS_OFFER"

    if msg_lower in ["fees", "fee", "price"]:
        state["stage"] = "active"
        if course and course != "Not Selected" and course in COURSE_FEES:
            fee, duration = COURSE_FEES[course]
            return (
                f"💰 *{course} — Fee Details*\\n\\n"
                f"📋 Fee: {fee}\\n"
                f"⏱ Duration: {duration}\\n"
                f"🎓 Kerala State Rutronix Approved\\n\\n"
                "📊 EMI available — monthly installments!\\n\\n"
                "Ithu one-time investment aanu.\\n"
                "Job kittiyal 1-2 months-il recover cheyyam! 💪\\n\\n"
                "Demo kaanan varamo, atho seat reserve cheyyano?"
            ), "BUTTONS_FEES"
        return FULL_FEE_TABLE, "BUTTONS_FEES"

    if msg_lower in ["courses", "course"]:
        state["stage"] = "goal_selection"
        return get_welcome_message(name), "BUTTONS_GOAL"

    if any(kw in msg_lower for kw in VISIT_KEYWORDS):
        state["stage"] = "visit_interested"
        threading.Thread(target=update_lead_status, args=(phone, "Office Visit Interested")).start()
        return (
            f"🏢 *Office Visit — Welcome {name}!*\\n\\n"
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
            "📞 *9447329972* — vilikku!\\n\\n"
            "⏰ Available: 9 AM – 7 PM (Mon-Sat)\\n"
            "📍 Oxford Computers, Malayinkeezhu\\n\\n"
            "Allenkil ividé message cheyyoo,\\n"
            "njan help cheyyam! 🙌"
        ), "NO_BUTTONS"

    # 3. STAGE-BASED FLOWS
    if current_stage == "goal_selection":
        goal_map = {"1": "job", "2": "business", "3": "basic", "4": "accounting"}
        if msg_lower in goal_map:
            goal = goal_map[msg_lower]
            courses = GOAL_COURSES[goal]
            state["stage"] = "course_recommendation"
            state["goal"] = goal
            lines = ["📚 *നിങ്ങൾക്ക് best ആയ courses:*\\n"]
            for i, (_, display, dur, fee) in enumerate(courses, 1):
                lines.append(f"{i}️⃣ {display} — {dur} — {fee}")
            lines.append("\\nCourse number reply cheyyoo! 🎓")
            return "\\n".join(lines), "NO_BUTTONS"
        elif msg_lower == "5":
            state["stage"] = "not_sure"
            if gemini_client:
                try:
                    return get_gemini_reply(
                        f"Student {name} is unsure which course to pick. Ask about their qualification, interest, and career goal. Then suggest the best course.", name
                    ), "BUTTONS_GOAL"
                except Exception:
                    pass
            return (
                f"{name}, no problem! 😊\\n\\n"
                "Ningalude qualification enthanu?\\n"
                "Eppol enthu cheyyunnu?\\n"
                "Ethu type job aanu interest?\\n\\n"
                "Reply cheyyoo — best course recommend cheyyam! 🎓"
            ), "BUTTONS_GOAL"

    if current_stage == "course_recommendation":
        goal = state.get("goal", "job")
        courses = GOAL_COURSES.get(goal, GOAL_COURSES["job"])
        idx = int(msg_lower) - 1 if msg_lower.isdigit() else -1
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
                f"✅ *{display}* — Great choice!\\n\\n"
                f"{detail}\\n"
                f"💰 Fee: {fee} | ⏱ {dur}\\n"
                f"🎓 Kerala State Rutronix Approved\\n\\n"
                f"Free demo class book cheyyatte? 🎓"
            ), "BUTTONS_COURSE"

    if current_stage == "demo_time_selection":
        if msg_lower in ["1", "2", "3"]:
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

    if current_stage == "demo_date_selection":
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

    if current_stage == "offer_selection":
        offer_map = {
            "1": ("CWPDE", "Certificate in Word Processing & Data Entry", "₹4,800", "6 Months", "https://rzp.io/rzp/xkWdKtd"),
            "2": ("DCA", "Diploma in Computer Applications", "₹6,400", "6 Months", "https://rzp.io/rzp/mJPPtM9x"),
            "3": ("AIDM", "AI-Driven Digital Marketing", "₹19,999", "6 Months", "https://rzp.io/rzp/vF76sj7Y"),
            "4": ("PGDCA", "Post Graduate Diploma in Computer Applications", "₹15,999", "12 Months", "https://rzp.io/rzp/KAQ2C7t"),
        }
        if msg_lower in offer_map:
            code, full_name, price, dur, link = offer_map[msg_lower]
            state["stage"] = "payment_sent"
            state["offer_course"] = code
            return (
                f"✅ *{code} — Great Choice!*\\n\\n"
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
            "📞 9447329972 — batch details\\n"
            "📍 Malayinkeezhu, Thiruvananthapuram\\n\\n"
            "കാണാൻ കാത്തിരിക്കുന്നു! 😊"
        ), "BUTTONS_AFTER_DEMO"

    # 4. EXACT KEYWORDS → keyword reply + fee
    exact_keywords = [
        "timing", "batch", "certificate", "job", "placement", "നമസ്കാരം",
        "pgdca", "pgd", "aidm", "sap", "python", "gst",
        "tally", "dca", "teacher", "accounting", "web",
        "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"
    ]

    if msg_lower not in exact_keywords and gemini_client:
        try:
            return get_gemini_reply(msg_text, name), "BUTTONS_COURSE"
        except Exception:
            return get_smart_fallback(name, msg_text), "BUTTONS_COURSE"

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

    if msg_lower in ["job", "placement"]:
        return (
            "💼 *Placement Support*\\n\\n"
            "✅ 100% placement assistance\\n"
            "✅ Resume preparation & Interview coaching\\n\\n"
            "Students Kerala & Gulf-ൽ working! 🌟"
        ), "BUTTONS_COURSE"

    # Individual course keywords
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
        courses = {
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
        info = courses.get(msg_lower)
        if info:
            cname, cdetail = info
            state["stage"] = "course_viewed"
            state["course"] = cname
            threading.Thread(target=update_lead_status, args=(phone, f"Viewed: {cname}")).start()
            fee, dur = COURSE_FEES.get(cname, ("", ""))
            return f"✅ *{cname}* — Great choice!\\n\\n{cdetail}\\n\\n💰 Fee: {fee} | ⏱ {dur}\\n🎓 Kerala State Rutronix Approved\\n\\nFree demo class book cheyyatte? 🎓", "BUTTONS_COURSE"

    if gemini_client:
        try:
            return get_gemini_reply(msg_text, name), "BUTTONS_COURSE"
        except Exception:
            return get_smart_fallback(name, msg_text), "BUTTONS_COURSE"
    return get_smart_fallback(name, msg_text), "BUTTONS_COURSE"

def get_welcome_message(name):
    return (
        f"👋 നമസ്കാരം *{name}*!\\n\\n"
        "*The Oxford Computers*-ലേക്ക് സ്വാഗതം! 🎓\\n"
        "Kerala Govt Certified • AI-Enabled Courses\\n\\n"
        "നിങ്ങൾ എന്താണ് ലക്ഷ്യം? 🤔\\n\\n"
        "1️⃣ Job Oriented — IT/Software career\\n"
        "2️⃣ Business/Freelance\\n"
        "3️⃣ Basic Computer/Office Job\\n"
        "4️⃣ Accounting/Tax\\n"
        "5️⃣ Not sure — help me choose\\n\\n"
        "Number reply cheyyoo! 📝"
    )
"""

# Extract the old smart reply section
start_marker = "def add_menu_footer(message):"
if start_marker not in content:
    start_marker = "def get_smart_reply("
end_marker = "def get_gemini_reply("

start_idx = content.find(start_marker)
end_idx = content.find(end_marker)

content = content[:start_idx] + new_smart_reply + "\n\n" + content[end_idx:]

with open('app.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Updated app.py")
