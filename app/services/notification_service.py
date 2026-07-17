"""Phase 16.5A7 — Notification service (ADR-021).

Single funnel for creating and reading in-app notifications. Routes must not
construct Notification rows directly; CODE_CONVENTIONS §3 keeps business logic
out of app/routes/*.

Tenant safety (ACTIVE_CONSTRAINTS §2): every function here takes an explicit
tenant_id and every query filters on it. `notify()` REFUSES to write without
one rather than falling back to a default tenant — Phase 16.5A7 discovery found
that `_get_default_tenant_id()` (Tenant.query.first()) resolves to an unrelated
tenant in production and silently mis-files rows. See ADR-021.

Delivery is in-app only. No email or WhatsApp fan-out in this phase.
"""

import logging

from datetime import datetime

from app.extensions import db
from app.models import Notification

logger = logging.getLogger(__name__)


class NotificationError(Exception):
    """Raised when a notification cannot be created safely."""


def notify(tenant_id, recipient, notif_type, title, body=None,
           lead_phone=None, task_id=None, commit=True):
    """Create one notification. Returns the row, or None when suppressed.

    Suppressed (returns None, no row) when:
      * recipient is empty or 'Unassigned' — nobody to notify.

    Raises NotificationError when:
      * tenant_id is missing — writing to a guessed tenant is a cross-tenant
        leak, so this fails loudly instead (ADR-021).
      * notif_type is not one of Notification.VALID_TYPES.
    """
    if not tenant_id:
        raise NotificationError(
            "notify() requires an explicit tenant_id — refusing to guess "
            "(ACTIVE_CONSTRAINTS §2, ADR-021)")

    if notif_type not in Notification.VALID_TYPES:
        raise NotificationError(
            f"unknown notif_type {notif_type!r}; "
            f"expected one of {Notification.VALID_TYPES}")

    clean = (recipient or "").strip()
    if not clean or clean == "Unassigned":
        logger.debug("notification suppressed: no recipient (type=%s tenant=%s)",
                     notif_type, tenant_id)
        return None

    row = Notification(
        tenant_id=tenant_id,
        recipient=clean,
        notif_type=notif_type,
        title=(title or "")[:200],
        body=(body[:500] if body else None),
        lead_phone=lead_phone,
        task_id=task_id,
        is_read=False,
    )
    db.session.add(row)
    if commit:
        db.session.commit()
    logger.info("notification created type=%s recipient=%s tenant=%s",
                notif_type, clean, tenant_id)
    return row


def unread_count(tenant_id, recipient):
    """Unread badge count. Backed by idx_notif_recipient_unread."""
    clean = (recipient or "").strip()
    if not tenant_id or not clean:
        return 0
    return (Notification.query
            .filter_by(tenant_id=tenant_id, recipient=clean, is_read=False)
            .count())


def recent(tenant_id, recipient, limit=10, unread_only=False):
    """Most recent notifications for the bell dropdown / notification centre."""
    clean = (recipient or "").strip()
    if not tenant_id or not clean:
        return []
    q = Notification.query.filter_by(tenant_id=tenant_id, recipient=clean)
    if unread_only:
        q = q.filter_by(is_read=False)
    return q.order_by(Notification.created_at.desc(),
                      Notification.id.desc()).limit(limit).all()


def mark_read(tenant_id, recipient, notification_id):
    """Mark one notification read. Tenant- AND recipient-scoped so a caller
    cannot flip another tenant's (or another user's) row by guessing an id."""
    clean = (recipient or "").strip()
    if not tenant_id or not clean:
        return False
    row = (Notification.query
           .filter_by(id=notification_id, tenant_id=tenant_id, recipient=clean)
           .first())
    if row is None:
        return False
    if not row.is_read:
        row.is_read = True
        row.read_at = datetime.utcnow()
        db.session.commit()
    return True


def mark_all_read(tenant_id, recipient):
    """Mark every unread notification for this recipient read. Returns count."""
    clean = (recipient or "").strip()
    if not tenant_id or not clean:
        return 0
    rows = (Notification.query
            .filter_by(tenant_id=tenant_id, recipient=clean, is_read=False)
            .all())
    now = datetime.utcnow()
    for r in rows:
        r.is_read = True
        r.read_at = now
    if rows:
        db.session.commit()
    return len(rows)
