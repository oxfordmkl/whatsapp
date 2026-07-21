import logging
from flask import Blueprint, jsonify
from sqlalchemy import text

from app.config import SHEETS_ID, GOOGLE_CREDENTIALS_JSON, GEMINI_MODEL
from app.services.ai_service import gemini_client
from app.state import count_states, count_pending_followups
from app.extensions import db

health_bp = Blueprint("health", __name__)


# Phase 16.5B0: "/" is registered by BOTH public_bp (index) and health_bp.
# public_bp is registered first (app/__init__.py:143), so Flask resolves GET /
# to public.index and this JSON endpoint became unreachable — the Broadcast
# Panel's Test button fetched "/", received the HTML landing page, and
# JSON.parse failed with "Unexpected token '<'", showing OFFLINE.
# "/health" gives the SAME response a reachable path. The "/" rule is left in
# place so nothing that still points at it changes behaviour.
@health_bp.route("/", methods=["GET"])
@health_bp.route("/health", methods=["GET"])
def health():
    # 1. Database check (Lightweight query)
    db_status = "disconnected"
    try:
        db.session.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        logging.error(f"Database health check failed: {e}")

    # 2. WhatsApp Token check (Cached from startup thread)
    whatsapp_status = "unknown"
    try:
        from app.services.whatsapp_service import token_status
        whatsapp_status = token_status
    except ImportError:
        pass
    except Exception as e:
        logging.error(f"WhatsApp token check failed: {e}")

    # 3. Scheduler check (Cached flag)
    scheduler_status = "stopped"
    try:
        from app.services.followup_service import scheduler_started
        if scheduler_started:
            scheduler_status = "running"
    except ImportError:
        pass
    except Exception as e:
        logging.error(f"Scheduler health check failed: {e}")

    return jsonify({
        "status":            "running",
        "database":          db_status,
        "scheduler":         scheduler_status,
        "whatsapp_token":    whatsapp_status,
        "app":               "Oxford Computers WhatsApp AI System v3.0",
        "sdk":               f"google-genai ({GEMINI_MODEL})",
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
