from flask import Blueprint, jsonify
from app.config import SHEETS_ID, GOOGLE_CREDENTIALS_JSON
from app.services.ai_service import gemini_client
from app.state import count_states, count_pending_followups

health_bp = Blueprint("health", __name__)


@health_bp.route("/", methods=["GET"])
def health():
    return jsonify({
        "status":            "running ✅",
        "app":               "Oxford Computers WhatsApp AI System v3.0",
        "sdk":               "google-genai (gemini-2.0-flash)",
        "leads_in_memory":   count_states(),
        "pending_followups": count_pending_followups(),
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
            "PostgreSQL-backed persistent state",
        ],
    })
