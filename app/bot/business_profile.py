"""
Phase 1.6.4 — Canonical BUSINESS PROFILE.

THE single source of truth for who the institute is and how to reach it. Every
other module (constants, screens, replies, prompts) must reference these values
rather than restating them, so a change here propagates everywhere and a detail
can never drift between two files.

Pure data, stdlib only — no imports, no app dependency — so it is safe to import
from anywhere (including at module scope) and trivially testable in isolation.

Scope note: this module holds *identity and contact* facts. Course catalogue,
fees and recognition wording remain in app/bot/constants.py.
"""

# ── Identity ─────────────────────────────────────────────────────────────────
INSTITUTE_NAME = "The Oxford Computers"

# ── Location ─────────────────────────────────────────────────────────────────
ADDRESS = "Krishna Building,Near Malayinkeezhu Sreekrishna Swami Temple, Main road Malayinkeezhu, Thiruvananthapuram, Kerala"
# The ONLY place this link may be written. Never duplicate it in another file.
MAPS_URL = "https://maps.app.goo.gl/SRCPnmchN6maeQX59"

# ── Contact ──────────────────────────────────────────────────────────────────
PHONE = "9447329972"
# The institute runs its WhatsApp Business line on the same number as the phone;
# kept as a separate name so the two can diverge without touching call sites.
WHATSAPP = PHONE
EMAIL = "info@theoxfordedu.com"
WEBSITE = "theoxfordedu.com"

# Short locality / city labels, for lines where the full ADDRESS is too long.
LOCALITY = "Malayinkeezhu"
CITY = "Thiruvananthapuram"

# ── Availability ─────────────────────────────────────────────────────────────
# Office (walk-in) hours. Class batch timings are a separate concern and live
# with the course/timing replies, not here.
OFFICE_HOURS = "9 AM – 5 PM (Mon–Sat)"
# Counsellor phone availability — wider than walk-in office hours.
COUNSELLOR_HOURS = "9 AM – 7 PM (Mon–Sat)"


# One reusable mapping for any surface that needs the whole profile.
BUSINESS_PROFILE = {
    "name":             INSTITUTE_NAME,
    "address":          ADDRESS,
    "locality":         LOCALITY,
    "city":             CITY,
    "maps_url":         MAPS_URL,
    "phone":            PHONE,
    "whatsapp":         WHATSAPP,
    "email":            EMAIL,
    "website":          WEBSITE,
    "office_hours":     OFFICE_HOURS,
    "counsellor_hours": COUNSELLOR_HOURS,
}
