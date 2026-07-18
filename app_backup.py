# -*- coding: utf-8 -*-
"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  The Oxford Computers — WhatsApp AI System v3.0
  Clean rewrite — zero patch conflicts
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ✅ Google Sheets CRM
  ✅ Gemini 2.0 Flash (google-genai SDK)
  ✅ Stage-based conversation state machine
  ✅ Interactive WhatsApp buttons (named presets)
  ✅ Broadcast + Template broadcast API
  ✅ Multi-day follow-up scheduler
  ✅ Admin stats + manual trigger endpoints
  ✅ Admin panel (panel.html)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import json
import time
import random
import threading
import requests
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google import genai

app = Flask(__name__)

def validate_token():
    r = requests.get(
        f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
    )
    if r.status_code == 200:
        print("✅ WhatsApp token valid")
    else:
        print(f"❌ Token invalid: {r.status_code} — {r.text}")

threading.Thread(target=validate_token, daemon=True).start()


# ═══════════════════════════════════════════════════════
#  CONFIGURATION  (Railway environment variables)
# ═══════════════════════════════════════════════════════
VERIFY_TOKEN         = os.environ.get("VERIFY_TOKEN", "oxford2026")
ACCESS_TOKEN         = os.environ.get("ACCESS_TOKEN", "")
PHONE_NUMBER_ID      = os.environ.get("PHONE_NUMBER_ID", "")
SHEETS_ID            = os.environ.get("SHEETS_ID", "")
GEMINI_API_KEY       = os.environ.get("GEMINI_API_KEY", "")
BROADCAST_API_KEY    = os.environ.get("BROADCAST_API_KEY", "Oxford_Broadcast#2026!LmQ8Pz")
ADMIN_KEY            = os.environ.get("ADMIN_KEY", "oxford_admin_2026")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS", "{}")

WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"

# ═══════════════════════════════════════════════════════
#  GEMINI AI SETUP
# ═══════════════════════════════════════════════════════
if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    print("✅ Gemini AI initialised (google-genai SDK, gemini-2.0-flash)")
else:
    gemini_client = None
    print("⚠️  GEMINI_API_KEY not set — AI replies disabled")

# ═══════════════════════════════════════════════════════
#  IN-MEMORY STORES
# ═══════════════════════════════════════════════════════
# { phone: { name, stage, course, goal, batch_time,
#            offer_course, last_msg, last_text } }
conversation_state: dict = {}

# [ { phone, name, send_at, message, day, done } ]
follow_up_queue: list = []


# ═══════════════════════════════════════════════════════
#  AALIZA — SYSTEM PROMPT FOR GEMINI
# ═══════════════════════════════════════════════════════
AALIZA_PROMPT = """
You are Aaliza, Senior Admission Counselor at The Oxford Computers, Malayinkeezhu, Thiruvananthapuram, Kerala.

YOUR SOLE GOAL:
Convert the student into one of these three actions:
  1. Book a free demo class
  2. Visit the office
  3. Make a payment / reserve a seat

YOUR COMMUNICATION STYLE:
- Speak exactly like a warm, confident Malayali senior counselor.
- Use natural Malayalam/Manglish mix. Real human tone, not corporate.
- Maximum 4-6 lines per reply. Never longer.
- One focused question per reply only.
- ALWAYS end with one of:
    "Demo book cheyyatte?"
    or "Office visit cheyyano?"
    or "Seat reserve cheyyano?"

STRICT RULES:
- NEVER list all courses unless student explicitly asks.
- NEVER say "job guarantee" — always say "placement assistance".
- NEVER badmouth any competitor.
- NEVER repeat a question already asked.
- If goal is clear, skip goal question — recommend 1-2 best courses directly.
- If goal is unclear, ask qualification + career goal FIRST.
- Create gentle urgency: "limited seats", "next batch starting soon".
- If fees concern, explain EMI + ROI logic immediately.
- If student says "I will think" or "nokkatte", push free demo softly — not payment.
- If student says "not interested", politely ask reason and reframe.
- If student says "no time", mention flexible morning/evening batches.
- If student says "confused", reassure and ask qualification + goal.

INSTITUTE DETAILS:
Name: The Oxford Computers
Location: Malayinkeezhu Junction, Thiruvananthapuram, Kerala
Approval: Kerala State Rutronix Government Certified
Website: theoxfordedu.com | Phone: 9447329972

COURSES & FEES:
1. PGDCA                    — 12 Months — ₹15,999
2. AIDM (AI Digital Mktg)  — 6 Months  — ₹19,999
3. SAP Financial Accounting — 4-6 Months— ₹11,999
4. Python Programming       — 3 Months  — ₹4,499
5. GST & Payroll Diploma    — 6 Months  — ₹18,999
6. DCA Fast Track           — 6 Months  — ₹6,400
7. Computer Teacher Training— 1 Year    — ₹11,999
8. Corporate Biz Accounting — 1 Year    — ₹40,000
9. Word Processing & Entry  — 6 Months  — ₹4,800
10. Web Designing           — 6 Months  — ₹8,800

HOOK + VALUE + CTA STYLE — ALWAYS follow this:
"Digital Marketing ippol demand und 👍
Freelance + business growth randinum useful aanu.
AIDM ningalkku nalla option aanu.
Oru free demo kaanumbo clarity varum… book cheyyatte? 🎓"

OBJECTION HANDLING — use these exact styles:

User: "fees high aanu"
Aaliza:
"Athu doubt varunnath normal aanu 😊
Pakshe ithu expense alla… skill investment aanu.
EMI option und, so tension venda 👍
Demo kaanumbo value clear aavum… book cheyyatte?"

User: "njan nokkatte"
Aaliza:
"Sure 😊 take your time.
Pakshe demo kaanathe decision edukkaruthu.
Just 1 free class kaanumbo clarity varum 👍
Book cheyyatte?"

User: "interest illa"
Aaliza:
"Ok 😊 problem illa.
Just ariyan… interest illa ennath course type kondaano,
time issue aano? Njan better option suggest cheyyam 👍"

User: "time illa"
Aaliza:
"Athu common issue aanu 😊
Athinu morning/evening flexible batches und.
Schedule adjust cheythu padikkaam 👍
Demo-il timing clear cheyyam… varamo?"

User: "confused aanu"
Aaliza:
"Confuse aavunnath normal aanu 😊
Njan simple aayi guide cheyyam.
+2 / Degree / Working aano?
Job aanu main goal alle?"
"""

