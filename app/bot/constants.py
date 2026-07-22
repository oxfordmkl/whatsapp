import random

def pick(items: list) -> str:
    """Return a random item from the list."""
    return random.choice(items)


# ══════════════════════════════════════════════════════════════════════════════
# CANONICAL BUSINESS FACTS — single source of truth.
# Every reply and AI prompt must agree with these values.
# Change here; all downstream strings inherit the change.
# ══════════════════════════════════════════════════════════════════════════════
# Phase 1.6.4: institute identity/contact facts now come from the canonical
# Business Profile — this file must NOT restate them. The aliases below are kept
# so every existing import site (router, prompts, replies) keeps working.
from app.bot.business_profile import BUSINESS_PROFILE, MAPS_URL as _MAPS_URL

INST_NAME       = BUSINESS_PROFILE["name"]
INST_LOCATION   = BUSINESS_PROFILE["address"]
INST_PHONE      = BUSINESS_PROFILE["phone"]
INST_WEBSITE    = BUSINESS_PROFILE["website"]
INST_MAPS_URL   = _MAPS_URL

# Backward-compatible view; the Business Profile is the authority.
INSTITUTE = BUSINESS_PROFILE

# Natural-language triggers that a LATER phase will map to the location reply.
# Declared here as configuration only — the router does NOT read this yet.
# NOTE: router.VISIT_WORDS already routes some of these ("location", "address",
# "map", "route") to msg_visit(); Phase 6.4+ reconciles the two so both consume
# INSTITUTE instead of hardcoded text.
LOCATION_KEYWORDS = (
    "location", "map", "google map", "address", "route",
    "evide", "institute evide", "location ayakku",
)

# Recognition — use the short label in course cards; full label in intros/cert replies.
RUTRONIX_LABEL  = "Kerala State Rutronix Approved"
RUTRONIX_FULL   = "Kerala State Rutronix Authorised Training Centre"

# Government / eligibility (always quote exactly — never invent eligibility).
PSC_NOTE        = "Eligible 6-month & 12-month govt-approved courses are PSC eligible"
NORKA_NOTE      = "NORKA Attestation available for eligible certificates"

# Learning delivery
LEARNING_MODES  = "Offline Classes | Online Live Classes | Fast Track available"

# Technology
AI_NOTE         = "All courses are AI-enabled — AI tools integrated throughout"
# ══════════════════════════════════════════════════════════════════════════════


# ── Phase 7F.2: Course name normalization ──────────────────────────────────
#
# Maps every known alias (lowercase key) to the canonical course name that
# appears in ALL_COURSES.  Applied at READ TIME only — no DB writes ever.
#
# Sources of aliases (from audit):
#   • ConversationState.offer_course  — short codes from OFFER_MENU
#   • GOAL_COURSES display labels     — longer variants used in bot text
#   • FULL_FEE_TABLE display labels   — abbreviated WhatsApp message strings
#
COURSE_NAME_ALIASES: dict = {
    # ── offer_course short codes (written by router.py from OFFER_MENU) ──
    "aidm":  "AIDM Digital Marketing",
    "dca":   "DCA Fast Track",
    "cwpde": "Word Processing & Data Entry",
    # "pgdca" already matches canonical — included for completeness
    "pgdca": "PGDCA",

    # ── GOAL_COURSES display label variants ───────────────────────────────
    "ai-driven digital marketing": "AIDM Digital Marketing",
    "web designing":               "Professional Web Designing",
    "gst & payroll diploma":       "GST & Payroll",
    "pgdca — post graduate diploma": "PGDCA",

    # ── FULL_FEE_TABLE abbreviated display labels ─────────────────────────
    "sap accounting":          "SAP Financial Accounting",
    "computer teaching":       "Computer Teacher Training",
    "business accounting":     "Corporate Business Accounting",
    "word processing":         "Word Processing & Data Entry",
}


def normalize_course_name(raw: str) -> str:
    """
    Map any known course alias to its canonical ALL_COURSES name.

    Rules:
    - Case-insensitive lookup.
    - Leading/trailing whitespace stripped before lookup.
    - Returns the original value (stripped) if no alias mapping exists.
    - Never raises — all errors return the original value unchanged.

    Phase 7F.2: read-time normalization only.  Zero DB writes.
    """
    try:
        if not raw:
            return raw
        stripped = raw.strip()
        return COURSE_NAME_ALIASES.get(stripped.lower(), stripped)
    except Exception:
        return raw


