import re

with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add ALL_BUTTONS and get_buttons
new_buttons_code = """
ALL_BUTTONS = [
    {"id": "COURSES", "title": "📚 Courses"},
    {"id": "DEMO", "title": "🎓 Free Demo"},
    {"id": "FEES", "title": "💰 Fees"},
    {"id": "OFFER", "title": "🔥 Offer"}
]

def get_buttons(exclude=None):
    buttons = []
    for btn in ALL_BUTTONS:
        if btn["id"] != exclude:
            buttons.append(btn)
    return buttons[:3]

def send_interactive_message(to_number, body_text, exclude_button=None):
    \"\"\"Sends WhatsApp interactive message with reply buttons\"\"\"
    buttons_data = get_buttons(exclude_button)
    buttons = [{"type": "reply", "reply": btn} for btn in buttons_data]
    
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": buttons
            }
        }
    }
    resp = requests.post(WHATSAPP_API_URL, headers=headers, json=payload)
    print(f"📤 Sent interactive to {to_number}: HTTP {resp.status_code}")
    
    if resp.status_code != 200:
        print("⚠️ Interactive message failed. Falling back to plain text.")
        fallback_text = add_menu_footer(body_text)
        return send_whatsapp_message(to_number, fallback_text)
        
    return resp

def send_whatsapp_message(to_number, message_text):"""

content = content.replace("def send_whatsapp_message(to_number, message_text):", new_buttons_code)

# 2. Update receive_message msg_type handling
old_msg_handling = """        if msg_type == "text":
            msg_text = message["text"]["body"].strip()
        elif msg_type == "button":
            msg_text = message["button"]["text"]
        else:
            msg_text = f"[{msg_type}]\""""

new_msg_handling = """        if msg_type == "text":
            msg_text = message["text"]["body"].strip()
        elif msg_type == "interactive":
            interactive_type = message["interactive"]["type"]
            if interactive_type == "button_reply":
                msg_text = message["interactive"]["button_reply"]["id"]
            elif interactive_type == "list_reply":
                msg_text = message["interactive"]["list_reply"]["id"]
            else:
                msg_text = f"[interactive_{interactive_type}]"
        elif msg_type == "button":
            msg_text = message["button"]["text"]
        else:
            msg_text = f"[{msg_type}]\""""
content = content.replace(old_msg_handling, new_msg_handling)

# 3. Update receive_message reply handling
old_reply = """        # 2. Generate and send smart reply
        reply = get_smart_reply(msg_text, contact_name, from_number, is_new_lead)
        send_whatsapp_message(from_number, reply)"""

new_reply = """        # 2. Generate and send smart reply
        reply, exclude_btn = get_smart_reply(msg_text, contact_name, from_number, is_new_lead)
        if exclude_btn == "NO_BUTTONS":
            send_whatsapp_message(from_number, reply)
        else:
            send_interactive_message(from_number, reply, exclude_btn)"""
content = content.replace(old_reply, new_reply)

# 4. Update get_smart_reply
old_get_smart = """def get_smart_reply(msg_text, name, phone, is_new_lead):
    reply = _get_smart_reply_internal(msg_text, name, phone, is_new_lead)
    is_exit_reply = (msg_text.lower().strip() == "exit")
    if reply and not is_exit_reply:
        return add_menu_footer(reply)
    return reply"""

new_get_smart = """def get_smart_reply(msg_text, name, phone, is_new_lead):
    reply, exclude_btn = _get_smart_reply_internal(msg_text, name, phone, is_new_lead)
    is_exit_reply = (msg_text.lower().strip() == "exit")
    if is_exit_reply:
        exclude_btn = "NO_BUTTONS"
    return reply, exclude_btn"""
content = content.replace(old_get_smart, new_get_smart)

# Now we need to patch _get_smart_reply_internal returns

content = content.replace('            return (\n                f"✅ {selected_time} confirmed!\\n\\n"\n                f"Preferred date ഏതാണ്?\\n"\n                f"(Example: Tomorrow, Monday, April 30)\\n\\n"\n                f"Date reply cheyyoo! 📅"\n            )', '            return (\n                f"✅ {selected_time} confirmed!\\n\\n"\n                f"Preferred date ഏതാണ്?\\n"\n                f"(Example: Tomorrow, Monday, April 30)\\n\\n"\n                f"Date reply cheyyoo! 📅"\n            ), "NO_BUTTONS"')