# ═══════════════════════════════════════════════════════
#  COURSE DETAIL CARDS
# ═══════════════════════════════════════════════════════
_PGDCA = (
    "📚 *PGDCA — Post Graduate Diploma in Computer Applications*\n"
    "⏱ 12 Months | 🎓 Rutronix + State Approved\n"
    "📋  C++/ Java, PYTHON, DBMS, Web Dev, Networks, Mobile App, Final Project\n"
    "💡 Best for graduates seeking an Govt+IT career\n"
    "💰 Fee: ₹15,999"
)
_AIDM = (
    "📚 *AIDM — AI-Driven Digital Marketing*\n"
    "⏱ 6 Months | 🎓 Industry Recognised\n"
    "📋 SEO, Social Media, Google Ads, Meta Ads, ChatGPT/AI Tools, Live Campaigns\n"
    "💡 Best for marketers, entrepreneurs, beginners\n"
    "💰 Fee: ₹19,999"
)
_SAP = (
    "📚 *SAP Financial Accounting & Controlling*\n"
    "⏱ 4-6 Months | 🎓 SAP Alliance Certificate\n"
    "📋 GL Accounting, AP/AR, Asset Accounting, SAP CO, Real-Time Project\n"
    "💡 Best for commerce graduates & accounting professionals\n"
    "💰 Fee: ₹15,999"
)
_PYTHON = (
    "📚 *Python — Beginner to Advanced*\n"
    "⏱ 3 Months | 🎓 Rutronix Certified\n"
    "📋 OOP, File Handling, Flask Basics, Pandas, Web Scraping, Automation\n"
    "💡 Best for beginners and IT aspirants\n"
    "💰 Fee: ₹4,499"
)
_GST = (
    "📚 *Diploma in GST, Taxation & Payroll*\n"
    "⏱ 6 Months | 🎓 Rutronix Certified\n"
    "📋 GST Concepts, Income Tax, Tally Prime, Payroll Processing, E-filing\n"
    "💡 Best for accounting professionals & commerce students\n"
    "💰 Fee: ₹18999"
)
_DCA = (
    "📚 *DCA — Diploma in Computer Applications (Fast Track)*\n"
    "⏱ 6 Months | 🎓 Rutronix + State Approved\n"
    "📋 Computer Fundamentals, MS Office, Programming Basics, Internet, Database, DTP\n"
    "💡 Best for students and office job seekers\n"
    "💰 Fee: ₹6,400"
)
_TEACHER = (
    "📚 *Computer Teacher Training Course*\n"
    "⏱ 1 Year | 🎓 Rutronix Certified\n"
    "📋 Teaching Methodology, MS Office Pedagogy, Programming Basics, Practice Teaching\n"
    "💡 Best for aspiring computer teachers\n"
    "💰 Fee: ₹11,999"
)
_ACCOUNTING = (
    "📚 *Diploma in Corporate Business Accounting & Taxation*\n"
    "⏱ 1 Year | 🎓 Rutronix Certified\n"
    "📋 Corporate Accounting, GST, Income Tax Corporate, Financial Modelling, Case Studies\n"
    "💡 Best for advanced accounting and finance careers\n"
    "💰 Fee: ₹40000"
)
_WORD = (
    "📚 *Certificate in Word Processing & Data Entry*\n"
    "⏱ 6 Months | 🎓 Rutronix Certified\n"
    "📋 Touch Typing, MS Word, Data Entry Techniques, DTP Basics, Document Management\n"
    "💡 Best for data entry professionals and beginners\n"
    "💰 Fee: ₹4,800"
)
_WEB = (
    "📚 *Professional Diploma in Web Designing*\n"
    "⏱ 6 Months | 🎓 Rutronix Certified\n"
    "📋 HTML5, CSS3, JavaScript, jQuery, PHP & MySQL, WordPress, Portfolio Project\n"
    "💡 Best for aspiring web developers and designers\n"
    "💰 Fee: ₹5,999"
)

# Index → (name, card)
ALL_COURSES = {
    "1":  ("PGDCA",                              _PGDCA),
    "2":  ("AIDM Digital Marketing",             _AIDM),
    "3":  ("SAP Financial Accounting",           _SAP),
    "4":  ("Python Programming",                 _PYTHON),
    "5":  ("GST & Payroll",                      _GST),
    "6":  ("DCA Fast Track",                     _DCA),
    "7":  ("Computer Teacher Training",          _TEACHER),
    "8":  ("Corporate Business Accounting",      _ACCOUNTING),
    "9":  ("Word Processing & Data Entry",       _WORD),
    "10": ("Professional Web Designing",         _WEB),
}

COURSE_FEES = {
    "PGDCA":                         ("₹15,999", "12 Months"),
    "AIDM Digital Marketing":        ("₹19,999", "6 Months"),
    "SAP Financial Accounting":      ("₹15,999", "4-6 Months"),
    "Python Programming":            ("₹4,499",  "3 Months"),
    "GST & Payroll":                 ("₹18,999",  "6 Months"),
    "DCA Fast Track":                ("₹6,400",  "6 Months"),
    "Computer Teacher Training":     ("₹11,999",  "1 Year"),
    "Corporate Business Accounting": ("₹40000",  "1 Year"),
    "Word Processing & Data Entry":  ("₹4,800",  "6 Months"),
    "Professional Web Designing":    ("₹8,800",  "6 Months"),
}

# Keyword → course name (for short-word triggers)
KEYWORD_TO_COURSE = {
    "pgdca": "1", "pgd": "1",
    "aidm": "2", "digital marketing": "2", "digital": "2",
    "sap": "3", "erp": "3",
    "python": "4", "programming": "4", "coding": "4",
    "gst": "5", "tally": "5", "taxation": "5", "payroll": "5",
    "dca": "6", "fast track": "6",
    "teacher": "7", "teaching": "7",
    "accounting": "8", "corporate": "8",
    "data entry": "9", "typing": "9", "word processing": "9",
    "web": "10", "web design": "10", "wordpress": "10", "html": "10",
}