_PGDCA = (
    "📚 *PGDCA — Post Graduate Diploma in Computer Applications*\n"
    f"⏱ 12 Months | 🎓 {RUTRONIX_LABEL}\n"
    "📋  C++/ Java, PYTHON, DBMS, Web Dev, Networks, Mobile App, Final Project\n"
    "💡 Best for graduates seeking an Govt+IT career\n"
    "💰 Fee: ₹15,999"
)
_AIDM = (
    "📚 *AIDM — AI-Driven Digital Marketing*\n"
    f"⏱ 6 Months | 🎓 {RUTRONIX_LABEL}\n"
    "📋 SEO, Social Media, Google Ads, Meta Ads, ChatGPT/AI Tools, Live Campaigns\n"
    "💡 Best for marketers, entrepreneurs, beginners\n"
    "💰 Fee: ₹19,999"
)
_SAP = (
    "📚 *SAP Financial Accounting & Controlling*\n"
    f"⏱ 6 Months | 🎓 SAP Alliance + {RUTRONIX_LABEL}\n"
    "📋 GL Accounting, AP/AR, Asset Accounting, SAP CO, Real-Time Project\n"
    "💡 Best for commerce graduates & accounting professionals\n"
    "💰 Fee: ₹15,000"
)
_PYTHON = (
    "📚 *Python — Beginner to Advanced*\n"
    f"⏱ 3 Months | 🎓 {RUTRONIX_LABEL}\n"
    "📋 OOP, File Handling, Flask Basics, Pandas, Web Scraping, Automation\n"
    "💡 Best for beginners and IT aspirants\n"
    "💰 Fee: ₹4,499"
)
_GST = (
    "📚 *Diploma in GST, Taxation & Payroll*\n"
    f"⏱ 6 Months | 🎓 {RUTRONIX_LABEL}\n"
    "📋 GST Concepts, Income Tax, Tally Prime, Payroll Processing, E-filing\n"
    "💡 Best for accounting professionals & commerce students\n"
    "💰 Fee: ₹18,999"
)
_DCA = (
    "📚 *DCA — Diploma in Computer Applications (Fast Track)*\n"
    f"⏱ 6 Months | 🎓 {RUTRONIX_LABEL}\n"
    "📋 Computer Fundamentals, MS Office, Programming Basics, Internet, Database, DTP\n"
    "💡 Best for students and office job seekers\n"
    "💰 Fee: ₹6,400"
)
_TEACHER = (
    "📚 *Computer Teacher Training Course*\n"
    f"⏱ 12 Months | 🎓 {RUTRONIX_LABEL}\n"
    "📋 Teaching Methodology, MS Office Pedagogy, Programming Basics, Practice Teaching\n"
    "💡 Best for aspiring computer teachers\n"
    "💰 Fee: ₹11,999"
)
_ACCOUNTING = (
    "📚 *Diploma in Corporate Business Accounting & Taxation*\n"
    f"⏱ 12 Months | 🎓 {RUTRONIX_LABEL}\n"
    "📋 Corporate Accounting, GST, Income Tax Corporate, Financial Modelling, Case Studies\n"
    "💡 Best for advanced accounting and finance careers\n"
    "💰 Fee: ₹40,000"
)
_WORD = (
    "📚 *Certificate in Word Processing & Data Entry*\n"
    f"⏱ 6 Months | 🎓 {RUTRONIX_LABEL}\n"
    "📋 Touch Typing, MS Word, Data Entry Techniques, DTP Basics, Document Management\n"
    "💡 Best for data entry professionals and beginners\n"
    "💰 Fee: ₹4,800"
)
_WEB = (
    "📚 *Professional Diploma in Web Designing*\n"
    f"⏱ 6 Months | 🎓 {RUTRONIX_LABEL}\n"
    "📋 HTML5, CSS3, JavaScript, jQuery, PHP & MySQL, WordPress, Portfolio Project\n"
    "💡 Best for aspiring web developers and designers\n"
    "💰 Fee: ₹8,800"
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
    "SAP Financial Accounting":      ("₹15,000", "6 Months"),
    "Python Programming":            ("₹4,499",  "3 Months"),
    "GST & Payroll":                 ("₹18,999",  "6 Months"),
    "DCA Fast Track":                ("₹6,400",  "6 Months"),
    "Computer Teacher Training":     ("₹11,999",  "1 Year"),
    "Corporate Business Accounting": ("₹40,000", "1 Year"),
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
        ("3",  "SAP Financial Accounting",         "6 Months",  "₹15,000"),
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
    "3️⃣  SAP Accounting         ₹15,000  (6M)\n"
    "4️⃣  Python Programming     ₹4,499   (3M)\n"
    "5️⃣  GST & Payroll          ₹18,999  (6M)\n"
    "6️⃣  DCA Fast Track         ₹6,400   (6M)\n"
    "7️⃣  Computer Teaching      ₹11,999   (1Y)\n"
    "8️⃣  Business Accounting    ₹40,000  (1Y)\n"
    "9️⃣  Word Processing        ₹4,800   (6M)\n"
    "🔟 Web Designing           ₹8,800   (6M)\n"
    "━━━━━━━━━━━━━━━━\n"
    f"🎓 {RUTRONIX_FULL}\n"
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
