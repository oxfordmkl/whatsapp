"""
Phase 8.2C.2 — Campaign Worker execution loop (UNWIRED).

Clones the proven FollowUpJob polling architecture and adds the approved
campaign execution contract:

    pending_tenant_ids()
      → claim_next_batch()
        → COMMIT claim
          → send_automation()
            → persist outcome (sent / retry / failed)
              → COMMIT outcome
                → check campaign completion

Nothing in this module is started or imported by production code. Startup
registration is Phase 8.2C.3. CAMPAIGN_ENGINE_V2 remains OFF in production.

Layering: CampaignWorker calls CampaignRepository and send_automation() only.
Campaign-level completion (running→completed) goes directly through the
repository because the worker, not CampaignService, owns the transaction
boundary during batch processing. CampaignService.transition() performs its
own commit, which would interleave with the batch commits and break the
approved execution order.

Retry policy (worker-owned, not repository-owned):
  attempt < MAX_RETRIES  →  schedule_recipient_retry() with 15*attempt min backoff
  attempt >= MAX_RETRIES →  mark_recipient_failed()  (terminal)

attempt is computed as (row.retry_count or 0) + 1, matching FollowUpJob's
retry_count >= 3 → done pattern exactly.

Reclaim policy (worker-owned):
  Rows stuck in `sending` for > STALE_MINUTES are moved back to `queued`.
  increment_retry=False: delivery outcome is unknown — the prior send may have
  succeeded before the process crashed. Reclaim is recovery, not failure.
"""
import logging
import time
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Poll / sizing constants — match FollowUpJob defaults.
POLL_INTERVAL = 300     # seconds between sweeps (5 minutes)
CLAIM_BATCH = 50        # recipients per tenant per cycle
STALE_MINUTES = 10      # minutes before a `sending` row is considered stuck

# Retry cap — after this many attempts the recipient is marked terminal.
# Matches FollowUpJob: retry_count >= 3 → done.
MAX_RETRIES = 3

# Set by init_campaign_worker(). Not called from anywhere until Phase 8.2C.3.
_app = None


def init_campaign_worker(app):
    """Store the Flask app reference and launch the daemon worker thread.

    Called once from create_app() — the call site is Phase 8.2C.3 and is
    intentionally absent from app/__init__.py until that phase is approved.
    This function exists so the call site can be added without touching this
    module again.
    """
    global _app
    _app = app
    threading.Thread(target=_campaign_worker_loop, daemon=True).start()
    logger.info("✅ Campaign worker started")


# ── Poll loop ─────────────────────────────────────────────────────────────────

def _campaign_worker_loop():
    """Main poll loop. Mirrors _followup_worker() structure exactly."""
    while True:
        try:
            with _app.app_context():
                _run_cycle()
        except Exception as e:
            logger.warning("⚠️  Campaign worker outer error: %s", e)

        time.sleep(POLL_INTERVAL)


def _run_cycle():
    """One sweep: reclaim stale rows, then claim and send due recipients."""
    from app.persistence.campaign_repository import CampaignRepository
    from app.extensions import db

    repo = CampaignRepository()
    now = datetime.utcnow()
    stale_before = now - timedelta(minutes=STALE_MINUTES)

    tenant_ids = repo.pending_tenant_ids(now=now)
    for tenant_id in tenant_ids:
        try:
            _reclaim_stale(repo, db.session, tenant_id, stale_before)
            _process_tenant(repo, db.session, tenant_id, now)
        except Exception as e:
            logger.warning(
                "⚠️  Campaign worker error — tenant=%s: %s", tenant_id, e
            )


# ── Per-tenant work ───────────────────────────────────────────────────────────

def _reclaim_stale(repo, session, tenant_id, stale_before):
    """Return stuck `sending` rows to `queued`. Does not count against retry cap."""
    count = repo.reclaim_stale_recipients(
        tenant_id, stale_before, increment_retry=False
    )
    if count:
        session.commit()
        logger.info(
            "🔄 Reclaimed %d stale recipient(s) for tenant %s", count, tenant_id
        )


def _process_tenant(repo, session, tenant_id, now):
    """Claim a batch for one tenant, commit the claim, then send each recipient."""
    claimed = repo.claim_next_batch(tenant_id, limit=CLAIM_BATCH, now=now)
    if not claimed:
        return

    # ── COMMIT CLAIM before any send ──────────────────────────────────────
    # This is the non-negotiable execution order from Phase 8.2C design.
    # Without a committed claim a crash mid-send leaves rows claimable again,
    # which defeats the double-send protection.
    session.commit()
    logger.info(
        "📦 Claimed %d recipient(s) for tenant %s", len(claimed), tenant_id
    )

    # Group by campaign to fetch each campaign row once per batch.
    by_campaign = {}
    for row in claimed:
        by_campaign.setdefault(row.campaign_id, []).append(row)

    for campaign_id, rows in by_campaign.items():
        campaign = repo.get(tenant_id, campaign_id)
        message_body = (campaign.message_body or "") if campaign else ""

        for row in rows:
            _send_one(repo, session, tenant_id, row, message_body, now)

        # After all recipients in this campaign are processed, check whether
        # the campaign is now fully terminal and can be marked completed.
        _check_campaign_completion(repo, session, tenant_id, campaign_id, now)


