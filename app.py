# -*- coding: utf-8 -*-
"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The Oxford Computers - WhatsApp AI System v2.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Features:
  ✅ Google Sheets CRM - auto lead logging
  ✅ Gemini AI Chatbot - Malayalam/English smart replies
  ✅ Keyword-based fast replies (no AI cost for simple queries)
  ✅ Broadcast campaign API
  ✅ Multi-day follow-up scheduler
  ✅ Lead status tracking
  ✅ Admin stats endpoint
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIXED in v2.1:
  🔧 Migrated from deprecated google-generativeai → google-genai
  🔧 Updated Gemini API calls to new SDK style
  🔧 GOOGLE_CREDENTIALS env var name aligned
  🔧 Model upgraded to gemini-2.0-flash
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import json
import time
import threading
import requests
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ✅ NEW SDK - google-genai (replaces deprecated google-generativeai)
from google import genai

app = Flask(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURATION - All from Railway Environment Variables
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VERIFY_TOKEN      = os.environ.get("VERIFY_TOKEN", "oxford2026")
ACCESS_TOKEN      = os.environ.get("ACCESS_TOKEN", "")
PHONE_NUMBER_ID   = os.environ.get("PHONE_NUMBER_ID", "")
SHEETS_ID         = os.environ.get("SHEETS_ID", "")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
BROADCAST_API_KEY = os.environ.get("BROADCAST_API_KEY", "oxford_broadcast_2026")
ADMIN_KEY         = os.environ.get("ADMIN_KEY", "oxford_admin_2026")

# ✅ FIXED: Variable name matches Railway dashboard (GOOGLE_CREDENTIALS)
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS", "{}")

WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ✅ GEMINI AI SETUP - New google-genai SDK
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    print("✅ Gemini AI initialized (google-genai SDK)")
else:
    gemini_client = None
    print("⚠️ GEMINI_API_KEY not set - AI replies disabled")

# In-memory conversation state (resets on redeploy - use DB for production)
conversation_state = {}  # {phone: {"stage": "new/interested/enrolled", "name": "", "last_msg": timestamp}}
follow_up_queue    = []  # [{phone, name, send_at, message, done}]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# INSTITUTE PROFILE - Oxford Computers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INSTITUTE_INFO = """
You are Aaliza, a Senior Admission Counselor at The Oxford Computers, Malayinkeezhu, Thiruvananthapuram, Kerala.

YOUR GOAL:
- CONVERT the student into:
  1. Booking a free demo class
  2. Visiting the office
  3. Making a payment

YOUR STYLE:
- Speak like a friendly Malayali counselor (Manglish/Malayalam mix).
- Keep replies short (max 5 lines).
- Be natural, warm, friendly, confident, not robotic.
- Ask questions to guide the student.

RULES:
- NEVER dump the all course list unless asked.
- ALWAYS recommend 1-2 best courses based on student need.
- ALWAYS end with a next step:
  → "Demo book cheyyatte?"
  → "Office visit cheyyano?"
  → "Seat reserve cheyyano?"
- If student is confused: Ask about qualification + goal.
- If student asks fees: Show fee + explain ROI (job kittiyal 1-2 months-il recover cheyyam).
- If student delays: Create urgency (limited seats, batch starting soon).
- NEVER sound like AI.
- NEVER be too long.
- NEVER ignore conversion goal.
- NEVER overpromise job guarantee. Say "placement assistance", not "job guarantee".
- NEVER badmouth competitors.
- Do not repeat the same question twice. If already asked goal, move forward.

INSTITUTE DETAILS:
Name: The Oxford Computers
Location: Malayinkeezhu Junction, Thiruvananthapuram
Approval: Kerala State Rutronix Government Certified
Website: theoxfordedu.com
Phone: 9447329972
Speciality: AI-enabled, government-certified courses

COURSES:
1. PGDCA - 12 Months - ₹15,999
2. AIDM (AI-Driven Digital Marketing) - 6 Months - ₹19,999
3. SAP Financial Accounting - 4-6 Months - ₹11,999
4. Python Programming - 3 Months - ₹4,499
5. GST & Payroll Diploma - 6 Months - ₹5,499
6. DCA Fast Track - 6 Months - ₹6,400
7. Computer Teacher Training - 1 Year - ₹7,999
8. Corporate Business Accounting - 1 Year - ₹7,999
9. Word Processing & Data Entry - 6 Months - ₹4,800
10. Web Designing - 6 Months - ₹5,999

EXAMPLES OF YOUR REPLIES (FEW-SHOT TRAINING):

User: enik digital marketing padikkanam
Reply:
Super choice! 👍
Digital Marketing ippol Kerala + Gulf-il demand undu.
Ningalk best option: 👉 AIDM (AI-Driven Digital Marketing)
6 months course aanu, live campaigns padippikkum.
Demo class kaanan varamo? 🎓

User: python course evide aanu location
Reply:
Nammude office Malayinkeezhu Junction-il aanu (Thiruvananthapuram). 📍
Python 3 months course aanu, ₹4,499 aanu fee.
Nalla career scope ulla course aanu! 💻
Neritt office-ilekk varamo, atho demo book cheyyano?

User: njan degree kazhinju, etha nalla course?
Reply:
Degree kazhinja aalkk best IT career aanu! 🌟
Job oriented aayi PGDCA (12 months) allengil Web Designing (6 months) nokkam.
Randilum 100% placement assistance undu. 💪
Enthanu kooduthal thalparyam? Programming aano?
"""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# KEYWORD FAST REPLIES (No AI needed - instant response)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_COURSES_MSG = (
    "1️⃣ PGDCA (12 Months)\n"
    "2️⃣ AI-Driven Digital Marketing (6 Months)\n"
    "3️⃣ SAP Financial Accounting (4-6 Months)\n"
    "4️⃣ Python Programming (3 Months)\n"
    "5️⃣ GST & Payroll Diploma (6 Months)\n"
    "6️⃣ DCA Fast Track (6 Months)\n"
    "7️⃣ Computer Teacher Training (1 Year)\n"
    "8️⃣ Corporate Business Accounting (1 Year)\n"
    "9️⃣ Word Processing & Data Entry (3 Months)\n"
    "🔟 Professional Web Designing (6 Months)\n\n"
    "Oru course-nte details ariyaan number reply cheyyoo! 🎓"
)

_PGDCA_MSG = (
    "📚 *Post Graduate Diploma in Computer Applications (PGDCA)*\n"
    "⏱ Duration: 12 Months\n"
    "🎓 Certificate: Rutronix + State Approved\n"
    "📋 Key Modules: Programming Fundamentals (C,C++,Java), DBMS, Web Development, Software Engineering, Computer Networks, Mobile App Development, Final Project & Internship\n"
    "💡 Best for: Graduates seeking an IT career\n\n"
    "Demo class-naayi *DEMO* reply cheyyoo! 🙌"
)

_AIDM_MSG = (
    "📚 *AIDM - AI-Driven Digital Marketing*\n"
    "⏱ Duration: 6 Months\n"
    "🎓 Certificate: Industry Recognized\n"
    "📋 Key Modules: Digital Marketing Fundamentals, SEO & Content Strategy, Social Media Marketing, Google Ads & Meta Ads, AI Tools (ChatGPT/Canva AI), Email Marketing & CRM, Live Campaign Projects\n"
    "💡 Best for: Marketers, entrepreneurs, and beginners\n\n"
    "Demo class-naayi *DEMO* reply cheyyoo! 🙌"
)

_SAP_MSG = (
    "📚 *SAP Financial Accounting & Controlling*\n"
    "⏱ Duration: 4-6 Months\n"
    "🎓 Certificate: SAP Alliance Certificate\n"
    "📋 Key Modules: Introduction to ERP & SAP, General Ledger Accounting, Accounts Payable & Receivable, Asset & Bank Accounting, SAP Controlling (CO), Real-Time SAP Project\n"
    "💡 Best for: Commerce graduates and accounting professionals\n\n"
    "Demo class-naayi *DEMO* reply cheyyoo! 🙌"
)

_PYTHON_MSG = (
    "📚 *Python - Beginner to Advanced*\n"
    "⏱ Duration: 3 Months\n"
    "🎓 Certificate: Rutronix\n"
    "📋 Key Modules: Python Fundamentals, OOP in Python, File Handling & Modules, Web Scraping, Flask Basics, Data Handling with Pandas, Automation Projects\n"
    "💡 Best for: Beginners and IT aspirants\n\n"
    "Demo class-naayi *DEMO* reply cheyyoo! 🙌"
)

_GST_MSG = (
    "📚 *Diploma in GST Practitioner, Taxation & Payroll*\n"
    "⏱ Duration: 6 Months\n"
    "🎓 Certificate: Rutronix\n"
    "📋 Key Modules: GST Concepts & Filing, Income Tax Basics, Tally Prime, Payroll Processing, E-filing & Returns\n"
    "💡 Best for: Accounting professionals and commerce students\n\n"
    "Demo class-naayi *DEMO* reply cheyyoo! 🙌"
)

_DCA_MSG = (
    "📚 *Diploma in Computer Applications - Fast Track (DCA)*\n"
    "⏱ Duration: 6 Months\n"
    "🎓 Certificate: Rutronix + State\n"
    "📋 Key Modules: Computer Fundamentals, MS Office Suite, Programming Basics, Internet & Email, Database Fundamentals, Data Entry & DTP\n"
    "💡 Best for: Students and office job seekers\n\n"
    "Demo class-naayi *DEMO* reply cheyyoo! 🙌"
)

_TEACHER_MSG = (
    "📚 *Computer Teacher Training Course*\n"
    "⏱ Duration: 1 Year\n"
    "🎓 Certificate: Rutronix\n"
    "📋 Key Modules: Computer Fundamentals Teaching, MS Office Pedagogy, Programming Basics, DTP Tools, Practice Teaching\n"
    "💡 Best for: Aspiring computer teachers\n\n"
    "Demo class-naayi *DEMO* reply cheyyoo! 🙌"
)

_ACCOUNTING_MSG = (
    "📚 *Diploma in Corporate Business Accounting & Taxation*\n"
    "⏱ Duration: 1 Year\n"
    "🎓 Certificate: Rutronix\n"
    "📋 Key Modules: Corporate Accounting, GST Implementation, Income Tax Corporate, Financial Modelling, Real-World Case Studies\n"
    "💡 Best for: Advanced accounting and finance careers\n\n"
    "Demo class-naayi *DEMO* reply cheyyoo! 🙌"
)

_WORD_MSG = (
    "📚 *Certificate in Word Processing & Data Entry*\n"
    "⏱ Duration: 6 Months\n"
    "🎓 Certificate: Rutronix\n"
    "📋 Key Modules: Touch Typing, MS Word Processing, Data Entry Techniques, Basic DTP, Office Document Management\n"
    "💡 Best for: Data entry professionals and beginners\n\n"
    "Demo class-naayi *DEMO* reply cheyyoo! 🙌"
)

_WEB_MSG = (
    "📚 *Professional Diploma in Web Designing*\n"
    "⏱ Duration: 6 Months\n"
    "🎓 Certificate: Rutronix\n"
    "📋 Key Modules: HTML5 & CSS3 Advanced, JavaScript & jQuery, PHP & MySQL Basics, WordPress Development, Live Project Portfolio\n"
    "💡 Best for: Aspiring web developers and designers\n\n"
    "Demo class-naayi *DEMO* reply cheyyoo! 🙌"
)

KEYWORD_REPLIES = {
    # Courses List
    "courses": _COURSES_MSG,
    "course": _COURSES_MSG,

    # Individual Course Details
    "pgdca": _PGDCA_MSG,
    "pgd": _PGDCA_MSG,
    "post graduate": _PGDCA_MSG,
    "pg diploma": _PGDCA_MSG,

    "aidm": _AIDM_MSG,
    "digital marketing": _AIDM_MSG,
    "digital": _AIDM_MSG,
    "marketing": _AIDM_MSG,
    "seo": _AIDM_MSG,
    "social media": _AIDM_MSG,

    "sap": _SAP_MSG,
    "erp": _SAP_MSG,
    "finance": _SAP_MSG,

    "python": _PYTHON_MSG,
    "programming": _PYTHON_MSG,
    "coding": _PYTHON_MSG,

    "gst": _GST_MSG,
    "tally": _GST_MSG,
    "tax": _GST_MSG,
    "taxation": _GST_MSG,
    "payroll": _GST_MSG,
    "income tax": _GST_MSG,

    "dca": _DCA_MSG,
    "diploma": _DCA_MSG,
    "fast track": _DCA_MSG,

    "teacher": _TEACHER_MSG,
    "teaching": _TEACHER_MSG,
    "computer teacher": _TEACHER_MSG,

    "accounting": _ACCOUNTING_MSG,
    "corporate": _ACCOUNTING_MSG,
    "business accounting": _ACCOUNTING_MSG,

    "data entry": _WORD_MSG,
    "typing": _WORD_MSG,
    "word processing": _WORD_MSG,
    "ms word": _WORD_MSG,

    "web design": _WEB_MSG,
    "web": _WEB_MSG,
    "wordpress": _WEB_MSG,
    "html": _WEB_MSG,

    # Demo class keywords handled in get_smart_reply state machine

    # Fee / course details
    "fee": (
        "💰 *Course Fees Information*\n\n"
        "Oxford Computers-ൽ courses എല്ലാം *Kerala State Rutronix approved* ആണ്.\n\n"
        "📢 *Government prescribed fees മാത്രമാണ് ഈടാക്കുന്നത്.*\n\n"
        "🎁 Special offers & discounts ലഭിക്കാൻ:\n\n"
        "📞 *9447329972*\n\n"
        "Or visit:\n"
        "📍 Oxford Computers, Malayinkeezhu\n"
        "🌐 theoxfordedu.com\n\n"
        "🔥 Today's Special Offer കാണാൻ *OFFER* type cheyyoo!"
    ),
    "fees": (
        "💰 *Course Fees Information*\n\n"
        "Oxford Computers-ൽ courses എല്ലാം *Kerala State Rutronix approved* ആണ്.\n\n"
        "📢 *Government prescribed fees മാത്രമാണ് ഈടാക്കുന്നത്.*\n\n"
        "🎁 Special offers & discounts ലഭിക്കാൻ:\n\n"
        "📞 *9447329972*\n\n"
        "Or visit:\n"
        "📍 Oxford Computers, Malayinkeezhu\n"
        "🌐 theoxfordedu.com\n\n"
        "🔥 Today's Special Offer കാണാൻ *OFFER* type cheyyoo!"
    ),
    "price": (
        "💰 *Course Fees Information*\n\n"
        "Oxford Computers-ൽ courses എല്ലാം *Kerala State Rutronix approved* ആണ്.\n\n"
        "📢 *Government prescribed fees മാത്രമാണ് ഈടാക്കുന്നത്.*\n\n"
        "🎁 Special offers & discounts ലഭിക്കാൻ:\n\n"
        "📞 *9447329972*\n\n"
        "Or visit:\n"
        "📍 Oxford Computers, Malayinkeezhu\n"
        "🌐 theoxfordedu.com\n\n"
        "🔥 Today's Special Offer കാണാൻ *OFFER* type cheyyoo!"
    ),

    # Location
    "address": (
        "📍 *The Oxford Computers*\n"
        "Malayinkeezhu Junction\n"
        "Thiruvananthapuram, Kerala\n\n"
        "🌐 theoxfordedu.com\n\n"
        "Bus route: Malayinkeezhu bus stop-ൽ നിന്ന് 2 minutes walk"
    ),
    "location": (
        "📍 *The Oxford Computers*\n"
        "Malayinkeezhu Junction\n"
        "Thiruvananthapuram, Kerala\n\n"
        "🌐 theoxfordedu.com\n\n"
        "Bus route: Malayinkeezhu bus stop-ൽ നിന്ന് 2 minutes walk"
    ),
    "എവിടെ": (
        "📍 *The Oxford Computers*\n"
        "Malayinkeezhu Junction\n"
        "Thiruvananthapuram, Kerala\n\n"
        "🌐 theoxfordedu.com\n\n"
        "Malayinkeezhu bus stop-ൽ നിന്ന് 2 minutes walk 🚶"
    ),

    # Certificate
    "certificate": (
        "🏆 *Government Certified Certificate*\n\n"
        "The Oxford Computers-ൽ നിന്നുള്ള certificate:\n"
        "✅ Kerala State Rutronix approved\n"
        "✅ Government recognized\n"
        "✅ Job interviews-ൽ valid\n"
        "✅ Higher studies-നു് accepted\n\n"
        "ഇത് ഒരു real government-backed certification ആണ്! 💪"
    ),

    # Placement
    "job": (
        "💼 *Placement Support*\n\n"
        "The Oxford Computers:\n"
        "✅ 100% placement assistance\n"
        "✅ Resume preparation help\n"
        "✅ Interview coaching\n"
        "✅ Job referral network\n"
        "✅ Alumni community\n\n"
        "ഞങ്ങളുടെ students Kerala & Gulf-ൽ working ആണ്! 🌟"
    ),
    "placement": (
        "💼 *Placement Support*\n\n"
        "The Oxford Computers:\n"
        "✅ 100% placement assistance\n"
        "✅ Resume preparation help\n"
        "✅ Interview coaching\n"
        "✅ Job referral network\n"
        "✅ Alumni community\n\n"
        "ഞങ്ങളുടെ students Kerala & Gulf-ൽ working ആണ്! 🌟"
    ),

    # Timing
    "timing": (
        "⏰ *Batch Timings*\n\n"
        "🌅 Morning Batch: 9:00 AM – 11:00 AM\n"
        "☀️ Afternoon Batch: 12:00 PM – 2:00 PM\n"
        "🌆 Evening Batch: 5:00 PM – 7:00 PM\n\n"
        "Weekend batches also available! 📅\n\n"
        "Preferred timing പറഞ്ഞാൽ book ചെയ്യാം!"
    ),
    "batch": (
        "⏰ *Batch Timings*\n\n"
        "🌅 Morning Batch: 9:00 AM – 11:00 AM\n"
        "☀️ Afternoon Batch: 12:00 PM – 2:00 PM\n"
        "🌆 Evening Batch: 5:00 PM – 7:00 PM\n\n"
        "Weekend batches also available! 📅\n\n"
        "Preferred timing പറഞ്ഞാൽ book ചെയ്യാം!"
    ),

    "exit": (
        "👋 നന്ദി! The Oxford Computers-ൽ \n"
        "നിന്ന് വിളിക്കാം - 📞 9447329972\n\n"
        "കൂടുതൽ info: 🌐 theoxfordedu.com\n\n"
        "വീണ്ടും സംസാരിക്കാൻ \n"
        "ഇവിടെ message cheyyoo! 😊"
    ),

    # Greetings - None means use welcome message
    "hi": None,
    "hello": None,
    "hii": None,
    "നമസ്കാരം": None,
    "hai": None,
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WEBHOOK VERIFICATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ Webhook verified!")
        return challenge, 200
    return "Forbidden", 403


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RECEIVE & PROCESS MESSAGES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route("/webhook", methods=["POST"])
def receive_message():
    data = request.get_json()

    try:
        entry   = data["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]

        # Ignore delivery/read status updates
        if "statuses" in value:
            return jsonify({"status": "ok"}), 200

        messages = value.get("messages", [])
        contacts = value.get("contacts", [])

        if not messages:
            return jsonify({"status": "ok"}), 200

        message      = messages[0]
        from_number  = message["from"]
        msg_type     = message.get("type", "")
        contact_name = contacts[0]["profile"]["name"] if contacts else "Student"

        if msg_type == "text":
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
            msg_text = f"[{msg_type}]"

        print(f"📱 {contact_name} ({from_number}): {msg_text}")

        # Track whether this is a brand new lead
        is_new_lead = from_number not in conversation_state
        if is_new_lead:
            conversation_state[from_number] = {
                "name": contact_name,
                "stage": "new"
            }
        else:
            conversation_state[from_number]["name"] = contact_name
            
        conversation_state[from_number]["last_msg"] = datetime.now().isoformat()
        conversation_state[from_number]["last_text"] = msg_text

        # 1. Save to Google Sheets CRM (non-blocking)
        threading.Thread(
            target=save_lead_to_sheets,
            args=(from_number, contact_name, msg_text, is_new_lead)
        ).start()

        # 2. Generate and send smart reply
        reply, exclude_btn = get_smart_reply(msg_text, contact_name, from_number, is_new_lead)
        if exclude_btn == "NO_BUTTONS":
            send_whatsapp_message(from_number, reply)
        else:
            send_interactive_message(from_number, reply, btn_preset=exclude_btn)

        # 3. Schedule follow-ups for new leads only
        if is_new_lead:
            schedule_followups(from_number, contact_name)

    except Exception as e:
        print(f"❌ Webhook error: {e}")

    return jsonify({"status": "ok"}), 200


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SMART REPLY ENGINE
# Priority: New Lead Welcome → Keyword → Course Number → Gemini AI → Fallback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SMART REPLY ENGINE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Goal → Course mapping (relative numbering)
GOAL_COURSES = {
    "job": [
        ("PGDCA", "PGDCA - Post Graduate Diploma", "12 Months", "₹15,999"),
        ("Python Programming", "Python Programming", "3 Months", "₹4,499"),
        ("Professional Diploma in Web Designing", "Web Designing", "6 Months", "₹5,999"),
        ("DCA Fast Track", "DCA - Fast Track Diploma", "6 Months", "₹6,400"),
    ],
    "business": [
        ("AIDM Digital Marketing", "AI-Driven Digital Marketing", "6 Months", "₹19,999"),
        ("Professional Diploma in Web Designing", "Web Designing", "6 Months", "₹5,999"),
        ("Python Programming", "Python Programming", "3 Months", "₹4,499"),
    ],
    "basic": [
        ("DCA Fast Track", "DCA - Fast Track Diploma", "6 Months", "₹6,400"),
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
    "💰 *Course Fees - The Oxford Computers*\n"
    "━━━━━━━━━━━━━━━━\n"
    "1. PGDCA - ₹15,999 (12M)\n"
    "2. AIDM Digital Marketing - ₹19,999 (6M)\n"
    "3. SAP Accounting - ₹11,999 (4-6M)\n"
    "4. Python - ₹4,499 (3M)\n"
    "5. GST & Payroll - ₹5,499 (6M)\n"
    "6. DCA Fast Track - ₹6,400 (6M)\n"
    "7. Teacher Training - ₹7,999 (1Y)\n"
    "8. Business Accounting - ₹7,999 (1Y)\n"
    "9. Data Entry - ₹4,800 (6M)\n"
    "10. Web Designing - ₹5,999 (6M)\n"
    "━━━━━━━━━━━━━━━━\n"
    "🎓 All courses Kerala State Rutronix Approved\n"
    "📊 EMI facility available!\n\n"
    "Ithu one-time investment aanu.\n"
    "Job kittiyal 1-2 months-il recover cheyyam! 💪\n\n"
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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SMART REPLY ENGINE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Goal → Course mapping (relative numbering)
GOAL_COURSES = {
    "job": [
        ("PGDCA", "PGDCA - Post Graduate Diploma", "12 Months", "₹15,999"),
        ("Python Programming", "Python Programming", "3 Months", "₹4,499"),
        ("Professional Diploma in Web Designing", "Web Designing", "6 Months", "₹5,999"),
        ("DCA Fast Track", "DCA - Fast Track Diploma", "6 Months", "₹6,400"),
    ],
    "business": [
        ("AIDM Digital Marketing", "AI-Driven Digital Marketing", "6 Months", "₹19,999"),
        ("Professional Diploma in Web Designing", "Web Designing", "6 Months", "₹5,999"),
        ("Python Programming", "Python Programming", "3 Months", "₹4,499"),
    ],
    "basic": [
        ("DCA Fast Track", "DCA - Fast Track Diploma", "6 Months", "₹6,400"),
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
    "💰 *Course Fees - The Oxford Computers*\n"
    "━━━━━━━━━━━━━━━━\n"
    "1. PGDCA - ₹15,999 (12M)\n"
    "2. AIDM Digital Marketing - ₹19,999 (6M)\n"
    "3. SAP Accounting - ₹11,999 (4-6M)\n"
    "4. Python - ₹4,499 (3M)\n"
    "5. GST & Payroll - ₹5,499 (6M)\n"
    "6. DCA Fast Track - ₹6,400 (6M)\n"
    "7. Teacher Training - ₹7,999 (1Y)\n"
    "8. Business Accounting - ₹7,999 (1Y)\n"
    "9. Data Entry - ₹4,800 (6M)\n"
    "10. Web Designing - ₹5,999 (6M)\n"
    "━━━━━━━━━━━━━━━━\n"
    "🎓 All courses Kerala State Rutronix Approved\n"
    "📊 EMI facility available!\n\n"
    "Ithu one-time investment aanu.\n"
    "Job kittiyal 1-2 months-il recover cheyyam! 💪\n\n"
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
            "last_msg": datetime.now().isoformat(), "last_text": msg_text,
            "last_reply": ""
        }
        
    state = conversation_state[phone]
    current_stage = state.get("stage", "new")
    course = state.get("course", "Not Selected")
    last_reply = state.get("last_reply", "")

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
            f"👋 നന്ദി {name}!\n\n"
            "The Oxford Computers-ൽ നിന്ന് വിളിക്കാം\n"
            "📞 9447329972\n\n"
            "🌐 theoxfordedu.com\n"
            "വീണ്ടും message cheyyoo! 😊"
        ), "NO_BUTTONS"

    # RESTART COMMAND
    if msg_lower in ["restart", "start", "start over"]:
        state["stage"] = "goal_selection"
        state["last_reply"] = "welcome"
        return get_welcome_message(name), "BUTTONS_GOAL"

    # GREETING OR VERY SHORT NEW LEAD - only show welcome if not already in goal_selection
    greetings = ["hi", "hello", "hai", "hii", "നമസ്കാരം", "hey"]
    if msg_lower in greetings or (is_new_lead and len(msg_lower) <= 2 and not msg_lower.isdigit()):
        if current_stage == "goal_selection" and last_reply == "welcome":
            # Already showed welcome, nudge forward
            return (
                f"😊 {name}, 1 muthal 5 vare oru number reply cheyyoo!\n\n"
                "1️⃣ Job | 2️⃣ Business | 3️⃣ Basic | 4️⃣ Accounting | 5️⃣ Not sure"
            ), "BUTTONS_GOAL"
        state["stage"] = "goal_selection"
        state["last_reply"] = "welcome"
        return get_welcome_message(name), "BUTTONS_GOAL"

    # b) CURRENT STAGE HANDLERS (for specific stage inputs like numbers, dates)
    if current_stage == "demo_time_selection" and msg_lower in ["1", "2", "3"]:
        times = {"1": "Morning", "2": "Afternoon", "3": "Evening"}
        selected_time = times[msg_lower]
        state["batch_time"] = selected_time
        state["stage"] = "demo_date_selection"
        return (
            f"✅ {selected_time} confirmed!\n\n"
            "Preferred date ഏതാണ്?\n"
            "(Example: Tomorrow, Monday, April 30)\n\n"
            "Date reply cheyyoo! 📅"
        ), "NO_BUTTONS"

    if current_stage == "demo_date_selection" and not is_offer_intent and not is_help_choose:
        user_date = msg_text.strip()
        state["stage"] = "demo_booked"
        batch_time = state.get("batch_time", "")
        status_msg = f"Demo Booked: {course} {batch_time} {user_date}"
        threading.Thread(target=update_lead_status, args=(phone, status_msg)).start()
        return (
            "🎉 *Demo Class Booked!*\n\n"
            f"📚 Course: {course}\n"
            f"⏰ Time: {batch_time}\n"
            f"📅 Date: {user_date}\n"
            "📍 The Oxford Computers, Malayinkeezhu\n\n"
            "നാളെ ഞങ്ങൾ WhatsApp-ൽ confirm ചെയ്യും!\n"
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
            f"✅ *{code} - Great Choice!*\n\n"
            f"📚 {full_name}\n"
            f"⏱ Duration: {dur}\n"
            f"🎓 Kerala State Rutronix Approved\n"
            f"💰 Special Price: *{price}*\n\n"
            "✅ Government certified\n"
            "✅ Receipt after payment\n"
            f"📍 Oxford Computers, Malayinkeezhu\n\n"
            f"👇 *Secure Payment Link:*\n{link}\n\n"
            "Payment ശേഷം Transaction ID\n"
            "ഇവിടെ reply cheyyoo 📩\n"
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
            "🎉 *Payment Received!*\n\n"
            f"✅ Transaction ID: {user_txn}\n"
            f"📚 Course: {offer_course}\n"
            f"👤 Name: {name}\n\n"
            "Seat confirmed! Welcome to\n"
            "*The Oxford Computers* 🎓\n\n"
            "📞 9447329972 - batch details\n"
            "📍 Malayinkeezhu, Thiruvananthapuram\n\n"
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
                f"✅ *{display}* - Great choice!\n\n"
                f"{detail}\n"
                f"💰 Fee: {fee} | ⏱ {dur}\n"
                f"🎓 Kerala State Rutronix Approved\n\n"
                f"Free demo class book cheyyatte? 🎓"
            ), "BUTTONS_COURSE"

    # c) INTENT PHRASES (offer/fees/demo/visit/call/help_choose/courses)
    # "courses" or "restart" keyword - reset to goal_selection with anti-loop guard
    if msg_lower in ["course", "courses", "padikkanam", "study"]:
        if current_stage == "goal_selection" and last_reply == "welcome":
            return (
                f"Already courses list kaanichu 😊\n\n"
                "Ningalk enthu type course aanu vendath?\n"
                "1️⃣ Job | 2️⃣ Business | 3️⃣ Basic | 4️⃣ Accounting | 5️⃣ Not sure"
            ), "BUTTONS_GOAL"
        state["stage"] = "goal_selection"
        state["last_reply"] = "welcome"
        return get_welcome_message(name), "BUTTONS_GOAL"

    if is_offer_intent:
        state["stage"] = "offer_selection"
        # do NOT reset last_reply here
        return (
            "🔥 *Today's Special Offer!*\n"
            "━━━━━━━━━━━━━━━━\n"
            "🎓 Kerala State Rutronix Approved\n\n"
            "1️⃣ CWPDE - ₹4,800 (6M)\n"
            "2️⃣ DCA - ₹6,400 (6M)\n"
            "3️⃣ AIDM - ₹19,999 (6M)\n"
            "4️⃣ PGDCA - ₹15,999 (12M)\n"
            "━━━━━━━━━━━━━━━━\n"
            "⚡ Limited seats! Book now!\n\n"
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
                f"💰 *{course} - Fee Details*\n\n"
                f"📋 Fee: {fee}\n"
                f"⏱ Duration: {duration}\n"
                f"🎓 Kerala State Rutronix Approved\n\n"
                "📊 EMI available - monthly installments!\n\n"
                "Ithu one-time investment aanu.\n"
                "Job kittiyal 1-2 months-il recover cheyyam! 💪\n\n"
                "Demo kaanan varamo, atho seat reserve cheyyano?"
            ), "BUTTONS_FEES"
        return FULL_FEE_TABLE, "BUTTONS_FEES"

    if msg_lower in ["demo", "free class", "free demo"]:
        state["stage"] = "demo_time_selection"  # go to demo flow, not goal_selection
        return (
            "🎓 *Free Demo Class Booking*\n\n"
            "Preferred batch time ഏത്?\n\n"
            "1️⃣ Morning - 9 AM to 11 AM\n"
            "2️⃣ Afternoon - 12 PM to 2 PM\n"
            "3️⃣ Evening - 5 PM to 7 PM\n\n"
            "Number reply cheyyoo! 📅"
        ), "NO_BUTTONS"

    if any(kw in msg_lower for kw in VISIT_KEYWORDS):
        state["stage"] = "visit_interested"  # set to visit, NOT goal_selection
        threading.Thread(target=update_lead_status, args=(phone, "Office Visit Interested")).start()
        return (
            f"🏢 *Office Visit - Welcome {name}!*\n\n"
            "📍 *The Oxford Computers*\n"
            "   Malayinkeezhu Junction\n"
            "   Thiruvananthapuram, Kerala\n\n"
            "⏰ Office Hours: 9 AM – 7 PM (Mon-Sat)\n"
            "📞 9447329972\n\n"
            "Eppol varananu convenient?\n"
            "Morning / Afternoon / Evening? 😊"
        ), "BUTTONS_COURSE"

    if any(kw in msg_lower for kw in HANDOFF_KEYWORDS):
        state["stage"] = "call_requested"  # set to call, NOT goal_selection
        threading.Thread(target=update_lead_status, args=(phone, "Call Requested")).start()
        return (
            f"😊 Of course {name}!\n\n"
            "Oru experienced counselor connect cheyyam.\n"
            "📞 *9447329972* - vilikku!\n\n"
            "⏰ Available: 9 AM – 7 PM (Mon-Sat)\n"
            "📍 Oxford Computers, Malayinkeezhu\n\n"
            "Allenkil ividé message cheyyoo,\n"
            "njan help cheyyam! 🙌"
        ), "NO_BUTTONS"

    if is_help_choose or is_not_sure_goal:
        # Only change stage if not already in not_sure (avoid resetting)
        if current_stage != "not_sure":
            state["stage"] = "not_sure"
        if gemini_client:
            try:
                return get_gemini_reply(
                    f"Student {name} is unsure which course to pick. Ask about their qualification, interest, and career goal. Then suggest the best course.", name
                ), "BUTTONS_GOAL"
            except Exception:
                pass
        return (
            f"{name}, no problem! 😊\n\n"
            "Ningalude qualification enthanu?\n"
            "Eppol enthu cheyyunnu?\n"
            "Ethu type job aanu interest?\n\n"
            "Reply cheyyoo - best course recommend cheyyam! 🎓"
        ), "BUTTONS_GOAL"

    # d) GOAL WORDS
    if is_job_goal:
        goal = "job"
        courses = GOAL_COURSES[goal]
        state["stage"] = "course_recommendation"
        state["goal"] = goal
        lines = ["📚 *നിങ്ങൾക്ക് best ആയ courses:*\n"]
        for i, (_, display, dur, fee) in enumerate(courses, 1):
            lines.append(f"{i}️⃣ {display} - {dur} - {fee}")
        lines.append("\nCourse number reply cheyyoo! 🎓")
        return "\n".join(lines), "NO_BUTTONS"

    if is_business_goal:
        goal = "business"
        courses = GOAL_COURSES[goal]
        state["stage"] = "course_recommendation"
        state["goal"] = goal
        lines = ["📚 *നിങ്ങൾക്ക് best ആയ courses:*\n"]
        for i, (_, display, dur, fee) in enumerate(courses, 1):
            lines.append(f"{i}️⃣ {display} - {dur} - {fee}")
        lines.append("\nCourse number reply cheyyoo! 🎓")
        return "\n".join(lines), "NO_BUTTONS"

    if is_basic_goal:
        goal = "basic"
        courses = GOAL_COURSES[goal]
        state["stage"] = "course_recommendation"
        state["goal"] = goal
        lines = ["📚 *നിങ്ങൾക്ക് best ആയ courses:*\n"]
        for i, (_, display, dur, fee) in enumerate(courses, 1):
            lines.append(f"{i}️⃣ {display} - {dur} - {fee}")
        lines.append("\nCourse number reply cheyyoo! 🎓")
        return "\n".join(lines), "NO_BUTTONS"

    if is_accounting_goal:
        goal = "accounting"
        courses = GOAL_COURSES[goal]
        state["stage"] = "course_recommendation"
        state["goal"] = goal
        lines = ["📚 *നിങ്ങൾക്ക് best ആയ courses:*\n"]
        for i, (_, display, dur, fee) in enumerate(courses, 1):
            lines.append(f"{i}️⃣ {display} - {dur} - {fee}")
        lines.append("\nCourse number reply cheyyoo! 🎓")
        return "\n".join(lines), "NO_BUTTONS"

    # e) COURSE KEYWORDS
    if msg_lower in ["timing", "batch"]:
        return (
            "⏰ *Batch Timings*\n\n"
            "🌅 Morning: 9 AM – 11 AM\n"
            "☀️ Afternoon: 12 PM – 2 PM\n"
            "🌆 Evening: 5 PM – 7 PM\n\n"
            "Weekend batches available! 📅\n"
            "Preferred timing reply cheyyoo!"
        ), "BUTTONS_COURSE"

    if msg_lower == "certificate":
        return (
            "🏆 *Government Certified Certificate*\n\n"
            "✅ Kerala State Rutronix approved\n"
            "✅ Job interviews-ൽ valid\n\n"
            "Real government-backed certification! 💪"
        ), "BUTTONS_COURSE"

    if msg_lower in ["placement", "job assistance", "placement support"]:
        return (
            "💼 *Placement Support*\n\n"
            "✅ 100% placement assistance\n"
            "✅ Resume preparation & Interview coaching\n\n"
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
        return course_kw_map[msg_lower] + f"\n\n💰 Fee: {fee} | ⏱ {dur}\n🎓 Kerala State Rutronix Approved\n\nFree demo class book cheyyatte? 🎓", "BUTTONS_COURSE"

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
            return f"✅ *{cname}* - Great choice!\n\n{cdetail}\n\n💰 Fee: {fee} | ⏱ {dur}\n🎓 Kerala State Rutronix Approved\n\nFree demo class book cheyyatte? 🎓", "BUTTONS_COURSE"

    # f) GEMINI FALLBACK
    if gemini_client:
        try:
            return get_gemini_reply(msg_text, name), "BUTTONS_COURSE"
        except Exception:
            return get_smart_fallback(name, msg_text), "BUTTONS_COURSE"
    return get_smart_fallback(name, msg_text), "BUTTONS_COURSE"

def get_welcome_message(name):
    return (
        f"👋 നമസ്കാരം *{name}*!\n\n"
        "*The Oxford Computers*-ലേക്ക് സ്വാഗതം! 🎓\n"
        "Kerala Govt Certified • AI-Enabled Courses\n\n"
        "നിങ്ങൾ എന്താണ് ലക്ഷ്യം? 🤔\n\n"
        "1️⃣ Job Oriented - IT/Software career\n"
        "2️⃣ Business/Freelance\n"
        "3️⃣ Basic Computer/Office Job\n"
        "4️⃣ Accounting/Tax\n"
        "5️⃣ Not sure - help me choose\n\n"
        "Number reply cheyyoo! 📝"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ✅ GEMINI AI REPLY - With 429 error handling
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_gemini_reply(msg_text, name, return_fallback=True):
    try:
        prompt = f"""{INSTITUTE_INFO}

Student name: {name}
Student message: "{msg_text}"
"""
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        return response.text.strip()

    except Exception as e:
        error_str = str(e).lower()
        if "429" in str(e) or "quota" in error_str or "resource" in error_str:
            print(f"⚠️ Gemini quota exceeded - using smart fallback")
        else:
            print(f"⚠️ Gemini error: {e}")
        
        if return_fallback:
            return get_smart_fallback(name, msg_text)
        return None

def get_smart_fallback(name, msg_text=""):
    msg_lower = msg_text.lower() if msg_text else ""

    if any(w in msg_lower for w in ["fee", "price", "cost", "vila"]):
        return (
            f"😊 {name}, fees ariyaan aagrahikkunnathin nandi!\n\n"
            "Government approved courses ₹4,499 muthal.\n"
            "EMI facility undh!\n\n"
            "Exact fee ariyaan *FEES* reply cheyyoo 💰\n"
            "📞 9447329972"
        )
    if any(w in msg_lower for w in ["job", "placement", "work", "career"]):
        return (
            f"{name}, nalla chodyam! 💪\n\n"
            "Oxford-il 100% placement assistance undh.\n"
            "Kerala & Gulf-il students working aanu.\n\n"
            "Best course ariyaan *COURSES* reply cheyyoo 📚\n"
            "Or free demo try cheyyoo: *DEMO* 🎓"
        )
    if any(w in msg_lower for w in ["course", "padikkaan", "learn", "study"]):
        return (
            f"{name}, 10 government certified courses und! 📚\n\n"
            "Ningalude goal enthanu?\n"
            "Job? Business? Basic computer?\n\n"
            "*COURSES* reply cheythal help cheyyam! 🎓"
        )

    return (
        f"😊 Nandi {name}!\n\n"
        "Njan Aaliza - The Oxford Computers-nte\n"
        "Senior Admission Counselor.\n\n"
        "Ningalkku help cheyyatte?\n"
        "📚 *COURSES* | 🎓 *DEMO* | 💰 *FEES*\n"
        "📞 9447329972"
    )

def get_fallback_reply(name):
    return get_smart_fallback(name)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GOOGLE SHEETS CRM
# Sheet columns: Timestamp | Name | Phone | Last Message | Status | Source | Notes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    # ✅ FIXED: reads GOOGLE_CREDENTIALS env var (matches Railway dashboard)
    creds_json = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEETS_ID)


def save_lead_to_sheets(phone, name, message, is_new_lead):
    """Save or update lead in Google Sheets"""
    try:
        if not SHEETS_ID or not GOOGLE_CREDENTIALS_JSON or GOOGLE_CREDENTIALS_JSON == "{}":
            print("⚠️ Sheets skipped - SHEETS_ID or GOOGLE_CREDENTIALS not configured")
            return

        wb = get_sheet()

        # Use 'Leads' sheet if it exists, otherwise use first sheet
        sheet_titles = [s.title for s in wb.worksheets()]
        leads_sheet = wb.worksheet("Leads") if "Leads" in sheet_titles else wb.sheet1

        # Setup header row if sheet is empty
        first_cell = leads_sheet.cell(1, 1).value
        if not first_cell:
            leads_sheet.update("A1:G1", [
                ["Timestamp", "Name", "Phone", "Last Message", "Status", "Source", "Notes"]
            ])

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        note_timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M]")
        note_entry = f"{note_timestamp} {message}"

        if is_new_lead:
            row = [timestamp, name, phone, message, "New Lead", "WhatsApp", note_entry]
            leads_sheet.append_row(row)
            print(f"✅ New lead saved: {name} ({phone})")
        else:
            all_phones = leads_sheet.col_values(3)
            if phone in all_phones:
                row_idx = all_phones.index(phone) + 1
                leads_sheet.update_cell(row_idx, 1, timestamp)
                leads_sheet.update_cell(row_idx, 4, message)
                
                existing_note = leads_sheet.cell(row_idx, 7).value or ""
                new_note = f"{existing_note}\n{note_entry}" if existing_note else note_entry
                leads_sheet.update_cell(row_idx, 7, new_note)
                
                print(f"✅ Lead updated: {name} ({phone})")

    except Exception as e:
        print(f"⚠️ Sheets error: {e}")


def update_lead_status(phone, status, append_note=None):
    """Update lead status column in Google Sheets"""
    try:
        if not SHEETS_ID or not GOOGLE_CREDENTIALS_JSON or GOOGLE_CREDENTIALS_JSON == "{}":
            return
        wb = get_sheet()
        sheet_titles = [s.title for s in wb.worksheets()]
        leads_sheet = wb.worksheet("Leads") if "Leads" in sheet_titles else wb.sheet1
        all_phones = leads_sheet.col_values(3)
        if phone in all_phones:
            row_idx = all_phones.index(phone) + 1
            leads_sheet.update_cell(row_idx, 5, status)
            if append_note:
                existing_note = leads_sheet.cell(row_idx, 7).value or ""
                new_note = f"{existing_note}\n{append_note}" if existing_note else append_note
                leads_sheet.update_cell(row_idx, 7, new_note)
            print(f"✅ Status updated: {phone} → {status}")
    except Exception as e:
        print(f"⚠️ Status update error: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MULTI-DAY FOLLOW-UP SCHEDULER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FOLLOWUP_MESSAGES = [
    {
        "day": 1,
        "hours": 24,
        "message": (
            "{name}-നു് നമസ്കാരം! 👋\n\n"
            "The Oxford Computers-ൽ നിന്ന്...\n\n"
            "നിങ്കൾ course-നെ കുറിച്ച് ആലോചിച്ചോ? 🤔\n\n"
            "ഒരു *free demo class* try ചെയ്ത് നോക്കൂ -\n"
            "zero commitment, 100% free! 🎓\n\n"
            "*DEMO* reply ചെയ്താൽ book ചെയ്യാം!"
        )
    },
    {
        "day": 3,
        "hours": 72,
        "message": (
            "👋 {name}, The Oxford Computers here!\n\n"
            "🌟 *Student Success Story*\n\n"
            "Riya (Attingal) - Web Design course complete ചെയ്ത്\n"
            "ഇപ്പോൾ ₹25,000/month earn ചെയ്യുന്നു! 💪\n\n"
            "താങ്കൾക്കും ഇത് possible ആണ്.\n"
            "Government certified course + Placement support.\n\n"
            "📅 Next batch starting soon!\n"
            "Seat reserve ചെയ്യാൻ reply ചെയ്യൂ 🙌"
        )
    },
    {
        "day": 7,
        "hours": 168,
        "message": (
            "{name}, last message! 😊\n\n"
            "The Oxford Computers - *Special Offer*\n\n"
            "🎁 ഈ batch-ൽ join ചെയ്യുന്നവർക്ക്:\n"
            "✅ Free registration (₹500 waived)\n"
            "✅ Free study materials\n"
            "✅ Flexible EMI option\n\n"
            "📍 Malayinkeezhu, Trivandrum\n"
            "🌐 theoxfordedu.com\n\n"
            "കൂടുതൽ info: *FEES* അല്ലെങ്കിൽ *DEMO* reply ചെയ്യൂ!"
        )
    }
]


def schedule_followups(phone, name):
    """Schedule multi-day follow-up messages for a new lead"""
    now = datetime.now()
    for followup in FOLLOWUP_MESSAGES:
        send_at = now + timedelta(hours=followup["hours"])
        follow_up_queue.append({
            "phone": phone,
            "name": name,
            "send_at": send_at,
            "message": followup["message"].format(name=name),
            "day": followup["day"],
            "done": False
        })
    print(f"📅 Follow-ups scheduled for {name} ({phone})")


def process_followup_queue():
    """Background thread - checks every 5 min and sends due follow-ups"""
    while True:
        try:
            now = datetime.now()
            for item in follow_up_queue:
                if not item["done"] and now >= item["send_at"]:
                    # Skip if lead replied within last 6 hours (they're active)
                    state = conversation_state.get(item["phone"], {})
                    last_msg_time = state.get("last_msg", "")
                    if last_msg_time:
                        last_dt = datetime.fromisoformat(last_msg_time)
                        if (now - last_dt).total_seconds() < 21600:  # 6 hours
                            item["done"] = True
                            print(f"⏭️ Follow-up skipped - {item['name']} recently active")
                            continue

                    send_whatsapp_message(item["phone"], item["message"])
                    threading.Thread(
                        target=update_lead_status,
                        args=(item["phone"], f"Follow-up Day {item['day']} Sent")
                    ).start()
                    item["done"] = True
                    print(f"📤 Follow-up Day {item['day']} sent to {item['name']}")

        except Exception as e:
            print(f"⚠️ Follow-up queue error: {e}")

        time.sleep(300)  # Check every 5 minutes


# Start background follow-up processor
followup_thread = threading.Thread(target=process_followup_queue, daemon=True)
followup_thread.start()
print("✅ Follow-up scheduler started")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SEND WHATSAPP MESSAGE - Named Button Presets
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

BUTTON_PRESETS = {
    "BUTTONS_GOAL": [
        {"id": "1", "title": "💼 Job Oriented"},
        {"id": "2", "title": "🚀 Business"},
        {"id": "3", "title": "🖥️ Basic Computer"},
    ],
    "BUTTONS_COURSE": [
        {"id": "DEMO", "title": "🎓 Free Demo"},
        {"id": "FEES", "title": "💰 Fees"},
        {"id": "VISIT", "title": "🏢 Visit Office"},
    ],
    "BUTTONS_FEES": [
        {"id": "DEMO", "title": "🎓 Free Demo"},
        {"id": "OFFER", "title": "🔥 Pay Now"},
        {"id": "CALL", "title": "📞 Call Us"},
    ],
    "BUTTONS_OFFER": [
        {"id": "OFFER", "title": "💳 Pay Now"},
        {"id": "DEMO", "title": "🎓 Free Demo"},
        {"id": "VISIT", "title": "🏢 Visit Office"},
    ],
    "BUTTONS_AFTER_DEMO": [
        {"id": "COURSES", "title": "📚 Courses"},
        {"id": "OFFER", "title": "🔥 Offer"},
        {"id": "VISIT", "title": "🏢 Visit Office"},
    ],
}

def send_interactive_message(to_number, body_text, btn_preset=None):
    """Sends WhatsApp interactive message with named button presets"""
    if not btn_preset or btn_preset == "NO_BUTTONS":
        return send_whatsapp_message(to_number, body_text)

    buttons_data = BUTTON_PRESETS.get(btn_preset, BUTTON_PRESETS["BUTTONS_COURSE"])
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
            "action": {"buttons": buttons}
        }
    }
    resp = requests.post(WHATSAPP_API_URL, headers=headers, json=payload)
    print(f"📤 Sent interactive [{btn_preset}] to {to_number}: HTTP {resp.status_code}")

    if resp.status_code != 200:
        print("⚠️ Interactive failed, falling back to plain text")
        return send_whatsapp_message(to_number, body_text)

    return resp

def send_whatsapp_message(to_number, message_text):
    """Send a plain text WhatsApp message"""
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": message_text}
    }
    resp = requests.post(WHATSAPP_API_URL, headers=headers, json=payload)
    print(f"📤 Sent to {to_number}: HTTP {resp.status_code}")
    return resp


def send_template_message(to_number, template_name, lang="en", components=None):
    """Send a Meta-approved template message"""
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "template",
        "template": {"name": template_name, "language": {"code": lang}}
    }
    if components:
        payload["template"]["components"] = components
    return requests.post(WHATSAPP_API_URL, headers=headers, json=payload)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BROADCAST API
# POST /broadcast  |  Header: X-API-Key
# Body: { "numbers": [...], "message": "...", "delay_seconds": 2 }
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route("/broadcast", methods=["POST"])
def broadcast():
    if request.headers.get("X-API-Key") != BROADCAST_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    data    = request.get_json()
    numbers = data.get("numbers", [])
    message = data.get("message", "")
    delay   = data.get("delay_seconds", 2)

    if not numbers or not message:
        return jsonify({"error": "numbers and message are required"}), 400

    results = []
    for number in numbers:
        # Normalize to Indian format
        number = str(number).strip()
        if not number.startswith("91"):
            number = "91" + number.lstrip("0")

        resp = send_whatsapp_message(number, message)
        results.append({
            "number": number,
            "status": resp.status_code,
            "success": resp.status_code == 200
        })
        time.sleep(delay)  # Rate limiting

    success_count = sum(1 for r in results if r["success"])
    return jsonify({
        "total": len(numbers),
        "success": success_count,
        "failed": len(numbers) - success_count,
        "results": results
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TEMPLATE BROADCAST API
# POST /broadcast-template  |  Header: X-API-Key
# Body: { "numbers": [...], "template_name": "...", "language": "en", "variables": ["{name}"] }
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route("/broadcast-template", methods=["POST"])
def broadcast_template():
    if request.headers.get("X-API-Key") != BROADCAST_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    numbers = data.get("numbers", [])
    template_name = data.get("template_name", "")
    language = data.get("language", "en")
    variables = data.get("variables", [])
    delay = data.get("delay_seconds", 2)

    if not numbers or not template_name:
        return jsonify({"error": "numbers and template_name are required"}), 400

    results = []
    for item in numbers:
        if isinstance(item, dict):
            number = str(item.get("phone", "")).strip()
            name = str(item.get("name", ""))
        else:
            number = str(item).strip()
            name = ""

        if not number.startswith("91"):
            number = "91" + number.lstrip("0")

        # Resolve variables
        resolved_vars = []
        for var in variables:
            if var == "{name}":
                resolved_vars.append(name)
            else:
                resolved_vars.append(str(var))

        components = []
        if resolved_vars:
            parameters = [{"type": "text", "text": v} for v in resolved_vars]
            components.append({
                "type": "body",
                "parameters": parameters
            })

        resp = send_template_message(number, template_name, lang=language, components=components)
        results.append({
            "number": number,
            "status": resp.status_code,
            "success": resp.status_code == 200
        })
        time.sleep(delay)

    success_count = sum(1 for r in results if r["success"])
    return jsonify({
        "total": len(numbers),
        "success": success_count,
        "failed": len(numbers) - success_count,
        "results": results
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ADMIN STATS API
# GET /stats  |  Header: X-Admin-Key
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route("/stats", methods=["GET"])
def stats():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    total_leads       = len(conversation_state)
    demo_requests     = sum(1 for s in conversation_state.values() if s.get("stage") == "demo_requested")
    pending_followups = sum(1 for f in follow_up_queue if not f["done"])

    return jsonify({
        "total_leads": total_leads,
        "demo_requests": demo_requests,
        "pending_followups": pending_followups,
        "active_conversations": [
            {
                "name": v["name"],
                "last_message": v.get("last_text", ""),
                "stage": v.get("stage", "new"),
                "last_active": v.get("last_msg", "")
            }
            for v in conversation_state.values()
        ]
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MANUAL FOLLOW-UP TRIGGER (testing / admin use)
# POST /trigger-followup  |  Header: X-Admin-Key
# Body: { "phone": "919...", "name": "...", "message": "..." }
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route("/trigger-followup", methods=["POST"])
def trigger_followup():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    data    = request.get_json()
    phone   = data.get("phone", "")
    name    = data.get("name", "Student")
    message = data.get("message", "")

    if not phone or not message:
        return jsonify({"error": "phone and message are required"}), 400

    if not phone.startswith("91"):
        phone = "91" + phone.lstrip("0")

    resp = send_whatsapp_message(phone, message)
    return jsonify({
        "success": resp.status_code == 200,
        "status": resp.status_code,
        "phone": phone
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BROADCAST ADMIN PANEL
# GET /panel?key=<ADMIN_KEY>
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route("/panel", methods=["GET"])
def admin_panel():
    admin_key = request.args.get("key", "")
    if admin_key != ADMIN_KEY:
        return """
        <html>
        <body style='font-family:sans-serif;text-align:center;padding:50px;
                     background:#0a0f0d;color:#25D366'>
          <h2>🔒 Access Denied</h2>
          <p style='color:#888'>URL-il ?key=YOUR_ADMIN_KEY add cheyyuka</p>
        </body>
        </html>
        """, 403

    try:
        with open("panel.html", "r", encoding="utf-8") as f:
            return f.read(), 200, {"Content-Type": "text/html"}
    except FileNotFoundError:
        return "panel.html not found in project root", 404


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HEALTH CHECK
# GET /
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "running ✅",
        "app": "Oxford Computers WhatsApp AI System v2.1",
        "sdk": "google-genai (new)",
        "features": [
            "Google Sheets CRM",
            "Gemini AI Chatbot (gemini-2.0-flash)",
            "Keyword Fast Replies",
            "Broadcast API",
            "Multi-day Follow-up Scheduler"
        ],
        "leads_in_memory": len(conversation_state),
        "pending_followups": sum(1 for f in follow_up_queue if not f["done"]),
        "gemini_active": gemini_client is not None,
        "sheets_configured": bool(SHEETS_ID and GOOGLE_CREDENTIALS_JSON != "{}")
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ENTRY POINT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