# Goal → recommended course indices
GOAL_COURSES = {
    "job": [
        ("1",  "PGDCA — Post Graduate Diploma",    "12 Months", "₹15,999"),
        ("4",  "Python Programming",               "3 Months",  "₹4,499"),
        ("10", "Web Designing",                    "6 Months",  "₹8,800"),
        ("6",  "DCA Fast Track",                   "6 Months",  "₹6,400"),
    ],
    "business": [
        ("2",  "AI-Driven Digital Marketing",      "6 Months",  "₹19,999"),
        ("10", "Web Designing",                    "6 Months",  "₹8,800"),
        ("4",  "Python Programming",               "3 Months",  "₹4,499"),
    ],
    "basic": [
        ("6",  "DCA Fast Track",                   "6 Months",  "₹6,400"),
        ("9",  "Word Processing & Data Entry",     "6 Months",  "₹4,800"),
        ("7",  "Computer Teacher Training",        "1 Year",    "₹11,999"),
    ],
    "accounting": [
        ("3",  "SAP Financial Accounting",         "4-6 Months","₹11,999"),
        ("5",  "GST & Payroll Diploma",            "6 Months",  "₹18,999"),
        ("8",  "Corporate Business Accounting",    "1 Year",    "₹40,000"),
    ],
}

# Offer / payment menu
OFFER_MENU = {
    "1": ("CWPDE", "Certificate in Word Processing & Data Entry",      "₹4,800",  "6 Months",  "https://rzp.io/rzp/xkWdKtd"),
    "2": ("DCA",   "Diploma in Computer Applications",                 "₹6,400",  "6 Months",  "https://rzp.io/rzp/mJPPtM9x"),
    "3": ("AIDM",  "AI-Driven Digital Marketing",                      "₹19,999", "6 Months",  "https://rzp.io/rzp/vF76sj7Y"),
    "4": ("PGDCA", "Post Graduate Diploma in Computer Applications",   "₹15,999", "12 Months", "https://rzp.io/rzp/KAQ2C7t"),
}

# Direct payment links keyed by course name (from st["course"])
# Courses without a link will show counselor fallback message
COURSE_PAYMENT_LINKS = {
    "Word Processing & Data Entry":  ("CWPDE", "Certificate in Word Processing & Data Entry", "₹4,800",  "6 Months",  "https://rzp.io/rzp/xkWdKtd"),
    "DCA Fast Track":                ("DCA",   "Diploma in Computer Applications",            "₹6,400",  "6 Months",  "https://rzp.io/rzp/mJPPtM9x"),
    "AIDM Digital Marketing":        ("AIDM",  "AI-Driven Digital Marketing",                 "₹19,999", "6 Months",  "https://rzp.io/rzp/vF76sj7Y"),
    "PGDCA":                         ("PGDCA", "Post Graduate Diploma in Computer Applications", "₹15,999", "12 Months", "https://rzp.io/rzp/KAQ2C7t"),
}

FULL_FEE_TABLE = (
    "💰 *Course Fees — The Oxford Computers*\n"
    "━━━━━━━━━━━━━━━━\n"
    "1️⃣  PGDCA                  ₹15,999  (12M)\n"
    "2️⃣  AIDM Digital Marketing ₹19,999  (6M)\n"
    "3️⃣  SAP Accounting         ₹11,999  (4-6M)\n"
    "4️⃣  Python Programming     ₹4,499   (3M)\n"
    "5️⃣  GST & Payroll          ₹18,999  (6M)\n"
    "6️⃣  DCA Fast Track         ₹6,400   (6M)\n"
    "7️⃣  Computer Teaching      ₹11,999   (1Y)\n"
    "8️⃣  Business Accounting    ₹40,000   (1Y)\n"
    "9️⃣  Word Processing        ₹4,800   (6M)\n"
    "🔟 Web Designing           ₹8,800   (6M)\n"
    "━━━━━━━━━━━━━━━━\n"
    "🎓 Kerala State Rutronix Approved\n"
    "📊 EMI / installment option available!\n\n"
    "Ithu one-time investment aanu — job kittiyal\n"
    "1-2 months-il fee recover cheyyam! 💪\n\n"
    "Free demo book cheyyano? → *DEMO* reply cheyyoo"
)


# ═══════════════════════════════════════════════════════
#  INTERACTIVE BUTTON PRESETS
#  WhatsApp allows max 3 buttons per interactive message
# ═══════════════════════════════════════════════════════
BUTTON_PRESETS = {
    "GOAL": [
        {"id": "1", "title": "💼 Job / IT Career"},
        {"id": "2", "title": "🚀 Business/Freelance"},
        {"id": "3", "title": "🖥️ Basic Computer"},
    ],
    "GOAL_MORE": [
        {"id": "4", "title": "📊 Accounting / Tax"},
        {"id": "5", "title": "🤔 Help me choose"},
        {"id": "DEMO", "title": "🎓 Free Demo"},
    ],
    "COURSE": [
        {"id": "DEMO",  "title": "🎓 Free Demo"},
        {"id": "FEES",  "title": "💰 See Fees"},
        {"id": "VISIT", "title": "🏢 Visit Office"},
    ],
    "FEES": [
        {"id": "DEMO",       "title": "🎓 Free Demo"},
        {"id": "ENROLL_NOW", "title": "💳 Enrol Now"},
        {"id": "CALL",       "title": "📞 Call Us"},
    ],
    "OFFER": [
        {"id": "DEMO",  "title": "🎓 Free Demo"},
        {"id": "VISIT", "title": "🏢 Visit Office"},
        {"id": "CALL",  "title": "📞 Call Us"},
    ],
    "AFTER_BOOKING": [
        {"id": "COURSES",    "title": "📚 More Courses"},
        {"id": "ENROLL_NOW", "title": "💳 Enrol Now"},
        {"id": "VISIT",      "title": "🏢 Visit Office"},
    ],
}


# ═══════════════════════════════════════════════════════
#  HELPERS — build state default
# ═══════════════════════════════════════════════════════
def _default_state(name: str) -> dict:
    return {
        "name":         name,
        "stage":        "new",
        "course":       "",
        "goal":         "",
        "batch_time":   "",
        "offer_course": "",
        "last_msg":     datetime.now().isoformat(),
        "last_text":    "",
    }


def _state(phone: str, name: str) -> dict:
    if phone not in conversation_state:
        conversation_state[phone] = _default_state(name)
    return conversation_state[phone]


