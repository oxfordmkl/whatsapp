"""
Phase 8.1B / 8.2A — Campaign repository (persistence access only, UNWIRED).

Scope guard: this is the *repository* layer for the Campaign Foundation —
tenant-scoped read and write access over the two campaign tables. It
deliberately contains NO campaign logic:

  * no lifecycle validation — update_status() writes whatever it is given;
    deciding whether draft→running is legal belongs to CampaignService (8.2B)
  * no audience evaluation, no sending, no scheduling, no retry policy
  * no derived counters — the caller decides what "sent" and "failed" mean and
    passes the numbers in; the repository only stores them
  * no WhatsApp, no HTTP, no threads

Tenant safety (Phase 8.1A rule, ADR-021 aligned): `tenant_id` is a REQUIRED
first positional argument on every method and appears in the WHERE clause of
every query, including writes. It is never defaulted, never inferred from the
session, and never resolved internally — a caller cannot accidentally read or
mutate another tenant's data. Update methods return None when the row does not
belong to the tenant, so a cross-tenant write is a silent no-op rather than a
corruption.

Transaction policy: this module NEVER commits and never rolls back. It may
flush() to obtain generated primary keys and to make writes visible to later
reads within the same transaction. Owning the transaction boundary is a
service-layer responsibility — identical to the contract established in
conversation_state_repository.py (Phase 1.5.5B).

Status: imported by nobody in production. Gated for future use behind
CAMPAIGN_ENGINE_V2 (default OFF); this module does not read the flag itself.
"""


from datetime import datetime

# Canonical status values this repository writes by name.
#
# Mirrored from app.models rather than imported: the repository is
# model-injectable (see __init__), so importing app.models here would defeat
# that and drag the whole app package into any consumer. A drift-guard test
# asserts these stay equal to the model constants.
STATUS_FAILED = "failed"
STATUS_ARCHIVED = "archived"

# Recipient delivery states this repository writes by name (same mirroring
# rationale; a drift-guard test keeps them equal to the model constants).
RECIPIENT_QUEUED = "queued"
RECIPIENT_SENDING = "sending"
RECIPIENT_SENT = "sent"
RECIPIENT_FAILED = "failed"


