import re

with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

start_marker = "def _get_smart_reply_internal(msg_text, name, phone, is_new_lead):"
end_marker = "def get_welcome_message(name):"

start_idx = content.find(start_marker)
end_idx = content.find(end_marker)

new_function = """def _get_smart_reply_internal(msg_text, name, phone, is_new_lead):
    msg_lower = msg_text.lower().strip()
    
    # Ensure conversation_state has default keys
    if phone not in conversation_state:
        conversation_state[phone] = {
            "name": name, 
            "stage": "new", 
            "course": "Not Selected",
            "last_msg": datetime.now().isoformat(),
            "last_text": msg_text
        }
        
    state = conversation_state[phone]
    current_stage = state.get("stage", "new")
    course = state.get("course", "Not Selected")

    # 1. New lead check
    if is_new_lead:
        state["stage"] = "active"
        return get_welcome_message(name), "COURSES"

    # 2. Check stage
    if current_stage == "demo_time_selection":
        if msg_lower in ["1", "2", "3"]:
            times = {"1": "Morning", "2": "Afternoon", "3": "Evening"}
            selected_time = times[msg_lower]
            state["batch_time"] = selected_time
            state["stage"] = "demo_date_selection"
            return (
                f"✅ {selected_time} confirmed!\\n\\n"
                f"Preferred date ഏതാണ്?\\n"
                f"(Example: Tomorrow, Monday, April 30)\\n\\n"
                f"Date reply cheyyoo! 📅"
            ), "NO_BUTTONS"

    if current_stage == "demo_date_selection":
        user_date = msg_text.strip()
        state["stage"] = "demo_booked"
        batch_time = state.get("batch_time", "")
        
        status_msg = f"Demo Booked: {course} {batch_time} {user_date}"
        threading.Thread(
            target=update_lead_status,
            args=(phone, status_msg)
        ).start()
        
        return (
            f"🎉 *Demo Class Booked!*\\n\\n"
            f"📚 Course: {course}\\n"
            f"⏰ Time: {batch_time}\\n"
            f"📅 Date: {user_date}\\n"
            f"📍 The Oxford Computers, Malayinkeezhu\\n\\n"
            f"നാളെ ഞങ്ങൾ WhatsApp-ൽ confirm ചെയ്യും!\\n"
            f"കൂടുതൽ info: 📞 9447329972\\n"
            f"🌐 theoxfordedu.com"
        ), "NO_BUTTONS"

    if current_stage == "offer_selection":
        if msg_lower == "1":
            state["stage"] = "payment_sent"
            state["offer_course"] = "CWPDE"
            return (
                "✅ *CWPDE — Great Choice!*\\n\\n"
                "📚 Certificate in Word Processing & Data Entry\\n"
                "⏱ Duration: 6 Months\\n"
                "🎓 Certificate: Rutronix Approved\\n"
                "💰 Special Price: *₹4,800*\\n"
                "📌 Exam Fee: separately as per Rutronix norms\\n\\n"
                "👇 *Secure Payment Link:*\\n"
                "https://rzp.io/rzp/xkWdKtd\\n\\n"
                "Payment ചെയ്ത ശേഷം:\\n"
                "📩 Transaction ID ഇവിടെ reply cheyyoo\\n"
                "(Example: T2504281234)\\n\\n"
                "Seat ഉടൻ confirm ആകും! 🎉\\n"
                "📍 The Oxford Computers, Malayinkeezhu\\n"
                "📞 9447329972"
            ), "NO_BUTTONS"
        elif msg_lower == "2":
            state["stage"] = "payment_sent"
            state["offer_course"] = "DCA"
            return (
                "✅ *DCA — Great Choice!*\\n\\n"
                "📚 Diploma in Computer Applications (Fast Track)\\n"
                "⏱ Duration: 6 Months\\n"
                "🎓 Certificate: Rutronix + State Approved\\n"
                "💰 Special Price: *₹6,400*\\n"
                "📌 Exam Fee: separately as per Rutronix norms\\n\\n"
                "👇 *Secure Payment Link:*\\n"
                "https://rzp.io/rzp/mJPPtM9x\\n\\n"
                "Payment ചെയ്ത ശേഷം:\\n"
                "📩 Transaction ID ഇവിടെ reply cheyyoo\\n"
                "(Example: T2504281234)\\n\\n"
                "Seat ഉടൻ confirm ആകും! 🎉\\n"
                "📍 The Oxford Computers, Malayinkeezhu\\n"
                "📞 9447329972"
            ), "NO_BUTTONS"
        elif msg_lower == "3":
            state["stage"] = "payment_sent"
            state["offer_course"] = "AIDM"
            return (
                "✅ *AIDM — Great Choice!*\\n\\n"
                "📚 AI-Driven Digital Marketing\\n"
                "⏱ Duration: 6 Months\\n"
                "🎓 Certificate: Industry Recognized\\n"
                "💰 Special Price: *₹19,999*\\n"
                "📌 Exam Fee: separately as per Rutronix norms\\n\\n"
                "👇 *Secure Payment Link:*\\n"
                "https://rzp.io/rzp/vF76sj7Y\\n\\n"
                "Payment ചെയ്ത ശേഷം:\\n"
                "📩 Transaction ID ഇവിടെ reply cheyyoo\\n"
                "(Example: T2504281234)\\n\\n"
                "Seat ഉടൻ confirm ആകും! 🎉\\n"
                "📍 The Oxford Computers, Malayinkeezhu\\n"
                "📞 9447329972"
            ), "NO_BUTTONS"
        elif msg_lower == "4":
            state["stage"] = "payment_sent"
            state["offer_course"] = "PGDCA"
            return (
                "✅ *PGDCA — Great Choice!*\\n\\n"
                "📚 Post Graduate Diploma in Computer Applications\\n"
                "⏱ Duration: 12 Months\\n"
                "🎓 Certificate: Rutronix + State Approved\\n"
                "💰 Special Price: *₹15,999*\\n"
                "📌 Exam Fee: separately as per Rutronix norms\\n\\n"
                "👇 *Secure Payment Link:*\\n"
                "https://rzp.io/rzp/KAQ2C7t\\n\\n"
                "Payment ചെയ്ത ശേഷം:\\n"
                "📩 Transaction ID ഇവിടെ reply cheyyoo\\n"
                "(Example: T2504281234)\\n\\n"
                "Seat ഉടൻ confirm ആകും! 🎉\\n"
                "📍 The Oxford Computers, Malayinkeezhu\\n"
                "📞 9447329972"
            ), "NO_BUTTONS"

    if current_stage == "payment_sent":
        user_txn = msg_text.strip()
        state["stage"] = "enrolled"
        offer_course = state.get("offer_course", "Unknown")
        
        note_timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M]")
        note_msg = f"{note_timestamp} OFFER Payment: {user_txn} Course: {offer_course}"
        status_msg = f"Payment Received: {user_txn}"
        
        threading.Thread(
            target=update_lead_status,
            args=(phone, status_msg, note_msg)
        ).start()
        
        return (
            f"🎉 *Payment Received!*\\n\\n"
            f"✅ Transaction ID: {user_txn}\\n"
            f"📚 Course: {offer_course}\\n"
            f"👤 Name: {name}\\n\\n"
            f"Seat confirmed! Welcome to \\n"
            f"*The Oxford Computers* 🎓\\n\\n"
            f"📞 Next step: 9447329972 ലേക്ക് \\n"
            f"വിളിക്കൂ — batch details കിട്ടും!\\n\\n"
            f"📍 Malayinkeezhu, Thiruvananthapuram\\n"
            f"🌐 theoxfordedu.com\\n\\n"
            f"കാണാൻ കാത്തിരിക്കുന്നു! 😊"
        ), "NO_BUTTONS"

    # 3. Check for long messages to route to Gemini
    if len(msg_lower) > 20:
        # Long message — use Gemini for smart reply
        if gemini_client:
            return get_gemini_reply(msg_text, name), None
        return get_fallback_reply(name), None

    # 4. Short message — check keywords
    # Explicit 'demo'
    if msg_lower == "demo" or (len(msg_lower) <= 20 and "free class" in msg_lower):
        state["stage"] = "demo_time_selection"
        return (
            "🎓 *Free Demo Class Booking*\\n\\n"
            "Preferred batch time ഏത്?\\n\\n"
            "1️⃣ Morning — 9 AM to 11 AM\\n"
            "2️⃣ Afternoon — 12 PM to 2 PM  \\n"
            "3️⃣ Evening — 5 PM to 7 PM\\n\\n"
            "Number reply cheyyoo! 📅"
        ), "DEMO"

    # Explicit 'offer'
    if msg_lower == "offer" or (len(msg_lower) <= 20 and "offer" in msg_lower):
        state["stage"] = "offer_selection"
        return (
            "🔥 *Today's Special Offer!*\\n"
            "━━━━━━━━━━━━━━━━\\n"
            "🎓 Kerala State Rutronix Approved\\n\\n"
            "1️⃣ CWPDE — Word Processing & Data Entry\\n"
            "   💰 Special Price: *₹4,800*\\n"
            "   ⏱ Duration: 6 Months\\n\\n"
            "2️⃣ DCA — Diploma in Computer Applications\\n"
            "   💰 Special Price: *₹6,400*\\n"
            "   ⏱ Duration: 6 Months\\n\\n"
            "3️⃣ AIDM — AI-Driven Digital Marketing\\n"
            "   💰 Special Price: *₹19,999*\\n"
            "   ⏱ Duration: 6 Months\\n\\n"
            "4️⃣ PGDCA — Post Graduate Diploma\\n"
            "   💰 Special Price: *₹15,999*\\n"
            "   ⏱ Duration: 12 Months\\n"
            "━━━━━━━━━━━━━━━━\\n"
            "⚡ Limited Time Offer!\\n"
            "📅 Seats limited — Book now!\\n\\n"
            "Number reply cheyyoo — \\n"
            "Payment link ഉടൻ അയക്കാം! 💳"
        ), "OFFER"

    # KEYWORD_REPLIES loop
    for keyword, reply in KEYWORD_REPLIES.items():
        if msg_lower == keyword or (len(msg_lower) <= 20 and keyword in msg_lower):
            state["stage"] = "active"  # Reset stage
            if reply is None:  # Greeting keyword
                return get_welcome_message(name), "COURSES"
            if keyword in ["courses", "course"]:
                exc = "COURSES"
            elif keyword in ["fee", "fees", "price"]:
                exc = "FEES"
            else:
                exc = None
            return reply, exc

    # 5. Course number selection (1-10)
    if msg_lower in ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]:
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
            "10": ("Professional Diploma in Web Designing", _WEB_MSG)
        }
        course_info = courses.get(msg_lower)
        if course_info:
            course_name, course_details = course_info
            state["stage"] = "demo_cta"
            state["course"] = course_name
            threading.Thread(
                target=update_lead_status,
                args=(phone, f"Viewed: {course_name}")
            ).start()
            return (
                f"✅ *{course_name}* — Great choice!\\n\\n"
                f"{course_details}\\n"
                f"📍 The Oxford Computers, Malayinkeezhu"
            ), "COURSES"

    # 6. Gemini AI fallback
    if gemini_client:
        return get_gemini_reply(msg_text, name), None

    # Fallback if no AI configured
    return get_fallback_reply(name), None


"""

with open('app.py', 'w', encoding='utf-8') as f:
    f.write(content[:start_idx] + new_function + content[end_idx:])