content = content.replace('            return "Please reply with 1, 2, or 3 to select a batch time."', '            return "Please reply with 1, 2, or 3 to select a batch time.", "NO_BUTTONS"')

content = content.replace('            return (\n                "✅ *CWPDE — Great Choice!*\\n\\n"\n                "📚 Certificate in Word Processing & Data Entry\\n"\n                "⏱ Duration: 6 Months\\n"\n                "🎓 Certificate: Rutronix Approved\\n"\n                "💰 Special Price: *₹4,800*\\n"\n                "📌 Exam Fee: separately as per Rutronix norms\\n\\n"\n                "👇 *Secure Payment Link:*\\n"\n                "https://rzp.io/rzp/xkWdKtd\\n\\n"\n                "Payment ചെയ്ത ശേഷം:\\n"\n                "📩 Transaction ID ഇവിടെ reply cheyyoo\\n"\n                "(Example: T2504281234)\\n\\n"\n                "Seat ഉടൻ confirm ആകും! 🎉\\n"\n                "📍 The Oxford Computers, Malayinkeezhu\\n"\n                "📞 9447329972"\n            )', '            return (\n                "✅ *CWPDE — Great Choice!*\\n\\n"\n                "📚 Certificate in Word Processing & Data Entry\\n"\n                "⏱ Duration: 6 Months\\n"\n                "🎓 Certificate: Rutronix Approved\\n"\n                "💰 Special Price: *₹4,800*\\n"\n                "📌 Exam Fee: separately as per Rutronix norms\\n\\n"\n                "👇 *Secure Payment Link:*\\n"\n                "https://rzp.io/rzp/xkWdKtd\\n\\n"\n                "Payment ചെയ്ത ശേഷം:\\n"\n                "📩 Transaction ID ഇവിടെ reply cheyyoo\\n"\n                "(Example: T2504281234)\\n\\n"\n                "Seat ഉടൻ confirm ആകും! 🎉\\n"\n                "📍 The Oxford Computers, Malayinkeezhu\\n"\n                "📞 9447329972"\n            ), "NO_BUTTONS"')

content = content.replace('            return (\n                "✅ *DCA — Great Choice!*\\n\\n"\n                "📚 Diploma in Computer Applications (Fast Track)\\n"\n                "⏱ Duration: 6 Months\\n"\n                "🎓 Certificate: Rutronix + State Approved\\n"\n                "💰 Special Price: *₹6,400*\\n"\n                "📌 Exam Fee: separately as per Rutronix norms\\n\\n"\n                "👇 *Secure Payment Link:*\\n"\n                "https://rzp.io/rzp/mJPPtM9x\\n\\n"\n                "Payment ചെയ്ത ശേഷം:\\n"\n                "📩 Transaction ID ഇവിടെ reply cheyyoo\\n"\n                "(Example: T2504281234)\\n\\n"\n                "Seat ഉടൻ confirm ആകും! 🎉\\n"\n                "📍 The Oxford Computers, Malayinkeezhu\\n"\n                "📞 9447329972"\n            )', '            return (\n                "✅ *DCA — Great Choice!*\\n\\n"\n                "📚 Diploma in Computer Applications (Fast Track)\\n"\n                "⏱ Duration: 6 Months\\n"\n                "🎓 Certificate: Rutronix + State Approved\\n"\n                "💰 Special Price: *₹6,400*\\n"\n                "📌 Exam Fee: separately as per Rutronix norms\\n\\n"\n                "👇 *Secure Payment Link:*\\n"\n                "https://rzp.io/rzp/mJPPtM9x\\n\\n"\n                "Payment ചെയ്ത ശേഷം:\\n"\n                "📩 Transaction ID ഇവിടെ reply cheyyoo\\n"\n                "(Example: T2504281234)\\n\\n"\n                "Seat ഉടൻ confirm ആകും! 🎉\\n"\n                "📍 The Oxford Computers, Malayinkeezhu\\n"\n                "📞 9447329972"\n            ), "NO_BUTTONS"')