# ═══════════════════════════════════════════════════════
#  WHATSAPP SEND FUNCTIONS
# ═══════════════════════════════════════════════════════
def _wa_headers() -> dict:
    return {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def send_text(to: str, text: str) -> requests.Response:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    r = requests.post(WHATSAPP_API_URL, headers=_wa_headers(), json=payload)
    print(f"📤 text → {to}  HTTP {r.status_code}")
    return r


def send_interactive(to: str, body: str, preset: str) -> requests.Response:
    """Send message with up to 3 reply buttons from a named preset."""
    buttons_data = BUTTON_PRESETS.get(preset, BUTTON_PRESETS["COURSE"])
    buttons = [{"type": "reply", "reply": b} for b in buttons_data]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": buttons},
        },
    }
    r = requests.post(WHATSAPP_API_URL, headers=_wa_headers(), json=payload)
    print(f"📤 interactive[{preset}] → {to}  HTTP {r.status_code}")
    if r.status_code != 200:
        print("⚠️  Interactive failed — falling back to plain text")
        return send_text(to, body)
    return r


def send_reply(to: str, body: str, preset: str | None) -> requests.Response:
    """Send text only or interactive depending on preset."""
    if not preset:
        return send_text(to, body)
    return send_interactive(to, body, preset)


def send_template(to: str, template: str, lang: str = "en",
                  components: list | None = None) -> requests.Response:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {"name": template, "language": {"code": lang}},
    }
    if components:
        payload["template"]["components"] = components
    return requests.post(WHATSAPP_API_URL, headers=_wa_headers(), json=payload)


# ═══════════════════════════════════════════════════════
#  GEMINI AI
# ═══════════════════════════════════════════════════════
def gemini_reply(user_msg: str, name: str, context: str = "") -> str | None:
    if not gemini_client:
        return None
    try:
        prompt = (
            f"{AALIZA_PROMPT}\n\n"
            f"{'Conversation so far:\n' + context + chr(10) if context else ''}"
            f"Student name: {name}\n"
            f"Student says: \"{user_msg}\"\n\n"
            f"Reply as Aaliza:"
        )
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        err = str(e).lower()
        if "429" in str(e) or "quota" in err or "resource" in err:
            print("⚠️  Gemini quota exceeded")
        else:
            print(f"⚠️  Gemini error: {e}")
        return None


def smart_fallback(name: str, msg: str = "") -> str:
    m = msg.lower()
    if any(w in m for w in ["fee", "price", "cost", "vila", "ethra","fees"]):
        return (
            f"😊 {name}, fees ariyaan government approved rates und!\n\n"
            "Courses ₹1,999 muthal thudangunnu.\n"
            "EMI / installment option um und! 📊\n\n"
            "Exact fee kaanan: *FEES* reply cheyyoo 💰\n"
            "Coursukal kanan *COURSES* ennu reply cheyyuka 📚\n"
            "📞 9447329972"
        )
    if any(w in m for w in ["job", "placement", "work", "career"]):
        return (
            f"{name}, nalla chodyam! 💪\n\n"
            "Oxford-il 100% placement assistance und.\n"
            "Students Kerala & Gulf-il work cheyyunnu. 🌍\n\n"
            "Best course ariyaan: *COURSES* reply cheyyoo 📚\n"
            "Or demo: *DEMO* 🎓"
        )
    return (
        f"😊 Nandi {name}!\n\n"
        "Njan Aaliza — The Oxford Computers-nte counselor.\n"
        "Ningalkku njan enthu help cheyyanam?\n\n"
        "📚 *COURSES* | 🎓 *DEMO* | 💰 *FEES*\n"
        "📞 9447329972"
    )


# ═══════════════════════════════════════════════════════
#  SCRIPT BANKS — randomised human phrases
# ═══════════════════════════════════════════════════════
DEMO_CTA = [
    "Oru free demo kaanan varamo? 🎓",
    "Demo class try cheythu nokkaamo? Zero risk aanu 👍",
    "One demo kaanumbozhe clarity varum 😊 book cheyyatte?",
    "Just oru demo mathram kaananam… ok aano?",
    "Ningal varumbo njan personally explain cheyyam 😊 demo book cheyyatte?",
]

COURSE_CLOSE = [
    "Ithu nalla future decision aanu 👍",
    "Ithu padichal real skill build aakum 💪",
    "Ithu career confidence koodan nalla option aanu.",
    "Beginners-kum easy aayi start cheyyan pattunna course aanu.",
]

URGENCY_LINES = [
    "Next batch starting soon aanu ⏳",
    "Limited seats aanu ippol ⚠️",
    "Late aayal next batch wait cheyyendi varum.",
    "Current batch fast fill aavunnu.",
]

TRUST_LINES = [
    "Kerala State Rutronix approved certificate aanu 🎓",
    "Placement assistance + interview support und 👍",
    "Practical training aanu, theory mathram alla.",
    "EMI / installment option available aanu.",
]

FEES_VALUE_LINES = [
    "Ithu one-time investment aanu 😊",
    "Nalla job kittiyal 1–2 months-il recover cheyyam 💪",
    "EMI option und, so full amount tension venda 👍",
    "Skill kittiyal athinte value long-term aanu.",
]

CONFUSED_LINES = [
    "Confuse aavunnath normal aanu 😊",
    "Problem illa, correct course choose cheyyan njan guide cheyyam 👍",
    "Ellarum first confuse aavum 😄 njan simple aayi explain cheyyam.",
]


def pick(items: list) -> str:
    """Return a random item from the list."""
    return random.choice(items)


