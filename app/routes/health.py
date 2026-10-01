import logging
from flask import Blueprint, current_app, jsonify
from sqlalchemy import text

from app.services.ai_service import gemini_client
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

    # Phase 2B: public and unauthenticated, so operational health ONLY.
    # Removed: the platform-wide lead and pending-follow-up counts (every
    # tenant's business volume), the feature inventory, the AI model name and
    # the Sheets configuration state. `gemini_active` stays because the legacy
    # broadcast panel reads it; Railway's health check needs only the 200.
    return jsonify({
        "status":         "running",
        "database":       db_status,
        "scheduler":      scheduler_status,
        "whatsapp_token": whatsapp_status,
        "app":            current_app.config.get("PLATFORM_NAME", "Xasnic"),
        "gemini_active":  gemini_client is not None,
    })