content = content.replace('            return (\n                "✅ *AIDM — Great Choice!*\\n\\n"\n                "📚 AI-Driven Digital Marketing\\n"\n                "⏱ Duration: 6 Months\\n"\n                "🎓 Certificate: Industry Recognized\\n"\n                "💰 Special Price: *₹19,999*\\n"\n                "📌 Exam Fee: separately as per Rutronix norms\\n\\n"\n                "👇 *Secure Payment Link:*\\n"\n                "https://rzp.io/rzp/vF76sj7Y\\n\\n"\n                "Payment ചെയ്ത ശേഷം:\\n"\n                "📩 Transaction ID ഇവിടെ reply cheyyoo\\n"\n                "(Example: T2504281234)\\n\\n"\n                "Seat ഉടൻ confirm ആകും! 🎉\\n"\n                "📍 The Oxford Computers, Malayinkeezhu\\n"\n                "📞 9447329972"\n            )', '            return (\n                "✅ *AIDM — Great Choice!*\\n\\n"\n                "📚 AI-Driven Digital Marketing\\n"\n                "⏱ Duration: 6 Months\\n"\n                "🎓 Certificate: Industry Recognized\\n"\n                "💰 Special Price: *₹19,999*\\n"\n                "📌 Exam Fee: separately as per Rutronix norms\\n\\n"\n                "👇 *Secure Payment Link:*\\n"\n                "https://rzp.io/rzp/vF76sj7Y\\n\\n"\n                "Payment ചെയ്ത ശേഷം:\\n"\n                "📩 Transaction ID ഇവിടെ reply cheyyoo\\n"\n                "(Example: T2504281234)\\n\\n"\n                "Seat ഉടൻ confirm ആകും! 🎉\\n"\n                "📍 The Oxford Computers, Malayinkeezhu\\n"\n                "📞 9447329972"\n            ), "NO_BUTTONS"')

content = content.replace('            return (\n                "✅ *PGDCA — Great Choice!*\\n\\n"\n                "📚 Post Graduate Diploma in Computer Applications\\n"\n                "⏱ Duration: 12 Months\\n"\n                "🎓 Certificate: Rutronix + State Approved\\n"\n                "💰 Special Price: *₹15,999*\\n"\n                "📌 Exam Fee: separately as per Rutronix norms\\n\\n"\n                "👇 *Secure Payment Link:*\\n"\n                "https://rzp.io/rzp/KAQ2C7t\\n\\n"\n                "Payment ചെയ്ത ശേഷം:\\n"\n                "📩 Transaction ID ഇവിടെ reply cheyyoo\\n"\n                "(Example: T2504281234)\\n\\n"\n                "Seat ഉടൻ confirm ആകും! 🎉\\n"\n                "📍 The Oxford Computers, Malayinkeezhu\\n"\n                "📞 9447329972"\n            )', '            return (\n                "✅ *PGDCA — Great Choice!*\\n\\n"\n                "📚 Post Graduate Diploma in Computer Applications\\n"\n                "⏱ Duration: 12 Months\\n"\n                "🎓 Certificate: Rutronix + State Approved\\n"\n                "💰 Special Price: *₹15,999*\\n"\n                "📌 Exam Fee: separately as per Rutronix norms\\n\\n"\n                "👇 *Secure Payment Link:*\\n"\n                "https://rzp.io/rzp/KAQ2C7t\\n\\n"\n                "Payment ചെയ്ത ശേഷം:\\n"\n                "📩 Transaction ID ഇവിടെ reply cheyyoo\\n"\n                "(Example: T2504281234)\\n\\n"\n                "Seat ഉടൻ confirm ആകും! 🎉\\n"\n                "📍 The Oxford Computers, Malayinkeezhu\\n"\n                "📞 9447329972"\n            ), "NO_BUTTONS"')

content = content.replace('            return "Please reply with 1, 2, 3, or 4 to select an offer."', '            return "Please reply with 1, 2, 3, or 4 to select an offer.", "NO_BUTTONS"')

