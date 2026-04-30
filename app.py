"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The Oxford Computers — WhatsApp AI System v2.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Features:
  ✅ Google Sheets CRM — auto lead logging
  ✅ Gemini AI Chatbot — Malayalam/English smart replies
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

# ✅ NEW SDK — google-genai (replaces deprecated google-generativeai)
from google import genai

app = Flask(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURATION — All from Railway Environment Variables
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
# ✅ GEMINI AI SETUP — New google-genai SDK
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    print("✅ Gemini AI initialized (google-genai SDK)")
else:
    gemini_client = None
    print("⚠️ GEMINI_API_KEY not set — AI replies disabled")

# In-memory conversation state (resets on redeploy — use DB for production)
conversation_state = {}  # {phone: {"stage": "new/interested/enrolled", "name": "", "last_msg": timestamp}}
follow_up_queue    = []  # [{phone, name, send_at, message, done}]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# INSTITUTE PROFILE — Oxford Computers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INSTITUTE_INFO = """
You are Aaliza, a warm and experienced Senior Admission 
Counselor at The Oxford Computers, Malayinkeezhu, 
Thiruvananthapuram, Kerala.

YOUR PERSONALITY:
- Speak like a knowledgeable friend, not a salesperson
- Warm, patient, encouraging tone always
- Reply in the SAME language the student uses
  (Malayalam, English, or Manglish mix)
- Use appropriate emojis naturally
- Keep replies concise — max 5-6 lines
- Never use corporate/formal language

YOUR EXPERTISE:
- Deep knowledge of all Oxford courses
- Understanding of Kerala job market
- Aware of Gulf NRI student needs
- Knowledge of government certification value

INSTITUTE DETAILS:
Name: The Oxford Computers
Location: Malayinkeezhu Junction, Thiruvananthapuram
Approval: Kerala State Rutronix Government Certified
Website: theoxfordedu.com
Phone: 9447329972
Speciality: AI-enabled, government-certified courses

COURSES:
1. PGDCA — 12 Months — ₹15,999
   (Programming, DBMS, Web Dev, Networks, Mobile App)
   Best for: Graduates seeking IT career

2. AIDM — AI-Driven Digital Marketing — 6 Months — ₹19,999
   (SEO, Google Ads, Meta Ads, ChatGPT, Live Campaigns)
   Best for: Entrepreneurs, marketing professionals

3. SAP Financial Accounting — 4-6 Months — ₹11,999
   (ERP, General Ledger, SAP CO, Real projects)
   Best for: Accounting/finance professionals

4. Python Programming — 3 Months — ₹4,499
   (Basics to Advanced, Flask, Pandas, Automation)
   Best for: Students wanting programming skills

5. GST & Payroll Diploma — 6 Months — ₹5,499
   (GST Filing, Tally Prime, Income Tax, E-filing)
   Best for: Accounting jobs, business owners

6. DCA Fast Track — 6 Months — ₹6,400
   (MS Office, Programming, Database, DTP)
   Best for: Quick job-ready skills

7. Computer Teacher Training — 1 Year — ₹7,999
   (Teaching methodology, curriculum, practice)
   Best for: Those wanting to teach computers

8. Corporate Business Accounting — 1 Year — ₹7,999
   (Corporate accounting, GST, Financial modelling)
   Best for: Corporate finance careers

9. Word Processing & Data Entry — 3 Months — ₹4,800
   (Typing, MS Word, DTP, Data entry)
   Best for: Office jobs, quick employment

10. Web Designing — 6 Months — ₹5,999
    (HTML5, CSS3, JavaScript, PHP, WordPress)
    Best for: Freelancing, web development career

KEY BENEFITS (mention naturally when relevant):
- Government certified certificate (Kerala State Rutronix)
- AI-enabled modern curriculum
- 100% placement assistance
- Flexible batch timings (morning/afternoon/evening)
- EMI facility available
- Free demo class available
- Alumni working in Kerala & Gulf

HANDLING COMMON SITUATIONS:

If student asks about competitors or other institutes:
→ Never mention or compare. Say: "Each institute has 
its own strengths. What I can tell you is what makes 
Oxford special for YOUR goals..." then focus on benefits.

If student says "fees is too high":
→ "I understand. Let me break it down — 
[monthly EMI calculation]. Plus government certificate 
increases your salary by ₹5,000-10,000/month. 
ROI is within 2-3 months of getting a job! 
Shall I explain the EMI options?"

If student seems confused about which course:
→ Ask 2-3 friendly questions about their background, 
goals, current job situation. Then recommend best fit.

If student is a parent enquiring for child:
→ Switch to more formal, reassuring tone.
→ Emphasize government certification, safety, 
placement record, institute reputation.

If student says "I'll think about it":
→ "Of course! Take your time. 
Meanwhile, why not attend our FREE demo class? 
Zero commitment — just come and see for yourself. 
Shall I book a slot for you?"

If student asks something you don't know:
→ "Great question! Let me connect you with our 
team for the exact details. 
Call/WhatsApp: 📞 9447329972"

IMPORTANT RULES:
- NEVER make up fees, dates, or facts
- NEVER promise 100% job guarantee (say "placement assistance")
- NEVER badmouth any competitor, institution, or course
- ALWAYS end with a soft CTA (demo class, call, visit)
- NEVER write more than 6 lines in one message
- If unsure, ask a clarifying question
"""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# KEYWORD FAST REPLIES (No AI needed — instant response)
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
        "നിന്ന് വിളിക്കാം — 📞 9447329972\n\n"
        "കൂടുതൽ info: 🌐 theoxfordedu.com\n\n"
        "വീണ്ടും സംസാരിക്കാൻ \n"
        "ഇവിടെ message cheyyoo! 😊"
    ),

    # Greetings — None means use welcome message
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
            send_interactive_message(from_number, reply, exclude_btn)

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
def add_menu_footer(message):
    menu = (
        "\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "📌 *Quick Menu:*\n"
        "📚 *COURSES* — All courses list\n"
        "🎓 *DEMO* — Free demo class\n"
        "💰 *FEES* — Fee details\n"
        "🔥 *OFFER* — Today's special offer\n"
        "🚪 *EXIT* — End conversation"
    )
    return message + menu


def get_smart_reply(msg_text, name, phone, is_new_lead):
    reply, exclude_btn = _get_smart_reply_internal(msg_text, name, phone, is_new_lead)
    is_exit_reply = (msg_text.lower().strip() == "exit")
    if is_exit_reply:
        exclude_btn = "NO_BUTTONS"
    return reply, exclude_btn


def _get_smart_reply_internal(msg_text, name, phone, is_new_lead):
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
                f"✅ {selected_time} confirmed!\n\n"
                f"Preferred date ഏതാണ്?\n"
                f"(Example: Tomorrow, Monday, April 30)\n\n"
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
            f"🎉 *Demo Class Booked!*\n\n"
            f"📚 Course: {course}\n"
            f"⏰ Time: {batch_time}\n"
            f"📅 Date: {user_date}\n"
            f"📍 The Oxford Computers, Malayinkeezhu\n\n"
            f"നാളെ ഞങ്ങൾ WhatsApp-ൽ confirm ചെയ്യും!\n"
            f"കൂടുതൽ info: 📞 9447329972\n"
            f"🌐 theoxfordedu.com"
        ), "NO_BUTTONS"

    if current_stage == "offer_selection":
        if msg_lower == "1":
            state["stage"] = "payment_sent"
            state["offer_course"] = "CWPDE"
            return (
                "✅ *CWPDE — Great Choice!*\n\n"
                "📚 Certificate in Word Processing & Data Entry\n"
                "⏱ Duration: 6 Months\n"
                "🎓 Certificate: Rutronix Approved\n"
                "💰 Special Price: *₹4,800*\n"
                "📌 Exam Fee: separately as per Rutronix norms\n\n"
                "👇 *Secure Payment Link:*\n"
                "https://rzp.io/rzp/xkWdKtd\n\n"
                "Payment ചെയ്ത ശേഷം:\n"
                "📩 Transaction ID ഇവിടെ reply cheyyoo\n"
                "(Example: T2504281234)\n\n"
                "Seat ഉടൻ confirm ആകും! 🎉\n"
                "📍 The Oxford Computers, Malayinkeezhu\n"
                "📞 9447329972"
            ), "NO_BUTTONS"
        elif msg_lower == "2":
            state["stage"] = "payment_sent"
            state["offer_course"] = "DCA"
            return (
                "✅ *DCA — Great Choice!*\n\n"
                "📚 Diploma in Computer Applications (Fast Track)\n"
                "⏱ Duration: 6 Months\n"
                "🎓 Certificate: Rutronix + State Approved\n"
                "💰 Special Price: *₹6,400*\n"
                "📌 Exam Fee: separately as per Rutronix norms\n\n"
                "👇 *Secure Payment Link:*\n"
                "https://rzp.io/rzp/mJPPtM9x\n\n"
                "Payment ചെയ്ത ശേഷം:\n"
                "📩 Transaction ID ഇവിടെ reply cheyyoo\n"
                "(Example: T2504281234)\n\n"
                "Seat ഉടൻ confirm ആകും! 🎉\n"
                "📍 The Oxford Computers, Malayinkeezhu\n"
                "📞 9447329972"
            ), "NO_BUTTONS"
        elif msg_lower == "3":
            state["stage"] = "payment_sent"
            state["offer_course"] = "AIDM"
            return (
                "✅ *AIDM — Great Choice!*\n\n"
                "📚 AI-Driven Digital Marketing\n"
                "⏱ Duration: 6 Months\n"
                "🎓 Certificate: Industry Recognized\n"
                "💰 Special Price: *₹19,999*\n"
                "📌 Exam Fee: separately as per Rutronix norms\n\n"
                "👇 *Secure Payment Link:*\n"
                "https://rzp.io/rzp/vF76sj7Y\n\n"
                "Payment ചെയ്ത ശേഷം:\n"
                "📩 Transaction ID ഇവിടെ reply cheyyoo\n"
                "(Example: T2504281234)\n\n"
                "Seat ഉടൻ confirm ആകും! 🎉\n"
                "📍 The Oxford Computers, Malayinkeezhu\n"
                "📞 9447329972"
            ), "NO_BUTTONS"
        elif msg_lower == "4":
            state["stage"] = "payment_sent"
            state["offer_course"] = "PGDCA"
            return (
                "✅ *PGDCA — Great Choice!*\n\n"
                "📚 Post Graduate Diploma in Computer Applications\n"
                "⏱ Duration: 12 Months\n"
                "🎓 Certificate: Rutronix + State Approved\n"
                "💰 Special Price: *₹15,999*\n"
                "📌 Exam Fee: separately as per Rutronix norms\n\n"
                "👇 *Secure Payment Link:*\n"
                "https://rzp.io/rzp/KAQ2C7t\n\n"
                "Payment ചെയ്ത ശേഷം:\n"
                "📩 Transaction ID ഇവിടെ reply cheyyoo\n"
                "(Example: T2504281234)\n\n"
                "Seat ഉടൻ confirm ആകും! 🎉\n"
                "📍 The Oxford Computers, Malayinkeezhu\n"
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
            f"🎉 *Payment Received!*\n\n"
            f"✅ Transaction ID: {user_txn}\n"
            f"📚 Course: {offer_course}\n"
            f"👤 Name: {name}\n\n"
            f"Seat confirmed! Welcome to \n"
            f"*The Oxford Computers* 🎓\n\n"
            f"📞 Next step: 9447329972 ലേക്ക് \n"
            f"വിളിക്കൂ — batch details കിട്ടും!\n\n"
            f"📍 Malayinkeezhu, Thiruvananthapuram\n"
            f"🌐 theoxfordedu.com\n\n"
            f"കാണാൻ കാത്തിരിക്കുന്നു! 😊"
        ), "NO_BUTTONS"

    # 3. Smart routing — keywords only for exact/short matches
    exact_keywords = [
        "fees", "fee", "price", "demo", "courses", "course",
        "offer", "hi", "hello", "hai", "hii", "location",
        "address", "timing", "batch", "certificate", "job",
        "placement", "നമസ്കാരം", "എവിടെ", "exit",
        "pgdca", "pgd", "aidm", "sap", "python", "gst",
        "tally", "dca", "teacher", "accounting", "web",
        "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"
    ]

    if msg_lower.strip() not in exact_keywords and gemini_client:
        return get_gemini_reply(msg_text, name), None

    # 4. Short message — check keywords
    # Explicit 'demo'
    if msg_lower == "demo" or (len(msg_lower) <= 20 and "free class" in msg_lower):
        state["stage"] = "demo_time_selection"
        return (
            "🎓 *Free Demo Class Booking*\n\n"
            "Preferred batch time ഏത്?\n\n"
            "1️⃣ Morning — 9 AM to 11 AM\n"
            "2️⃣ Afternoon — 12 PM to 2 PM  \n"
            "3️⃣ Evening — 5 PM to 7 PM\n\n"
            "Number reply cheyyoo! 📅"
        ), "DEMO"

    # Explicit 'offer'
    if msg_lower == "offer" or (len(msg_lower) <= 20 and "offer" in msg_lower):
        state["stage"] = "offer_selection"
        return (
            "🔥 *Today's Special Offer!*\n"
            "━━━━━━━━━━━━━━━━\n"
            "🎓 Kerala State Rutronix Approved\n\n"
            "1️⃣ CWPDE — Word Processing & Data Entry\n"
            "   💰 Special Price: *₹4,800*\n"
            "   ⏱ Duration: 6 Months\n\n"
            "2️⃣ DCA — Diploma in Computer Applications\n"
            "   💰 Special Price: *₹6,400*\n"
            "   ⏱ Duration: 6 Months\n\n"
            "3️⃣ AIDM — AI-Driven Digital Marketing\n"
            "   💰 Special Price: *₹19,999*\n"
            "   ⏱ Duration: 6 Months\n\n"
            "4️⃣ PGDCA — Post Graduate Diploma\n"
            "   💰 Special Price: *₹15,999*\n"
            "   ⏱ Duration: 12 Months\n"
            "━━━━━━━━━━━━━━━━\n"
            "⚡ Limited Time Offer!\n"
            "📅 Seats limited — Book now!\n\n"
            "Number reply cheyyoo — \n"
            "Payment link ഉടൻ അയക്കാം! 💳"
        ), "OFFER"

    # Smart routing → Gemini AI unless it's an exact short keyword
    if len(msg_lower) > 10 and gemini_client:
        # Check if it's NOT an exact keyword match
        exact_keywords = [
            "fees", "fee", "demo", "courses", "course", "offer",
            "hi", "hello", "hai", "hii", "location", "address",
            "timing", "batch", "certificate", "job", "placement",
            "നമസ്കാരം", "എവിടെ",
            "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"
        ]
        if msg_lower.strip() not in exact_keywords:
            return get_gemini_reply(msg_text, name), None

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
                f"✅ *{course_name}* — Great choice!\n\n"
                f"{course_details}\n"
                f"📍 The Oxford Computers, Malayinkeezhu"
            ), "COURSES"

    # 6. Gemini AI fallback
    if gemini_client:
        return get_gemini_reply(msg_text, name), None

    # Fallback if no AI configured
    return get_fallback_reply(name), None


def get_welcome_message(name):
    return (
        f"👋 നമസ്കാരം *{name}*!\n\n"
        f"*The Oxford Computers*-ലേക്ക് സ്വാഗതം! 🎓\n\n"
        f"Kerala Government certified, AI-enabled courses.\n\n"
        f"Course details ariyaan *COURSES* reply cheyyoo! 📚\n\n"
        f"📍 Malayinkeezhu, Thiruvananthapuram\n"
        f"🌐 theoxfordedu.com\n\n"
        f"Free demo class-നായി *DEMO* reply ചെയ്യൂ! 🙌\n"
        f"Fees അറിയാൻ *FEES* reply ചെയ്യൂ! 💰"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ✅ GEMINI AI REPLY — New google-genai SDK
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_gemini_reply(msg_text, name):
    """Get AI-powered reply using google-genai SDK"""
    try:
        prompt = f"""
{INSTITUTE_INFO}

Student name: {name}
Student message: "{msg_text}"

Reply as the Oxford Computers AI assistant. Keep reply under 200 words.
Use Malayalam if student wrote in Malayalam, English if in English.
Always end with an actionable suggestion (demo class, call, visit).
"""
        # ✅ New SDK: client.models.generate_content()
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        return response.text.strip()

    except Exception as e:
        print(f"⚠️ Gemini error: {e}")
        return get_fallback_reply(name)


def get_fallback_reply(name):
    return (
        f"നന്ദി {name}! 😊\n\n"
        f"കൂടുതൽ വിവരങ്ങൾക്ക്:\n"
        f"🌐 theoxfordedu.com\n"
        f"📍 Malayinkeezhu, Thiruvananthapuram\n\n"
        f"Free demo class-നായി *DEMO* reply ചെയ്യൂ! 🎓"
    )


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
            print("⚠️ Sheets skipped — SHEETS_ID or GOOGLE_CREDENTIALS not configured")
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
            "ഒരു *free demo class* try ചെയ്ത് നോക്കൂ —\n"
            "zero commitment, 100% free! 🎓\n\n"
            "*DEMO* reply ചെയ്താൽ book ചെയ്യാം!"
        )
    },
    {
        "day": 3,
        "hours": 72,
        "message": (
            "👋 {name}, Oxford Computers here!\n\n"
            "🌟 *Student Success Story*\n\n"
            "Riya (Attingal) — Web Design course complete ചെയ്ത്\n"
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
            "The Oxford Computers — *Special Offer*\n\n"
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
    """Background thread — checks every 5 min and sends due follow-ups"""
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
                            print(f"⏭️ Follow-up skipped — {item['name']} recently active")
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
# SEND WHATSAPP MESSAGE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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
    """Sends WhatsApp interactive message with reply buttons"""
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