# ═══════════════════════════════════════════════════════
#  CONVERSATION FLOWS  (pure functions → return (text, preset))
# ═══════════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════════
#  OBJECTION DETECTOR + HANDLER
# ═══════════════════════════════════════════════════════
def detect_objection(low: str) -> str | None:
    """Return objection type string or None."""
    if any(x in low for x in ["fees high", "fee high", "rate high", "expensive",
                               "costly", "kooduthal", "\u0d15\u0d42\u0d1f\u0d41\u0d24\u0d32\u0d4d", "high aanu"]):
        return "fees_high"
    if any(x in low for x in ["think", "nokkatte", "alochikkam", "later", "pinne",
                               "\u0d28\u0d4b\u0d15\u0d4d\u0d15\u0d1f\u0d4d\u0d1f\u0d46", "\u0d06\u0d32\u0d4b\u0d1a\u0d3f\u0d15\u0d4d\u0d15\u0d3e\u0d02"]):
        return "think_later"
    if any(x in low for x in ["interest illa", "not interested", "vend",
                               "\u0d35\u0d47\u0d23\u0d4d\u0d1f", "illa interest"]):
        return "not_interested"
    if any(x in low for x in ["time illa", "busy", "samayam illa", "\u0d38\u0d2e\u0d2f\u0d02 \u0d07\u0d32\u0d4d\u0d32"]):
        return "time_issue"
    if any(x in low for x in ["already job", "job und", "working", "work cheyyunnu"]):
        return "already_working"
    if any(x in low for x in ["confused", "doubt", "ariyilla", "not sure", "\u0d38\u0d02\u0d36\u0d2f\u0d02"]):
        return "confused"
    if any(x in low for x in ["free undo", "free aano", "free course", "\u0d38\u0d57\u0d1c\u0d28\u0d4d\u0d2f\u0d02"]):
        return "free_ask"
    return None


def handle_objection(kind: str, name: str, st: dict) -> tuple[str, str | None]:
    """Return (reply_text, preset) for known objection types."""
    if kind == "fees_high":
        text = (
            "Athu doubt varunnath normal aanu 😊\n\n"
            "Pakshe ithu expense alla\u2026 skill investment aanu 👍\n"
            f"{pick(FEES_VALUE_LINES)}\n\n"
            "Demo kaanumbo value clear aavum\u2026 book cheyyatte? 🎓"
        )
        return text, "FEES"

    if kind == "think_later":
        text = (
            "Sure 😊 take your time.\n\n"
            "Pakshe demo kaanathe decision edukkaruthu 👍\n"
            "Just 1 free class kaanumbo full clarity varum.\n\n"
            "Demo book cheyyatte?"
        )
        return text, "COURSE"

    if kind == "not_interested":
        text = (
            "Ok 😊 problem illa.\n\n"
            "Just ariyan\u2026 interest illa ennath course type kondaano,\n"
            "time issue aano, alle fees concern aano?\n\n"
            "Njan better option suggest cheyyam 👍"
        )
        return text, "COURSE"

    if kind == "time_issue":
        text = (
            "Athu common issue aanu 😊\n\n"
            "Athinu flexible batches und — morning / evening choose cheyyam 👍\n"
            "Schedule adjust cheythu padikkaam.\n\n"
            "Preferred time parayamo?"
        )
        return text, "COURSE"

    if kind == "already_working":
        text = (
            "Super 👍 already working aanenkil ithu upgrade aayi use cheyyam.\n\n"
            "Better salary / better role kittan extra skill help cheyyum 💪\n"
            "Part-time batch option und.\n\n"
            "Demo kaanumbo idea clear aavum\u2026 varamo?"
        )
        return text, "COURSE"

    if kind == "confused":
        text = (
            f"{pick(CONFUSED_LINES)}\n\n"
            "Ningal +2 / Degree / Working aano?\n"
            "Job aanu main goal alle?\n\n"
            "Reply cheyyoo — best course njan suggest cheyyam 🎓"
        )
        st["stage"] = "not_sure"
        return text, "GOAL"

    if kind == "free_ask":
        text = (
            "Full course free alla 😊\n\n"
            "Pakshe free demo class und 👍\n"
            "Athil course, fees, timing ellaam clear aayi explain cheyyam.\n\n"
            "Demo book cheyyatte?"
        )
        return text, "COURSE"

    return None, None


# ═══════════════════════════════════════════════════════
#  MAIN SMART REPLY ENGINE
#  Returns (reply_text, button_preset_or_None)
# ═══════════════════════════════════════════════════════
VISIT_WORDS   = {"visit", "office", "varam", "neritt", "address", "location",
                 "map", "route", "varanam", "edukkam"}
# NOTE: "confused" and "doubt" removed — handled by objection detector
CALL_WORDS    = {"call me", "call", "counselor", "talk to counselor",
                 "office number", "vilikku", "phone"}
GREETING_WORDS = {"hi", "hello", "hai", "hii", "hey", "namaskaram",
                  "നമസ്കാരം", "hy", "helo", "helloo"}