class CampaignRepository:
    """Tenant-scoped read access to Campaign / CampaignRecipient.

    `session` and the model classes are injectable for testing; in production
    they resolve lazily to app.extensions.db.session and app.models.
    """

    __slots__ = ("_session", "_campaign", "_recipient")

    def __init__(self, session=None, campaign_model=None, recipient_model=None):
        self._session = session
        self._campaign = campaign_model
        self._recipient = recipient_model

    # ── lazy collaborators ────────────────────────────────────────────────
    @property
    def _s(self):
        if self._session is not None:
            return self._session
        from app.extensions import db  # lazy: no import-time app dependency
        return db.session

    @property
    def _c(self):
        if self._campaign is not None:
            return self._campaign
        from app.models import Campaign
        return Campaign

    @property
    def _r(self):
        if self._recipient is not None:
            return self._recipient
        from app.models import CampaignRecipient
        return CampaignRecipient

    # ── Campaign reads ────────────────────────────────────────────────────
    def get(self, tenant_id, campaign_id):
        """Return the campaign, or None when it does not belong to the tenant."""
        c = self._c
        return (
            self._s.query(c)
            .filter(c.id == campaign_id, c.tenant_id == tenant_id)
            .first()
        )

    def list_for_tenant(self, tenant_id, status=None, limit=50, offset=0):
        """Campaigns for one tenant, newest first. Optionally filtered by status.

        Ordering + tenant filter match ix_campaigns_tenant_created;
        status filtering matches ix_campaigns_tenant_status.
        """
        c = self._c
        q = self._s.query(c).filter(c.tenant_id == tenant_id)
        if status is not None:
            q = q.filter(c.status == status)
        return q.order_by(c.created_at.desc()).limit(limit).offset(offset).all()

    def count_for_tenant(self, tenant_id, status=None) -> int:
        c = self._c
        q = self._s.query(c).filter(c.tenant_id == tenant_id)
        if status is not None:
            q = q.filter(c.status == status)
        return q.count()

    # ── Recipient reads ───────────────────────────────────────────────────
    def list_recipients(self, tenant_id, campaign_id, status=None,
                        limit=100, offset=0):
        """Recipients of one campaign, tenant-scoped.

        campaign_id alone would be sufficient given the FK, but tenant_id is
        included so the tenant filter is present on every recipient query
        without exception.
        """
        r = self._r
        q = self._s.query(r).filter(
            r.campaign_id == campaign_id, r.tenant_id == tenant_id
        )
        if status is not None:
            q = q.filter(r.status == status)
        return q.order_by(r.id).limit(limit).offset(offset).all()

    def count_recipients(self, tenant_id, campaign_id, status=None) -> int:
        r = self._r
        q = self._s.query(r).filter(
            r.campaign_id == campaign_id, r.tenant_id == tenant_id
        )
        if status is not None:
            q = q.filter(r.status == status)
        return q.count()

    def status_breakdown(self, tenant_id, campaign_id) -> dict:
        """{status: count} for one campaign — the progress roll-up.

        Grouped aggregate rather than N counts; matches
        ix_campaign_recipients_campaign_status.
        """
        from sqlalchemy import func
        r = self._r
        rows = (
            self._s.query(r.status, func.count(r.id))
            .filter(r.campaign_id == campaign_id, r.tenant_id == tenant_id)
            .group_by(r.status)
            .all()
        )
        return {status: count for status, count in rows}

    def get_recipient_by_wa_message_id(self, tenant_id, wa_message_id):
        """Look up a recipient by its Meta message id.

        The join used by a future delivery-status webhook. Read-only here.
        """
        r = self._r
        return (
            self._s.query(r)
            .filter(r.wa_message_id == wa_message_id, r.tenant_id == tenant_id)
            .first()
        )

    # ── Writes (Phase 8.2A) ───────────────────────────────────────────────
    # None of these commit. All are tenant-scoped. None validate lifecycle.

    def create_campaign(self, tenant_id, name, description=None,
                        message_body=None, template_id=None,
                        audience_rule_id=None, scheduled_at=None,
                        status=None, created_by=None):
        """Insert a campaign row and return it (flushed, so `id` is populated).

        `status` defaults to the model default (draft) when not supplied. The
        repository does not decide whether the requested status is a legal
        starting state — that is CampaignService's call.
        """
        c = self._c
        campaign = c(
            tenant_id=tenant_id,
            name=name,
            description=description,
            message_body=message_body,
            template_id=template_id,
            audience_rule_id=audience_rule_id,
            scheduled_at=scheduled_at,
            created_by=created_by,
        )
        if status is not None:
            campaign.status = status
        self._s.add(campaign)
        self._s.flush()          # assign PK; NO commit
        return campaign

    def add_recipients(self, tenant_id, campaign_id, recipients):
        """Bulk-insert recipient rows for a campaign. Returns the count added.

        `recipients` is an iterable of either phone strings or mappings with at
        least a "phone" key and optionally "name" / "send_at".

        Caller responsibilities (deliberately NOT handled here):
          * de-duplicating the input — the DB enforces
            UNIQUE(campaign_id, phone) and will raise IntegrityError on a
            repeat, which the service should treat as a programming error
          * confirming the campaign belongs to the tenant before calling
          * setting total_recipients (see update_counters)

        add_all() is used rather than a bulk insert so the model's Python-side
        column defaults (status, retry_count, timestamps) are applied.
        """
        r = self._r
        rows = []
        for item in recipients or []:
            if isinstance(item, dict):
                phone = item.get("phone")
                name = item.get("name")
                send_at = item.get("send_at")
            else:
                phone, name, send_at = item, None, None
            if not phone:
                continue
            rows.append(r(
                campaign_id=campaign_id,
                tenant_id=tenant_id,
                phone=phone,
                name=name,
                send_at=send_at,
            ))
        if not rows:
            return 0
        self._s.add_all(rows)
        self._s.flush()          # surface IntegrityError now, not at commit
        return len(rows)

    def update_status(self, tenant_id, campaign_id, status,
                      started_at=None, completed_at=None):
        """Set a campaign's status. Returns the campaign, or None if not found
        for this tenant.

        No transition validation — see the module docstring. Optional
        started_at / completed_at are written only when provided, so a caller
        can stamp them in the same round-trip.
        """
        campaign = self.get(tenant_id, campaign_id)
        if campaign is None:
            return None
        campaign.status = status
        if started_at is not None:
            campaign.started_at = started_at
        if completed_at is not None:
            campaign.completed_at = completed_at
        self._s.flush()
        return campaign

    def update_counters(self, tenant_id, campaign_id, total_recipients=None,
                        sent_count=None, failed_count=None):
        """Overwrite the cached counters. Returns the campaign, or None.

        Absolute values, not increments: the caller computes them (e.g. from
        status_breakdown()) so the repository never has to decide which
        recipient statuses count as sent or failed. Only the arguments supplied
        are written.
        """
        campaign = self.get(tenant_id, campaign_id)
        if campaign is None:
            return None
        if total_recipients is not None:
            campaign.total_recipients = total_recipients
        if sent_count is not None:
            campaign.sent_count = sent_count
        if failed_count is not None:
            campaign.failed_count = failed_count
        self._s.flush()
        return campaign

    def mark_failed(self, tenant_id, campaign_id, failure_reason,
                    completed_at=None):
        """Record a fatal campaign-level failure. Returns the campaign, or None.

        Stores the reason and sets status to the failed state. Recipient-level
        failures are a different concern and are not touched here.
        """
        campaign = self.get(tenant_id, campaign_id)
        if campaign is None:
            return None
        campaign.status = STATUS_FAILED
        campaign.failure_reason = failure_reason
        if completed_at is not None:
            campaign.completed_at = completed_at
        self._s.flush()
        return campaign

    def archive_campaign(self, tenant_id, campaign_id):
        """Set status to archived. Returns the campaign, or None.

        Archiving is a status change, not a delete — campaign history and its
        recipient ledger are retained for audit.
        """
        campaign = self.get(tenant_id, campaign_id)
        if campaign is None:
            return None
        campaign.status = STATUS_ARCHIVED
        self._s.flush()
        return campaign

    # ── Recipient-level persistence (Phase 8.2C.1) ────────────────────────
    #
    # These support the campaign worker (8.2C.2) but contain NO worker policy:
    # the repository does not decide when to give up, how long to back off, or
    # what counts as stale. The caller passes those in and owns the commit.
    #
    # Approved claim policy: queued → sending → commit → send. This module
    # performs the status transition only; the WORKER must commit before
    # sending, which is what makes a crash mid-send leave a recoverable
    # `sending` row rather than a silently re-sent one.
    #
    # Horizontal scaling is explicitly out of scope: no row locking, no
    # SKIP LOCKED, no advisory locks. Correctness rests on the verified
    # single-worker topology (1 replica × 1 gunicorn worker).

    def claim_next_batch(self, tenant_id, limit=50, campaign_id=None, now=None):
        """Claim due recipients by moving them queued → sending.

        Returns the claimed rows. Due means status='queued' AND the send time
        has arrived — `send_at` NULL counts as "send immediately", which is how
        an unscheduled campaign behaves.

        The caller MUST commit before sending. Without that commit the claim is
        not durable and a crash would leave the rows claimable again, which is
        exactly the double-send the approved policy exists to prevent.

        Ordering is by id so a batch is processed in creation order and the
        claim is deterministic across cycles.
        """
        from sqlalchemy import or_
        r = self._r
        reference = now or datetime.utcnow()

        q = self._s.query(r).filter(
            r.tenant_id == tenant_id,
            r.status == RECIPIENT_QUEUED,
            or_(r.send_at.is_(None), r.send_at <= reference),
        )
        if campaign_id is not None:
            q = q.filter(r.campaign_id == campaign_id)

        rows = q.order_by(r.id).limit(limit).all()
        for row in rows:
            row.status = RECIPIENT_SENDING
        if rows:
            self._s.flush()
        return rows

    def mark_recipient_sent(self, tenant_id, recipient_id, wa_message_id=None,
                            sent_at=None):
        """Record a successful send. Returns the recipient, or None.

        `wa_message_id` is stored as the join key for future Meta delivery /
        read webhooks; delivered_at and read_at stay untouched here because
        nothing has confirmed delivery yet — only that Meta accepted the send.
        """
        row = self._get_recipient(tenant_id, recipient_id)
        if row is None:
            return None
        stamp = sent_at or datetime.utcnow()
        row.status = RECIPIENT_SENT
        row.sent_at = stamp
        row.last_attempt_at = stamp
        if wa_message_id is not None:
            row.wa_message_id = wa_message_id
        self._s.flush()
        return row

    def mark_recipient_failed(self, tenant_id, recipient_id, failure_reason,
                              attempted_at=None):
        """Record a TERMINAL recipient failure. Returns the recipient, or None.

        Increments retry_count (a factual count of attempts) and stores the
        reason. Deciding that this attempt was the last one — the 3-retry cap —
        is worker policy, not a repository concern: the worker calls this method
        instead of schedule_recipient_retry() when the cap is reached.
        """
        row = self._get_recipient(tenant_id, recipient_id)
        if row is None:
            return None
        row.status = RECIPIENT_FAILED
        row.retry_count = (row.retry_count or 0) + 1
        row.last_attempt_at = attempted_at or datetime.utcnow()
        row.failure_reason = failure_reason
        self._s.flush()
        return row

    def schedule_recipient_retry(self, tenant_id, recipient_id, failure_reason,
                                 next_send_at, attempted_at=None):
        """Return a failed attempt to the queue for a later retry.

        Separate from mark_recipient_failed() so neither method has to decide
        between retrying and giving up — the worker chooses which to call, and
        supplies `next_send_at` computed from the approved backoff policy
        (15/30/45 minutes). The repository never computes a backoff.
        """
        row = self._get_recipient(tenant_id, recipient_id)
        if row is None:
            return None
        row.status = RECIPIENT_QUEUED
        row.retry_count = (row.retry_count or 0) + 1
        row.last_attempt_at = attempted_at or datetime.utcnow()
        row.failure_reason = failure_reason
        row.send_at = next_send_at
        self._s.flush()
        return row

    def find_stuck_recipients(self, tenant_id, stale_before, limit=100):
        """Recipients left in `sending` since before `stale_before` (read-only).

        A row reaches this state when the process died between the claim commit
        and the send outcome. `stale_before` is supplied by the caller — the
        repository holds no opinion on what "stale" means.
        """
        r = self._r
        return (
            self._s.query(r)
            .filter(
                r.tenant_id == tenant_id,
                r.status == RECIPIENT_SENDING,
                r.last_attempt_at.is_(None) | (r.last_attempt_at < stale_before),
            )
            .order_by(r.id)
            .limit(limit)
            .all()
        )

    def reclaim_stale_recipients(self, tenant_id, stale_before, limit=100,
                                 increment_retry=False):
        """Return stuck `sending` rows to `queued`. Returns the count reclaimed.

        `increment_retry` defaults to False because a stuck row may well have
        been delivered — the process died without learning the outcome, so the
        attempt is not known to have failed. The caller may set it True to make
        reclaims count against the retry cap and so bound the number of times a
        single recipient can be re-attempted.

        Rows are iterated rather than bulk-updated so the identity map stays
        consistent for the caller; reclaim batches are small by nature.
        """
        rows = self.find_stuck_recipients(tenant_id, stale_before, limit=limit)
        for row in rows:
            row.status = RECIPIENT_QUEUED
            if increment_retry:
                row.retry_count = (row.retry_count or 0) + 1
        if rows:
            self._s.flush()
        return len(rows)

    def pending_tenant_ids(self, now=None, limit=100):
        """Distinct tenant_ids that currently have due recipients.

        The ONLY method here that is not tenant-scoped, and deliberately so: a
        background worker must discover which tenants have work without being
        told, exactly as the follow-up worker polls globally today. It returns
        identifiers ONLY — never row data — so no tenant's content can leak
        through it. The worker then claims per tenant via claim_next_batch(),
        keeping every data access tenant-scoped.
        """
        from sqlalchemy import or_
        r = self._r
        reference = now or datetime.utcnow()
        rows = (
            self._s.query(r.tenant_id)
            .filter(
                r.status == RECIPIENT_QUEUED,
                or_(r.send_at.is_(None), r.send_at <= reference),
            )
            .distinct()
            .limit(limit)
            .all()
        )
        return [t[0] for t in rows]

    # ── internal ──────────────────────────────────────────────────────────
    def _get_recipient(self, tenant_id, recipient_id):
        """Tenant-scoped recipient lookup used by the write methods."""
        r = self._r
        return (
            self._s.query(r)
            .filter(r.id == recipient_id, r.tenant_id == tenant_id)
            .first()
        )