content = content.replace('        return (\n            f"🎉 *Demo Class Booked!*\\n\\n"\n            f"📚 Course: {course}\\n"\n            f"⏰ Time: {batch_time}\\n"\n            f"📅 Date: {user_date}\\n"\n            f"📍 The Oxford Computers, Malayinkeezhu\\n\\n"\n            f"നാളെ ഞങ്ങൾ WhatsApp-ൽ confirm ചെയ്യും!\\n"\n            f"കൂടുതൽ info: 📞 9447329972\\n"\n            f"🌐 theoxfordedu.com"\n        )', '        return (\n            f"🎉 *Demo Class Booked!*\\n\\n"\n            f"📚 Course: {course}\\n"\n            f"⏰ Time: {batch_time}\\n"\n            f"📅 Date: {user_date}\\n"\n            f"📍 The Oxford Computers, Malayinkeezhu\\n\\n"\n            f"നാളെ ഞങ്ങൾ WhatsApp-ൽ confirm ചെയ്യും!\\n"\n            f"കൂടുതൽ info: 📞 9447329972\\n"\n            f"🌐 theoxfordedu.com"\n        ), "NO_BUTTONS"')

content = content.replace('        return (\n            f"🎉 *Payment Received!*\\n\\n"\n            f"✅ Transaction ID: {user_txn}\\n"\n            f"📚 Course: {offer_course}\\n"\n            f"👤 Name: {name}\\n\\n"\n            f"Seat confirmed! Welcome to \\n"\n            f"*The Oxford Computers* 🎓\\n\\n"\n            f"📞 Next step: 9447329972 ലേക്ക് \\n"\n            f"വിളിക്കൂ — batch details കിട്ടും!\\n\\n"\n            f"📍 Malayinkeezhu, Thiruvananthapuram\\n"\n            f"🌐 theoxfordedu.com\\n\\n"\n            f"കാണാൻ കാത്തിരിക്കുന്നു! 😊"\n        )', '        return (\n            f"🎉 *Payment Received!*\\n\\n"\n            f"✅ Transaction ID: {user_txn}\\n"\n            f"📚 Course: {offer_course}\\n"\n            f"👤 Name: {name}\\n\\n"\n            f"Seat confirmed! Welcome to \\n"\n            f"*The Oxford Computers* 🎓\\n\\n"\n            f"📞 Next step: 9447329972 ലേക്ക് \\n"\n            f"വിളിക്കൂ — batch details കിട്ടും!\\n\\n"\n            f"📍 Malayinkeezhu, Thiruvananthapuram\\n"\n            f"🌐 theoxfordedu.com\\n\\n"\n            f"കാണാൻ കാത്തിരിക്കുന്നു! 😊"\n        ), "NO_BUTTONS"')

content = content.replace('        return (\n            "🎓 *Free Demo Class Booking*\\n\\n"\n            "Preferred batch time ഏത്?\\n\\n"\n            "1️⃣ Morning — 9 AM to 11 AM\\n"\n            "2️⃣ Afternoon — 12 PM to 2 PM  \\n"\n            "3️⃣ Evening — 5 PM to 7 PM\\n\\n"\n            "Number reply cheyyoo! 📅"\n        )', '        return (\n            "🎓 *Free Demo Class Booking*\\n\\n"\n            "Preferred batch time ഏത്?\\n\\n"\n            "1️⃣ Morning — 9 AM to 11 AM\\n"\n            "2️⃣ Afternoon — 12 PM to 2 PM  \\n"\n            "3️⃣ Evening — 5 PM to 7 PM\\n\\n"\n            "Number reply cheyyoo! 📅"\n        ), "DEMO"')