def smart_reply(msg_text: str, name: str, phone: str,
                is_new_lead: bool) -> tuple[str, str | None]:
    """
    Central router — stage-first, then keyword intercepts, then Gemini.
    NEVER resets stage unless the student explicitly triggers it.
    """
    raw = msg_text.strip()
    low = raw.lower()

    st = _state(phone, name)
    st["last_msg"]  = datetime.now().isoformat()
    st["last_text"] = raw
    stage   = st["stage"]
    course  = st["course"]

    # ── 0. WEBSITE DEFAULT MESSAGE (High Priority) ────
    if "course details" in low or "want course details" in low:
        st["stage"] = "goal_selection"
        st["course"] = ""
        return msg_website_lead()

    # ── 1. BRAND NEW LEAD ─────────────────────────────
    if is_new_lead:
        st["stage"] = "goal_selection"
        return msg_welcome(name)

    # ── 2. GLOBAL KEYWORD OVERRIDES (always work) ─────

    # Exit
    if low == "exit":
        st["stage"] = "done"
        return msg_exit(name)

    # Restart / greeting — only if not mid-flow
    if low in GREETING_WORDS and stage in ("new", "done", "enrolled", "goal_selection"):
        st["stage"] = "goal_selection"
        return msg_welcome(name)

    # ── 2b. OBJECTION DETECTION — runs before course keywords ──
    objection = detect_objection(low)
    if objection:
        return handle_objection(objection, name, st)

    # Demo booking trigger
    if low in {"demo", "free demo", "free class", "book demo"}:
        st["stage"] = "demo_time_ask"
        return msg_demo_time_ask()

    # ── ENROLL NOW button (from FEES / AFTER_BOOKING preset) ──
    # Sends direct payment link for selected course — never shows offer menu
    if low in {"enroll_now", "enrol_now", "pay_now"}:
        if course and course in COURSE_PAYMENT_LINKS:
            code, full_name, price, dur, link = COURSE_PAYMENT_LINKS[course]
            st["stage"] = "payment_pending"
            st["offer_course"] = code
            return msg_payment_link(code, full_name, price, dur, link)
        elif course:
            # Course selected but no payment link available yet
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

    # ── Explicit OFFER MENU — only for deliberate offer/discount intent ──
    if low in {"offer", "today offer", "offer undo", "discount"} or \
       ("offer" in low and "discount" in low):
        st["stage"] = "offer_menu"
        return msg_offer_menu()

    # ── Generic payment trigger (typed keywords) ──
    if low in {"pay", "payment", "enrol", "enroll", "seat", "fees pay", "reserve seat"}:
        if course and course in COURSE_PAYMENT_LINKS:
            code, full_name, price, dur, link = COURSE_PAYMENT_LINKS[course]
            st["stage"] = "payment_pending"
            st["offer_course"] = code
            return msg_payment_link(code, full_name, price, dur, link)
        # No course selected — show offer menu as fallback
        st["stage"] = "offer_menu"
        return msg_offer_menu()

    # Fees
    if low in {"fees", "fee", "price", "cost", "ethra", "how much"}:
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
        # Full fee table — add EMI note at end
        return (
            FULL_FEE_TABLE + "\n\nExact course select cheythal EMI/monthly idea paranjutharam."
        ), "FEES"

    # Courses list
    if low in {"courses", "course", "list", "all courses", "padikkaan", "study"}:
        # Anti-loop: if already in goal_selection, nudge instead of repeating
        if stage == "goal_selection":
            return (
                f"😊 {name}, oru number reply cheyyoo!\n\n"
                "1️⃣ Job | 2️⃣ Business | 3️⃣ Basic\n"
                "4️⃣ Accounting | 5️⃣ Not sure\n\n"
                "Ningalude goal-ku best course recommend cheyyam! 🎓"
            ), "GOAL"
        st["stage"] = "goal_selection"
        return msg_welcome(name)

    # Visit
    if any(w in low for w in VISIT_WORDS):
        threading.Thread(
            target=update_lead_status, args=(phone, "Office Visit Interested")
        ).start()
        return msg_visit()

    # Call / handoff
    if any(w in low for w in CALL_WORDS):
        threading.Thread(
            target=update_lead_status, args=(phone, "Call Requested")
        ).start()
        return msg_call_us(name)

    # Certificate
    if "certificate" in low or "certific" in low:
        text = (
            "🏆 *Government Certified Certificate*\n\n"
            "✅ Kerala State Rutronix Approved\n"
            "✅ Valid for job applications\n"
            "✅ Accepted for higher studies\n\n"
            "Real government-backed certification! 💪"
        )
        return text, "COURSE"

    # Placement / job
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
        return text, "COURSE"

    # Batch timings
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

    # ── 3. STAGE-BASED FLOWS ──────────────────────────

    # Goal selection (1-5 buttons)
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

        # Unrecognised input while in goal_selection — gentle nudge
        if low.isdigit():
            return (
                f"Please reply with a number between 1 and 5, {name}! 😊\n\n"
                "1️⃣ Job | 2️⃣ Business | 3️⃣ Basic\n"
                "4️⃣ Accounting | 5️⃣ Not sure"
            ), "GOAL"

    # Course recommendation (numbered choice from goal list)
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
                threading.Thread(
                    target=update_lead_status, args=(phone, f"Viewed: {c_name}")
                ).start()
                return msg_course_detail(c_idx)

    # Demo time selection
    if stage == "demo_time_ask":
        times = {"1": "Morning (9–11 AM)", "2": "Afternoon (12–2 PM)", "3": "Evening (5–7 PM)"}
        if low in times:
            st["batch_time"] = times[low]
            st["stage"] = "demo_date_ask"
            return msg_demo_date_ask(times[low])

    # Demo date input
    if stage == "demo_date_ask":
        date_text = raw
        st["stage"] = "demo_booked"
        bt = st.get("batch_time", "")
        status = f"Demo Booked: {course} | {bt} | {date_text}"
        threading.Thread(target=update_lead_status, args=(phone, status)).start()
        return msg_demo_booked(course, bt, date_text)

    # Offer selection (1-4)
    if stage == "offer_menu":
        if low in OFFER_MENU:
            code, full_name, price, dur, link = OFFER_MENU[low]
            st["offer_course"] = code
            st["stage"] = "payment_pending"
            return msg_payment_link(code, full_name, price, dur, link)

    # Payment confirmation (transaction ID)
    if stage == "payment_pending":
        txn = raw
        offer = st.get("offer_course", "Unknown")
        st["stage"] = "enrolled"
        ts = datetime.now().strftime("[%Y-%m-%d %H:%M]")
        note = f"{ts} Payment: {txn} Course: {offer}"
        threading.Thread(
            target=update_lead_status,
            args=(phone, f"Payment Received: {txn}", note)
        ).start()
        return msg_payment_confirmed(txn, offer, name)

    # ── 4. KEYWORD COURSE SHORTCUTS ───────────────────
    for kw, idx in KEYWORD_TO_COURSE.items():
        if kw in low:
            c_name = ALL_COURSES[idx][0]
            st["course"] = c_name
            st["stage"]  = "course_viewed"
            return msg_course_detail(idx)

    # Direct course number (1-10) from anywhere
    if low in ALL_COURSES:
        c_name = ALL_COURSES[low][0]
        st["course"] = c_name
        st["stage"]  = "course_viewed"
        threading.Thread(
            target=update_lead_status, args=(phone, f"Viewed: {c_name}")
        ).start()
        return msg_course_detail(low)

    # ── 5. GEMINI FALLBACK ────────────────────────────
    ai = gemini_reply(raw, name)
    if ai:
        return ai, "COURSE"

    return smart_fallback(name, raw), "COURSE"


# ═══════════════════════════════════════════════════════
#  WEBHOOK VERIFICATION
# ═══════════════════════════════════════════════════════
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ Webhook verified")
        return challenge, 200
    return "Forbidden", 403


