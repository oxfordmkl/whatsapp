"""
Phase 8.2C.2 — Campaign Worker execution loop (UNWIRED).

Clones the proven FollowUpJob polling architecture and adds the approved
campaign execution contract:

    pending_tenant_ids()
      → claim_next_batch()
        → COMMIT claim
          → send_campaign_message()
            → persist outcome (sent / retry / failed)
              → COMMIT outcome
                → check campaign completion

Nothing in this module is started or imported by production code. Startup
registration is Phase 8.2C.3. CAMPAIGN_ENGINE_V2 remains OFF in production.

Layering: CampaignWorker calls CampaignRepository and send_campaign_message()
only. ADR-024 D1: dispatch never calls send_automation — that interceptor is
for conversational automation (Phase 11-D3B2), a different contract with
different accounting requirements (see ADR-024 for the full analysis).
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

# ADR-024 D6: inter-send delay, matching the legacy engine's proven posture
# (app/services/campaign_service.py). A module constant so it is adjustable
# without a schema or API change.
CAMPAIGN_SEND_DELAY_SECONDS = 1.5

# Set by init_campaign_worker(). Not called from anywhere until Phase 8.2C.3.
_app = None

# Phase 8.2E.10 H2: read once at startup, refuses to start the worker under
# multi-process concurrency (see init_campaign_worker docstring).
_MAX_SAFE_WEB_CONCURRENCY = 1

# Phase 8.2E.10 H1: tracks whether the loop is currently paused (flag OFF) so
# the transition is logged exactly once in each direction, not every 300s
# cycle. False at import time (assume active): if the flag is ON on the
# first iteration — the common case — nothing is logged, matching "avoid log
# spam". If it is OFF, the first iteration correctly logs a pause.
_paused = False


def init_campaign_worker(app):
    """Store the Flask app reference and launch the daemon worker thread.

    Called once from create_app() — the call site is Phase 8.2C.3 and is
    intentionally absent from app/__init__.py until that phase is approved.
    This function exists so the call site can be added without touching this
    module again.

    Phase 8.2E.10 H2: refuses to start under WEB_CONCURRENCY > 1. Nothing in
    claim_next_batch() takes row locks (SELECT ... FOR UPDATE / SKIP LOCKED),
    so two worker processes claiming the same tenant's due recipients at once
    both see the same 'queued' rows and both send them — there is no
    concurrency-safety mechanism for this fix to rely on, only the single-
    process assumption the module has always documented. WEB_CONCURRENCY is
    read directly from the environment (not via app.flags) because this is an
    infrastructure-topology check, not a feature flag. Fails closed: on
    refusal, the application continues running — every other subsystem,
    including FollowUpJob, is unaffected — but no campaign worker thread is
    created, so no duplicate sends. FollowUpService is explicitly out of
    scope for this phase; it is not concurrency-safe either, but its worker
    is unconditional (not gated by CAMPAIGN_ENGINE_V2) and asserting there
    is a separate change with its own approval.
    """
    import os

    raw = os.environ.get("WEB_CONCURRENCY")
    if raw is not None:
        try:
            concurrency = int(raw)
        except ValueError:
            logger.error(
                "❌ Campaign worker refused to start — WEB_CONCURRENCY=%r is "
                "not a valid integer. claim_next_batch() has no row-level "
                "locking and is unsafe under multiple processes; set "
                "WEB_CONCURRENCY=1 (or unset it) to start the worker.",
                raw,
            )
            return
        if concurrency > _MAX_SAFE_WEB_CONCURRENCY:
            logger.error(
                "❌ Campaign worker refused to start — WEB_CONCURRENCY=%d > %d. "
                "claim_next_batch() has no row-level locking and is unsafe "
                "under multiple processes; two workers would claim and send "
                "the same recipients. Set WEB_CONCURRENCY=1 to start the "
                "worker, or implement SKIP LOCKED claiming first.",
                concurrency, _MAX_SAFE_WEB_CONCURRENCY,
            )
            return

    global _app
    _app = app
    threading.Thread(target=_campaign_worker_loop, daemon=True).start()
    logger.info("✅ Campaign worker started")


# ── Poll loop ─────────────────────────────────────────────────────────────────

def _campaign_worker_loop():
    """Main poll loop. Mirrors _followup_worker() structure exactly.

    Phase 8.2E.10 H1: re-reads CAMPAIGN_ENGINE_V2 on every iteration, not
    just at startup. Without this, flipping the flag OFF on a running
    instance stops the HTTP routes (require_campaign_engine checks live) but
    leaves this thread dispatching real WhatsApp sends every cycle — an
    operator would reasonably believe the feature was disabled and be wrong.
    The thread is never recreated and Flask is never restarted; a flag flip
    in either direction takes effect on the next iteration, making this loop
    symmetric with the route-level check.
    """
    global _paused
    while True:
        from app.flags import campaign_engine_v2_enabled

        if not campaign_engine_v2_enabled():
            if not _paused:
                logger.info(
                    "⏸️  Campaign worker paused — CAMPAIGN_ENGINE_V2 is OFF"
                )
                _paused = True
            time.sleep(POLL_INTERVAL)
            continue

        if _paused:
            logger.info(
                "▶️  Campaign worker resumed — CAMPAIGN_ENGINE_V2 is ON"
            )
            _paused = False

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

        # ADR-024 D3: resolve once per campaign, not per recipient — the
        # template is invariant across a batch. None means either the
        # campaign has no template_id (message_body-only) or resolution
        # failed; launch-time validation (CampaignService.transition) should
        # have already refused an unusable template, so None here in
        # practice means "no template configured".
        template = None
        if campaign and getattr(campaign, "template_id", None):
            from app.marketing.campaign_service import resolve_campaign_template
            template = resolve_campaign_template(
                tenant_id, campaign.template_id, session=session
            )

        for row in rows:
            _send_one(repo, session, tenant_id, row, message_body, template, now)

        # ADR-025 D10: keep the campaign's denormalised counters in step with
        # the recipient rows /progress already reports from — without this,
        # list/detail responses report stale zeros that contradict /progress
        # for the same campaign (P8).
        _update_campaign_counters(repo, session, tenant_id, campaign_id)
        session.commit()

        # After all recipients in this campaign are processed, check whether
        # the campaign is now fully terminal and can be marked completed.
        _check_campaign_completion(repo, session, tenant_id, campaign_id, now)


# ── Per-recipient send ────────────────────────────────────────────────────────

def _send_one(repo, session, tenant_id, row, message_body, template, now):
    """Send one recipient and persist the outcome. All exceptions isolated.

    ADR-024 D1: dispatches via send_campaign_message(), never send_automation
    — the interceptor is for conversational automation, not campaign content.
    No rate-limit delay on the opt-out fast path (mirrors the legacy engine's
    `continue`-skips-the-sleep behaviour); a delay applies to every path that
    actually reaches the provider.
    """
    from app.models import ConversationState

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
    try:
        result = send_campaign_message(row.phone, name, tenant_id, message_body, template)
        if result["outcome"] != "sent":
            raise Exception(result.get("reason") or "send failed")

        repo.mark_recipient_sent(
            tenant_id, row.id,
            wa_message_id=result.get("wa_message_id"),
            sent_at=datetime.utcnow(),
        )
        session.commit()
        # ADR-024 D5: restore conversation-history parity with the legacy
        # engine. Never allowed to turn a successful send into a failure.
        _log_campaign_send(
            tenant_id, row.phone, message_body,
            result.get("wa_message_id"), result.get("send_type", "text"),
        )
        logger.info(
            "📤 Campaign send → %s (campaign=%s)", row.phone, row.campaign_id
        )

    except Exception as e:
        logger.warning(
            "⚠️  Campaign send failed — phone=%s campaign=%s: %s",
            row.phone, row.campaign_id, e,
        )
        _handle_failure(repo, session, tenant_id, row, str(e), now)
    finally:
        # ADR-024 D6 — only reached on an actual dispatch attempt, never on
        # the opt-out early return above.
        time.sleep(CAMPAIGN_SEND_DELAY_SECONDS)


def _log_campaign_send(tenant_id, phone, message_body, wa_message_id, send_type):
    """ADR-024 D5: log a successful campaign send to conversation history.

    Best-effort — logging failure must never turn an already-committed send
    into a reported failure, so every exception is swallowed here.
    """
    try:
        from app.services.log_service import save_conversation_message
        save_conversation_message(
            phone=phone,
            direction="outgoing",
            message=message_body or "",
            message_type=send_type,
            source="campaign",
            wa_message_id=wa_message_id,
            tenant_id=tenant_id,
        )
    except Exception as e:
        logger.warning(
            "⚠️  Campaign conversation-history log failed — phone=%s: %s",
            phone, e,
        )


# ── Dispatch (ADR-024 D1/D2/D4) ────────────────────────────────────────────────

def _window_open(phone, tenant_id):
    """Return True iff the 24-hour customer-service window is open.

    Replicates the window check inside send_automation rather than calling
    it (ADR-024 R1) — campaign dispatch needs its own truthful outcome, not
    the interceptor's silent substitution.
    """
    from app.models import ConversationState

    state = ConversationState.query.filter_by(phone=phone, tenant_id=tenant_id).first()
    if state and state.last_msg:
        try:
            last_dt = datetime.fromisoformat(state.last_msg)
            if (datetime.utcnow() - last_dt).total_seconds() < 86400:
                return True
        except ValueError:
            pass
    return False


def _template_components(template, name):
    """Build Meta template `components` from MessageTemplate.variables.

    Only `name` is available on a CampaignRecipient snapshot today; any other
    declared variable resolves to an empty string rather than raising —
    richer recipient-level variables are an audience-phase concern, not a
    dispatch-phase one. Returns None when the template declares no variables
    (send_template() omits the components key entirely in that case).
    """
    import json

    try:
        var_names = json.loads(template.variables or "[]")
    except (ValueError, TypeError):
        var_names = []
    if not var_names:
        return None

    parameters = [
        {"type": "text", "text": name if str(v).lower() == "name" else ""}
        for v in var_names
    ]
    return [{"type": "body", "parameters": parameters}]


def send_campaign_message(phone, name, tenant_id, message_body, template):
    """ADR-024 D1/D2/D4: dispatch one campaign message with truthful accounting.

    Never delegates to the automation interceptor and never persists a
    pending-message fallback — those belong to conversational automation
    (ADR-024 D1). Returns a structured
    outcome so the caller never has to infer what happened from a bare HTTP
    status code:

        {"outcome": "sent",   "wa_message_id": ..., "send_type": "text"|"template"}
        {"outcome": "failed", "reason": ...}

    Window open  → plain text carrying the campaign's own message_body.
    Window closed + resolved template → the campaign's approved WhatsApp
      template (ADR-024 D3 resolution happens once per campaign, by the
      caller — `template` here is already resolved or None).
    Window closed + no template → failed, explicitly. No substitution.
    """
    from app.services.whatsapp_service import send_text, send_template

    if _window_open(phone, tenant_id):
        response = send_text(phone, message_body, tenant_id=tenant_id)
        if response.status_code == 200:
            return {
                "outcome": "sent",
                "wa_message_id": _extract_message_id(response),
                "send_type": "text",
            }
        return {
            "outcome": "failed",
            "reason": f"API error {response.status_code}: {response.text[:200]}",
        }

    if template is None:
        return {
            "outcome": "failed",
            "reason": "24-hour window closed and no approved WhatsApp template configured",
        }

    components = _template_components(template, name)
    response = send_template(
        phone, template.provider_template_id,
        lang=template.language or "en",
        components=components,
        tenant_id=tenant_id,
    )
    if response.status_code == 200:
        return {
            "outcome": "sent",
            "wa_message_id": _extract_message_id(response),
            "send_type": "template",
        }
    return {
        "outcome": "failed",
        "reason": f"API error {response.status_code}: {response.text[:200]}",
    }


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


# ── Counter maintenance (ADR-025 D10) ──────────────────────────────────────────

def _update_campaign_counters(repo, session, tenant_id, campaign_id):
    """Sync Campaign.sent_count / failed_count from status_breakdown().

    "Successful" and "unsuccessful terminal" mirror the exact groupings
    CampaignService._evaluate_outcome() uses for reconciliation — sent /
    delivered / read count as sent; failed / cancelled count as failed. Rows
    still in queued/sending are counted in neither, matching their non-terminal
    status. Does not commit — the caller commits alongside this call.
    """
    breakdown = repo.status_breakdown(tenant_id, campaign_id)
    sent_count = sum(breakdown.get(s, 0) for s in ("sent", "delivered", "read"))
    failed_count = sum(breakdown.get(s, 0) for s in ("failed", "cancelled"))
    repo.update_counters(
        tenant_id, campaign_id, sent_count=sent_count, failed_count=failed_count
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
