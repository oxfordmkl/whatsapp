"""
log_service.py — Phase 4D
Lightweight message logging helper.
Safe to call from webhook, scheduler, and CRM routes.
Never raises — all errors are logged internally.
"""
import logging
from datetime import datetime

_MAX_TEXT = 5000   # Prevent oversized DB rows and abuse payloads


def log_message(
    phone: str,
    direction: str,
    message_type: str,
    message_text: str,
    meta_json: str = None,
) -> None:
    """
    Append one message event to the message_log table.

    Args:
        phone:        WhatsApp phone number
        direction:    "inbound" or "outbound"
        message_type: "user", "ai", "followup", or "manual"
        message_text: raw message body (truncated to 5000 chars)
        meta_json:    optional JSON string for extra metadata
    """
    try:
        from app.models import MessageLog
        from app.extensions import db

        entry = MessageLog(
            phone=phone,
            direction=direction,
            message_type=message_type,
            message_text=(message_text or "")[:_MAX_TEXT],
            meta_json=meta_json,
            created_at=datetime.utcnow(),
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        logging.exception(
            f"[log_service] Failed to log {direction}/{message_type} for {phone}"
        )