# ═══════════════════════════════════════════════════════
#  WEBHOOK — RECEIVE MESSAGES
# ═══════════════════════════════════════════════════════
@app.route("/webhook", methods=["POST"])
def receive_message():
    data = request.get_json(silent=True) or {}
    try:
        entry   = data["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]

        # Ignore delivery / read receipts
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

        # Parse message text based on type
        if msg_type == "text":
            msg_text = message["text"]["body"].strip()
        elif msg_type == "interactive":
            itype = message["interactive"]["type"]
            if itype == "button_reply":
                msg_text = message["interactive"]["button_reply"]["id"]
            elif itype == "list_reply":
                msg_text = message["interactive"]["list_reply"]["id"]
            else:
                msg_text = f"[interactive_{itype}]"
        elif msg_type == "button":
            msg_text = message["button"]["text"]
        else:
            # Unsupported type — ignore silently
            return jsonify({"status": "ok"}), 200

        print(f"📱 {contact_name} ({from_number}): {msg_text}")

        is_new_lead = from_number not in conversation_state

        # ── CRM save (background) ──
        threading.Thread(
            target=save_lead_to_sheets,
            args=(from_number, contact_name, msg_text, is_new_lead),
        ).start()

        # ── Generate reply ──
        reply_text, preset = smart_reply(msg_text, contact_name, from_number, is_new_lead)
        send_reply(from_number, reply_text, preset)

        # ── Schedule follow-ups for new leads ──
        if is_new_lead:
            schedule_followups(from_number, contact_name)

    except Exception as e:
        print(f"❌ Webhook error: {e}")

    return jsonify({"status": "ok"}), 200


# ═══════════════════════════════════════════════════════
#  GOOGLE SHEETS CRM
# ═══════════════════════════════════════════════════════
def _get_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_json = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEETS_ID)


def save_lead_to_sheets(phone: str, name: str, message: str, is_new: bool):
    try:
        if not SHEETS_ID or GOOGLE_CREDENTIALS_JSON == "{}":
            return
        wb  = _get_sheet()
        titles = [s.title for s in wb.worksheets()]
        ws  = wb.worksheet("Leads") if "Leads" in titles else wb.sheet1

        # Init headers if blank
        if not ws.cell(1, 1).value:
            ws.update("A1:G1", [
                ["Timestamp", "Name", "Phone", "Last Message", "Status", "Source", "Notes"]
            ])

        ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        note  = f"[{ts}] {message}"

        if is_new:
            ws.append_row([ts, name, phone, message, "New Lead", "WhatsApp", note])
            print(f"✅ CRM: new lead saved — {name}")
        else:
            phones = ws.col_values(3)
            if phone in phones:
                row = phones.index(phone) + 1
                ws.update_cell(row, 1, ts)
                ws.update_cell(row, 4, message)
                existing = ws.cell(row, 7).value or ""
                ws.update_cell(row, 7, f"{existing}\n{note}" if existing else note)
                print(f"✅ CRM: lead updated — {name}")
    except Exception as e:
        print(f"⚠️  Sheets save error: {e}")


def update_lead_status(phone: str, status: str, append_note: str = ""):
    try:
        if not SHEETS_ID or GOOGLE_CREDENTIALS_JSON == "{}":
            return
        wb  = _get_sheet()
        titles = [s.title for s in wb.worksheets()]
        ws  = wb.worksheet("Leads") if "Leads" in titles else wb.sheet1
        phones = ws.col_values(3)
        if phone in phones:
            row = phones.index(phone) + 1
            ws.update_cell(row, 5, status)
            if append_note:
                existing = ws.cell(row, 7).value or ""
                ws.update_cell(row, 7, f"{existing}\n{append_note}" if existing else append_note)
            print(f"✅ CRM: status → {status} ({phone})")
    except Exception as e:
        print(f"⚠️  Sheets status error: {e}")


# ═══════════════════════════════════════════════════════
#  MULTI-DAY FOLLOW-UP SCHEDULER
# ═══════════════════════════════════════════════════════
FOLLOWUP_TEMPLATES = [
    {
        "day": 1,
        "hours": 24,
        "message": (
            "Hi {name} 😊 Aaliza here from The Oxford Computers.\n\n"
            "Course about alochichu nokkiyo?\n"
            "Confusion undenkil njan help cheyyam.\n\n"
            "Oru free demo class attend cheythal clarity varum 🎓\n"
            "*DEMO* reply cheythal book cheyyam."
        ),
    },
    {
        "day": 3,
        "hours": 72,
        "message": (
            "{name}, small reminder 😊\n\n"
            "Next batch starting soon aanu.\n"
            "Late aayal next batch wait cheyyendi varum.\n\n"
            "Ningalkku job-oriented course venel njan best option suggest cheyyam.\n"
            "*COURSES* / *DEMO* reply cheyyoo."
        ),
    },
    {
        "day": 7,
        "hours": 168,
        "message": (
            "{name}, last follow-up aanu 😊\n\n"
            "This batch-il free demo + EMI option available aanu.\n"
            "Seat limited aanu.\n\n"
            "Interested aanenkil *DEMO* or *VISIT* reply cheyyoo.\n"
            "All the best from The Oxford Computers 🎓"
        ),
    },
]


def schedule_followups(phone: str, name: str):
    now = datetime.now()
    for tmpl in FOLLOWUP_TEMPLATES:
        follow_up_queue.append({
            "phone":   phone,
            "name":    name,
            "send_at": now + timedelta(hours=tmpl["hours"]),
            "message": tmpl["message"].format(name=name),
            "day":     tmpl["day"],
            "done":    False,
        })
    print(f"📅 Follow-ups scheduled for {name}")


def _followup_worker():
    while True:
        try:
            now = datetime.now()
            for item in follow_up_queue:
                if item["done"] or now < item["send_at"]:
                    continue
                # Skip if lead was active in the last 6 hours
                st = conversation_state.get(item["phone"], {})
                last = st.get("last_msg", "")
                if last:
                    delta = (now - datetime.fromisoformat(last)).total_seconds()
                    if delta < 21_600:
                        item["done"] = True
                        print(f"⏭️  Follow-up skipped — {item['name']} recently active")
                        continue
                send_text(item["phone"], item["message"])
                threading.Thread(
                    target=update_lead_status,
                    args=(item["phone"], f"Follow-up Day {item['day']} Sent"),
                ).start()
                item["done"] = True
                print(f"📤 Follow-up Day {item['day']} → {item['name']}")
        except Exception as e:
            print(f"⚠️  Follow-up worker error: {e}")
        time.sleep(300)


threading.Thread(target=_followup_worker, daemon=True).start()
print("✅ Follow-up scheduler started")