content = content.replace('        return (\n            "🔥 *Today\'s Special Offer!*\\n"\n            "━━━━━━━━━━━━━━━━\\n"\n            "🎓 Kerala State Rutronix Approved\\n\\n"\n            "1️⃣ CWPDE — Word Processing & Data Entry\\n"\n            "   💰 Special Price: *₹4,800*\\n"\n            "   ⏱ Duration: 6 Months\\n\\n"\n            "2️⃣ DCA — Diploma in Computer Applications\\n"\n            "   💰 Special Price: *₹6,400*\\n"\n            "   ⏱ Duration: 6 Months\\n\\n"\n            "3️⃣ AIDM — AI-Driven Digital Marketing\\n"\n            "   💰 Special Price: *₹19,999*\\n"\n            "   ⏱ Duration: 6 Months\\n\\n"\n            "4️⃣ PGDCA — Post Graduate Diploma\\n"\n            "   💰 Special Price: *₹15,999*\\n"\n            "   ⏱ Duration: 12 Months\\n"\n            "━━━━━━━━━━━━━━━━\\n"\n            "⚡ Limited Time Offer!\\n"\n            "📅 Seats limited — Book now!\\n\\n"\n            "Number reply cheyyoo — \\n"\n            "Payment link ഉടൻ അയക്കാം! 💳"\n        )', '        return (\n            "🔥 *Today\'s Special Offer!*\\n"\n            "━━━━━━━━━━━━━━━━\\n"\n            "🎓 Kerala State Rutronix Approved\\n\\n"\n            "1️⃣ CWPDE — Word Processing & Data Entry\\n"\n            "   💰 Special Price: *₹4,800*\\n"\n            "   ⏱ Duration: 6 Months\\n\\n"\n            "2️⃣ DCA — Diploma in Computer Applications\\n"\n            "   💰 Special Price: *₹6,400*\\n"\n            "   ⏱ Duration: 6 Months\\n\\n"\n            "3️⃣ AIDM — AI-Driven Digital Marketing\\n"\n            "   💰 Special Price: *₹19,999*\\n"\n            "   ⏱ Duration: 6 Months\\n\\n"\n            "4️⃣ PGDCA — Post Graduate Diploma\\n"\n            "   💰 Special Price: *₹15,999*\\n"\n            "   ⏱ Duration: 12 Months\\n"\n            "━━━━━━━━━━━━━━━━\\n"\n            "⚡ Limited Time Offer!\\n"\n            "📅 Seats limited — Book now!\\n\\n"\n            "Number reply cheyyoo — \\n"\n            "Payment link ഉടൻ അയക്കാം! 💳"\n        ), "OFFER"')

content = content.replace('            return (\n                f"✅ *{course_name}* — Great choice!\\n\\n"\n                f"{course_details}\\n"\n                f"📍 The Oxford Computers, Malayinkeezhu"\n            )', '            return (\n                f"✅ *{course_name}* — Great choice!\\n\\n"\n                f"{course_details}\\n"\n                f"📍 The Oxford Computers, Malayinkeezhu"\n            ), "COURSES"')

# For keyword replies loop
old_keyword_loop = """    # 8. Check keywords (fast, zero AI cost)
    for keyword, reply in KEYWORD_REPLIES.items():
        if keyword in msg_lower:
            if reply is None:  # Greeting keyword
                return get_welcome_message(name)
            return reply"""
new_keyword_loop = """    # 8. Check keywords (fast, zero AI cost)
    for keyword, reply in KEYWORD_REPLIES.items():
        if keyword in msg_lower:
            if reply is None:  # Greeting keyword
                return get_welcome_message(name), "COURSES"
            if keyword in ["courses", "course"]:
                exc = "COURSES"
            elif keyword in ["fee", "fees", "price"]:
                exc = "FEES"
            else:
                exc = None
            return reply, exc"""
content = content.replace(old_keyword_loop, new_keyword_loop)

old_end = """    # 6. New lead fallback — always send welcome first
    if is_new_lead:
        return get_welcome_message(name)

    # 7. Use Gemini AI for everything else
    if gemini_client:
        return get_gemini_reply(msg_text, name)

    # 8. Fallback if no AI configured
    return get_fallback_reply(name)"""
new_end = """    # 6. New lead fallback — always send welcome first
    if is_new_lead:
        return get_welcome_message(name), "COURSES"

    # 7. Use Gemini AI for everything else
    if gemini_client:
        return get_gemini_reply(msg_text, name), None

    # 8. Fallback if no AI configured
    return get_fallback_reply(name), None"""
content = content.replace(old_end, new_end)


with open('app.py', 'w', encoding='utf-8') as f:
    f.write(content)