# ── Per-recipient send ────────────────────────────────────────────────────────

def _send_one(repo, session, tenant_id, row, message_body, now):
    """Send one recipient and persist the outcome. All exceptions isolated."""
    from app.models import ConversationState
    from app.services.whatsapp_service import send_automation

    try:
        # Opt-out check — mirrors FollowUpJob Phase 11-D1 Task D exactly.
        state_row = ConversationState.query.filter_by(
            phone=row.phone, tenant_id=tenant_id
        ).first()
        if state_row and getattr(state_row, "is_opted_out", False):
            repo.mark_recipient_failed(
                tenant_id, row.id,
                failure_reason="opted out",
                attempted_at=now,
            )
            session.commit()
            logger.warning(
                "🚫 Campaign send skipped — %s opted out (campaign=%s)",
                row.phone, row.campaign_id,
            )
            return

        name = row.name or "Student"
        response = send_automation(
            row.phone, message_body, name=name, tenant_id=tenant_id
        )
        if response.status_code != 200:
            raise Exception(
                f"API error {response.status_code}: {response.text[:200]}"
            )

        wa_message_id = _extract_message_id(response)
        repo.mark_recipient_sent(
            tenant_id, row.id,
            wa_message_id=wa_message_id,
            sent_at=datetime.utcnow(),
        )
        session.commit()
        logger.info(
            "📤 Campaign send → %s (campaign=%s)", row.phone, row.campaign_id
        )

    except Exception as e:
        logger.warning(
            "⚠️  Campaign send failed — phone=%s campaign=%s: %s",
            row.phone, row.campaign_id, e,
        )
        _handle_failure(repo, session, tenant_id, row, str(e), now)


def _handle_failure(repo, session, tenant_id, row, reason, now):
    """Apply retry or terminal failure based on how many attempts have been made."""
    # attempt is 1-indexed: attempt=1 means this is the first failure.
    # FollowUpJob equivalent: retry_count >= 3 → done.
    attempt = (row.retry_count or 0) + 1

    if attempt >= MAX_RETRIES:
        repo.mark_recipient_failed(
            tenant_id, row.id, failure_reason=reason, attempted_at=now
        )
        session.commit()
        logger.warning(
            "🛑 Campaign recipient %s permanently failed after %d attempt(s) "
            "(campaign=%s)",
            row.phone, attempt, row.campaign_id,
        )
    else:
        # Backoff: 15 * attempt minutes — identical to FollowUpJob policy.
        next_send_at = now + timedelta(minutes=15 * attempt)
        repo.schedule_recipient_retry(
            tenant_id, row.id,
            failure_reason=reason,
            next_send_at=next_send_at,
            attempted_at=now,
        )
        session.commit()
        logger.info(
            "⏳ Campaign recipient %s retry %d/%d at %s (campaign=%s)",
            row.phone, attempt, MAX_RETRIES, next_send_at, row.campaign_id,
        )


# ── Campaign reconciliation ───────────────────────────────────────────────────

def _check_campaign_completion(repo, session, tenant_id, campaign_id, now):
    """Delegate campaign lifecycle reconciliation to CampaignService.

    Phase 8.2C.4: lifecycle decisions (complete vs. failed vs. still running)
    belong to the service layer, not the worker. The worker passes its own
    repo and session so the service shares the same transaction context and the
    worker remains the transaction owner (CampaignService.reconcile_campaign
    commits on the same session the worker opened).

    Possible outcomes from reconcile_campaign():
      "running"   — recipients still pending, no action
      "completed" — all terminal, at least one sent; service committed
      "failed"    — all terminal, zero sends; service committed
      "skipped"   — campaign not found or not running; no action
    """
    from app.marketing.campaign_service import CampaignService
    svc = CampaignService(repository=repo, session=session, clock=lambda: now)
    result = svc.reconcile_campaign(tenant_id, campaign_id)
    if result not in ("running", "skipped"):
        logger.info(
            "✅ Campaign %s → %s (tenant=%s)", campaign_id, result, tenant_id
        )


# ── Utilities ─────────────────────────────────────────────────────────────────

def _extract_message_id(response):
    """Extract the wamid from a successful Meta send response.

    Returns None on any error — a failed extraction must never block recording
    the send as successful. The wa_message_id is a best-effort join key for
    future delivery/read webhooks; its absence does not change send semantics.
    """
    try:
        return response.json()["messages"][0]["id"]
    except Exception:
        return None