# ═══════════════════════════════════════════════════════
#  BROADCAST — plain text
#  POST /broadcast  |  Header: X-API-Key
#  Body: { "numbers": [...], "message": "...", "delay_seconds": 2 }
# ═══════════════════════════════════════════════════════
@app.route("/broadcast", methods=["POST"])
def broadcast():
    if request.headers.get("X-API-Key") != BROADCAST_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    body    = request.get_json(silent=True) or {}
    numbers = body.get("numbers", [])
    message = body.get("message", "")
    delay   = body.get("delay_seconds", 2)

    if not numbers or not message:
        return jsonify({"error": "numbers and message are required"}), 400

    results = []
    for num in numbers:
        num = str(num).strip()
        if not num.startswith("91"):
            num = "91" + num.lstrip("0")
        r = send_text(num, message)
        results.append({"number": num, "status": r.status_code, "ok": r.status_code == 200})
        time.sleep(delay)

    ok = sum(1 for x in results if x["ok"])
    return jsonify({"total": len(numbers), "success": ok,
                    "failed": len(numbers) - ok, "results": results})


# ═══════════════════════════════════════════════════════
#  BROADCAST — template
#  POST /broadcast-template  |  Header: X-API-Key
#  Body: { "numbers": [...], "template_name": "...",
#          "language": "en", "variables": ["{name}"],
#          "delay_seconds": 2 }
# ═══════════════════════════════════════════════════════
@app.route("/broadcast-template", methods=["POST"])
def broadcast_template():
    if request.headers.get("X-API-Key") != BROADCAST_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    body          = request.get_json(silent=True) or {}
    numbers       = body.get("numbers", [])
    template_name = body.get("template_name", "")
    language      = body.get("language", "en")
    variables     = body.get("variables", [])
    delay         = body.get("delay_seconds", 2)

    if not numbers or not template_name:
        return jsonify({"error": "numbers and template_name are required"}), 400

    results = []
    for item in numbers:
        if isinstance(item, dict):
            num  = str(item.get("phone", "")).strip()
            name = str(item.get("name", ""))
        else:
            num  = str(item).strip()
            name = ""

        if not num.startswith("91"):
            num = "91" + num.lstrip("0")

        resolved = [name if v == "{name}" else str(v) for v in variables]
        components = []
        if resolved:
            components = [{"type": "body",
                           "parameters": [{"type": "text", "text": v} for v in resolved]}]

        r = send_template(num, template_name, language, components or None)
        results.append({"number": num, "status": r.status_code, "ok": r.status_code == 200})
        time.sleep(delay)

    ok = sum(1 for x in results if x["ok"])
    return jsonify({"total": len(numbers), "success": ok,
                    "failed": len(numbers) - ok, "results": results})


# ═══════════════════════════════════════════════════════
#  MANUAL FOLLOW-UP TRIGGER
#  POST /trigger-followup  |  Header: X-Admin-Key
#  Body: { "phone": "919...", "name": "...", "message": "..." }
# ═══════════════════════════════════════════════════════
@app.route("/trigger-followup", methods=["POST"])
def trigger_followup():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    body    = request.get_json(silent=True) or {}
    phone   = body.get("phone", "")
    message = body.get("message", "")

    if not phone or not message:
        return jsonify({"error": "phone and message are required"}), 400

    if not phone.startswith("91"):
        phone = "91" + phone.lstrip("0")

    r = send_text(phone, message)
    return jsonify({"ok": r.status_code == 200, "status": r.status_code, "phone": phone})


# ═══════════════════════════════════════════════════════
#  ADMIN STATS
#  GET /stats  |  Header: X-Admin-Key
# ═══════════════════════════════════════════════════════
@app.route("/stats", methods=["GET"])
def stats():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    return jsonify({
        "total_leads":       len(conversation_state),
        "pending_followups": sum(1 for f in follow_up_queue if not f["done"]),
        "stage_breakdown": {
            s: sum(1 for v in conversation_state.values() if v.get("stage") == s)
            for s in {"new", "goal_selection", "course_recommendation", "course_viewed",
                      "demo_time_ask", "demo_date_ask", "demo_booked",
                      "offer_menu", "payment_pending", "enrolled", "not_sure", "done"}
        },
        "active_conversations": [
            {
                "name":        v["name"],
                "stage":       v.get("stage", ""),
                "last_text":   v.get("last_text", ""),
                "last_active": v.get("last_msg", ""),
                "course":      v.get("course", ""),
            }
            for v in conversation_state.values()
        ],
    })


# ═══════════════════════════════════════════════════════
#  ADMIN PANEL
#  GET /panel?key=<ADMIN_KEY>
# ═══════════════════════════════════════════════════════
@app.route("/panel", methods=["GET"])
def admin_panel():
    if request.args.get("key", "") != ADMIN_KEY:
        return (
            "<html><body style='font-family:sans-serif;text-align:center;"
            "padding:50px;background:#0a0f0d;color:#25D366'>"
            "<h2>🔒 Access Denied</h2>"
            "<p style='color:#888'>URL-il ?key=YOUR_ADMIN_KEY add cheyyuka</p>"
            "</body></html>"
        ), 403
    try:
        with open("panel.html", "r", encoding="utf-8") as f:
            return f.read(), 200, {"Content-Type": "text/html"}
    except FileNotFoundError:
        return "panel.html not found in project root", 404


# ═══════════════════════════════════════════════════════
#  HEALTH CHECK
#  GET /
# ═══════════════════════════════════════════════════════
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status":            "running ✅",
        "app":               "Oxford Computers WhatsApp AI System v3.0",
        "sdk":               "google-genai (gemini-2.0-flash)",
        "leads_in_memory":   len(conversation_state),
        "pending_followups": sum(1 for f in follow_up_queue if not f["done"]),
        "gemini_active":     gemini_client is not None,
        "sheets_configured": bool(SHEETS_ID and GOOGLE_CREDENTIALS_JSON != "{}"),
        "features": [
            "Stage-based conversation state machine",
            "Google Sheets CRM",
            "Gemini 2.0 Flash AI (humanised Manglish)",
            "Interactive WhatsApp buttons (named presets)",
            "Broadcast API",
            "Template Broadcast API",
            "Multi-day Follow-up Scheduler",
            "Admin Stats + Manual Trigger",
        ],
    })


# ═══════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)