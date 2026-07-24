"""
Phase 8.2B — CampaignService: lifecycle, validation and transaction ownership.

This service owns the *rules* of a campaign. It deliberately does NOT send
anything: no WhatsApp call, no worker, no scheduler, no retry, no recipient
delivery-status updates. Those arrive in Phase 8.2C.

Layering (Phase 8.1A approved):

    routes (8.2D)  →  CampaignService (here)  →  CampaignRepository (8.2A)
                                              →  db.session

  * CampaignRepository holds NO rules and never commits.
  * CampaignService holds the rules and OWNS the transaction boundary
    (begin / commit / rollback).

Tenant safety (ADR-021): `tenant_id` is required on every public method and is
never inferred. A falsy tenant_id raises rather than silently writing — the
same rule the admin routes follow when `_actor_tenant_id()` returns None.

Feature flag: mutating operations refuse to run unless CAMPAIGN_ENGINE_V2 is
enabled. With the flag OFF (the default, and production today) this service is
inert even if something imports and calls it — defence in depth while the
legacy engine remains live. Read-only helpers (validation, transition checks)
are always available so they can be exercised without enabling the engine.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Status vocabulary ────────────────────────────────────────────────────────
# Mirrored from app.models rather than imported so this module stays testable
# without the app package (same rationale as campaign_repository.py). A
# drift-guard test asserts these match the model constants.
DRAFT = "draft"
VALIDATED = "validated"
SCHEDULED = "scheduled"
RUNNING = "running"
COMPLETED = "completed"
CANCELLED = "cancelled"
FAILED = "failed"
ARCHIVED = "archived"

# ── Recipient terminal / non-terminal vocabulary (Phase 8.2C.4) ──────────────
# Mirrored from app.models for the same reason as the campaign constants above.
# A drift-guard test keeps these equal to the model constants.
#
# Non-terminal (a campaign with any of these is still running):
R_QUEUED = "queued"
R_SENDING = "sending"
#
# Successful terminal (at least one → campaign COMPLETED not FAILED):
R_SENT = "sent"
R_DELIVERED = "delivered"
R_READ = "read"
#
# Unsuccessful terminal (all of these → campaign FAILED when no success exists):
R_FAILED = "failed"
R_CANCELLED = "cancelled"

# ── Lifecycle (exactly the Phase 8.2B approved transition set) ───────────────
#
#   draft      → validated
#   validated  → scheduled | running
#   scheduled  → running
#   running    → completed | failed | cancelled
#   completed / failed / cancelled → archived
#   archived   → (terminal)
#
ALLOWED_TRANSITIONS = {
    DRAFT:     frozenset({VALIDATED}),
    VALIDATED: frozenset({SCHEDULED, RUNNING}),
    SCHEDULED: frozenset({RUNNING}),
    RUNNING:   frozenset({COMPLETED, FAILED, CANCELLED}),
    COMPLETED: frozenset({ARCHIVED}),
    FAILED:    frozenset({ARCHIVED}),
    CANCELLED: frozenset({ARCHIVED}),
    ARCHIVED:  frozenset(),
}

# Recipient ceiling. Deliberately identical to the legacy engine's limit
# (campaign_service.start_campaign) so the new path is never more permissive
# than the one it will replace. Raising it requires the tenant quota policy
# that Phase 8.1A recorded as still-undecided.
MAX_RECIPIENTS = 100

# Minimum digits in a recipient identifier. Format normalisation (the legacy
# "91" prefixing) is a send-path concern and belongs to BroadcastService in
# 8.2C — this service only rejects obviously unusable values.
MIN_PHONE_DIGITS = 8


# ── Errors ───────────────────────────────────────────────────────────────────
class CampaignEngineDisabled(RuntimeError):
    """Raised when a mutating operation runs while CAMPAIGN_ENGINE_V2 is OFF."""


class CampaignValidationError(ValueError):
    """Raised when a create/transition is attempted with invalid input.

    Carries the structured ValidationResult so callers can surface field-level
    errors rather than a single string.
    """

    def __init__(self, result):
        super().__init__("; ".join(result.errors) or "validation failed")
        self.result = result


class CampaignTransitionError(ValueError):
    """Raised when a lifecycle transition is not permitted."""

    def __init__(self, from_status, to_status):
        super().__init__(f"illegal transition {from_status} -> {to_status}")
        self.from_status = from_status
        self.to_status = to_status


# ── Structured validation result ─────────────────────────────────────────────
@dataclass(frozen=True)
class ValidationResult:
    """Outcome of a validation call.

    `ok` is True only when there are no errors. Warnings never block.
    """
    errors: tuple = field(default_factory=tuple)
    warnings: tuple = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.errors

    def __bool__(self) -> bool:
        return self.ok

    def merge(self, other):
        return ValidationResult(
            errors=tuple(self.errors) + tuple(other.errors),
            warnings=tuple(self.warnings) + tuple(other.warnings),
        )

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": list(self.errors),
                "warnings": list(self.warnings)}


def _ok(warnings=()) -> ValidationResult:
    return ValidationResult(errors=(), warnings=tuple(warnings))


def _err(*errors) -> ValidationResult:
    return ValidationResult(errors=tuple(errors))


# ── Service ──────────────────────────────────────────────────────────────────
class CampaignService:
    """Campaign lifecycle, validation and transaction ownership.

    `repository`, `session` and `clock` are injectable for testing; in
    production they resolve lazily to CampaignRepository, db.session and
    datetime.utcnow.
    """

    __slots__ = ("_repo", "_session", "_clock")

    def __init__(self, repository=None, session=None, clock=None):
        self._repo = repository
        self._session = session
        self._clock = clock

    # ── lazy collaborators ────────────────────────────────────────────────
    @property
    def repository(self):
        if self._repo is not None:
            return self._repo
        from app.persistence.campaign_repository import CampaignRepository
        return CampaignRepository()

    @property
    def session(self):
        if self._session is not None:
            return self._session
        from app.extensions import db  # lazy: no import-time app dependency
        return db.session

    def _now(self):
        return self._clock() if self._clock else datetime.utcnow()

    # ── Guards ────────────────────────────────────────────────────────────
    @staticmethod
    def engine_enabled() -> bool:
        """True when CAMPAIGN_ENGINE_V2 is on. Read live on every call."""
        try:
            from app.flags import campaign_engine_v2_enabled
            return campaign_engine_v2_enabled()
        except Exception:      # pragma: no cover - defensive
            return False

    def _require_engine(self):
        if not self.engine_enabled():
            raise CampaignEngineDisabled(
                "CAMPAIGN_ENGINE_V2 is OFF — the legacy engine remains live."
            )

    @staticmethod
    def _require_tenant(tenant_id):
        """ADR-021: never infer a tenant; refuse to write without one."""
        if not tenant_id:
            raise CampaignValidationError(_err("tenant_id is required"))

    # ── Validation (always available, no flag, no DB) ─────────────────────
    def validate_campaign(self, name=None, message_body=None, template_id=None,
                          description=None) -> ValidationResult:
        """Validate campaign content.

        Rules:
          * name is required and non-blank
          * exactly one content source — a message body OR a template
        """
        errors, warnings = [], []

        if not name or not str(name).strip():
            errors.append("name is required")
        elif len(str(name).strip()) > 200:
            errors.append("name exceeds 200 characters")

        has_body = bool(message_body and str(message_body).strip())
        has_template = template_id is not None
        if not has_body and not has_template:
            errors.append("either message_body or template_id is required")
        elif has_body and has_template:
            errors.append("provide message_body or template_id, not both")

        if description is not None and len(str(description)) > 5000:
            warnings.append("description is unusually long")

        return ValidationResult(errors=tuple(errors), warnings=tuple(warnings))

    def validate_recipients(self, recipients) -> ValidationResult:
        """Validate an audience list.

        Rules:
          * at least one recipient
          * at most MAX_RECIPIENTS
          * no duplicate phones (the DB would raise; caught here first)
          * each phone present and plausibly long enough

        Normalisation is NOT performed — that is a send-path concern (8.2C).
        """
        errors, warnings = [], []
        phones = []

        for item in recipients or []:
            phone = item.get("phone") if isinstance(item, dict) else item
            if not phone or not str(phone).strip():
                errors.append("recipient with empty phone")
                continue
            phone = str(phone).strip()
            if sum(ch.isdigit() for ch in phone) < MIN_PHONE_DIGITS:
                errors.append(f"implausible phone: {phone}")
                continue
            phones.append(phone)

        if not phones and not errors:
            errors.append("at least one recipient is required")

        if len(phones) > MAX_RECIPIENTS:
            errors.append(
                f"audience of {len(phones)} exceeds the maximum of {MAX_RECIPIENTS}"
            )

        duplicates = {p for p in phones if phones.count(p) > 1}
        if duplicates:
            errors.append(f"duplicate recipients: {', '.join(sorted(duplicates))}")

        return ValidationResult(errors=tuple(errors), warnings=tuple(warnings))

    def validate_schedule(self, scheduled_at, now=None) -> ValidationResult:
        """Validate a requested schedule time.

        Rules:
          * required for a scheduled campaign
          * must be a datetime
          * must be in the future
        """
        if scheduled_at is None:
            return _err("scheduled_at is required to schedule a campaign")
        if not isinstance(scheduled_at, datetime):
            return _err("scheduled_at must be a datetime")
        reference = now or self._now()
        if scheduled_at <= reference:
            return _err("scheduled_at must be in the future")
        return _ok()

    # ── Lifecycle rules (pure, no DB) ─────────────────────────────────────
    @staticmethod
    def can_transition(from_status, to_status) -> bool:
        return to_status in ALLOWED_TRANSITIONS.get(from_status, frozenset())

    @staticmethod
    def allowed_next(from_status) -> frozenset:
        return ALLOWED_TRANSITIONS.get(from_status, frozenset())

    @staticmethod
    def is_terminal(status) -> bool:
        return not ALLOWED_TRANSITIONS.get(status, frozenset())

    # ── Commands (own the transaction) ────────────────────────────────────
    def create_campaign(self, tenant_id, name, recipients=None,
                        message_body=None, template_id=None,
                        audience_rule_id=None, description=None,
                        created_by=None):
        """Create a campaign in `draft`, optionally with its recipients.

        Validates first, then persists via the repository, then COMMITS. Any
        failure rolls the whole thing back — a campaign is never left half
        created with a partial audience.

        Recipients are optional: a draft may be saved before its audience is
        chosen. When supplied they are validated and stored, and
        total_recipients is set.
        """
        self._require_engine()
        self._require_tenant(tenant_id)

        result = self.validate_campaign(
            name=name, message_body=message_body, template_id=template_id,
            description=description,
        )
        if recipients is not None:
            result = result.merge(self.validate_recipients(recipients))
        if not result.ok:
            raise CampaignValidationError(result)

        try:
            campaign = self.repository.create_campaign(
                tenant_id, name,
                description=description,
                message_body=message_body,
                template_id=template_id,
                audience_rule_id=audience_rule_id,
                created_by=created_by,
            )
            if recipients:
                added = self.repository.add_recipients(
                    tenant_id, campaign.id, recipients
                )
                self.repository.update_counters(
                    tenant_id, campaign.id, total_recipients=added
                )
            self._audit_campaign_created(tenant_id, campaign)
            self.session.commit()
            return campaign
        except Exception:
            self.session.rollback()
            raise

    def transition(self, tenant_id, campaign_id, to_status,
                   failure_reason=None):
        """Move a campaign to `to_status`, enforcing the lifecycle.

        Raises CampaignTransitionError when the move is not permitted, leaving
        the campaign untouched. Stamps started_at / completed_at as the target
        state requires. Owns commit / rollback.
        """
        self._require_engine()
        self._require_tenant(tenant_id)

        campaign = self.repository.get(tenant_id, campaign_id)
        if campaign is None:
            raise CampaignValidationError(
                _err(f"campaign {campaign_id} not found for this tenant")
            )

        from_status = campaign.status
        if not self.can_transition(from_status, to_status):
            raise CampaignTransitionError(from_status, to_status)

        now = self._now()
        started_at = now if to_status == RUNNING else None
        completed_at = now if to_status in (COMPLETED, CANCELLED, FAILED) else None

        try:
            if to_status == FAILED:
                self.repository.mark_failed(
                    tenant_id, campaign_id,
                    failure_reason or "unspecified failure",
                    completed_at=completed_at,
                )
            elif to_status == ARCHIVED:
                self.repository.archive_campaign(tenant_id, campaign_id)
            else:
                self.repository.update_status(
                    tenant_id, campaign_id, to_status,
                    started_at=started_at, completed_at=completed_at,
                )
            self._audit_status_changed(tenant_id, campaign, from_status, to_status)
            self.session.commit()
            return campaign
        except Exception:
            self.session.rollback()
            raise

    # ── Named lifecycle commands (thin wrappers over transition) ──────────
    def mark_validated(self, tenant_id, campaign_id):
        return self.transition(tenant_id, campaign_id, VALIDATED)

    def schedule(self, tenant_id, campaign_id, scheduled_at):
        """validated → scheduled, storing the run time.

        The schedule itself is validated here; nothing consumes scheduled_at
        until the worker exists (8.2C).
        """
        self._require_engine()
        self._require_tenant(tenant_id)

        result = self.validate_schedule(scheduled_at)
        if not result.ok:
            raise CampaignValidationError(result)

        campaign = self.repository.get(tenant_id, campaign_id)
        if campaign is None:
            raise CampaignValidationError(
                _err(f"campaign {campaign_id} not found for this tenant")
            )
        if not self.can_transition(campaign.status, SCHEDULED):
            raise CampaignTransitionError(campaign.status, SCHEDULED)

        try:
            campaign.scheduled_at = scheduled_at
            self.repository.update_status(tenant_id, campaign_id, SCHEDULED)
            self._audit_status_changed(tenant_id, campaign, VALIDATED, SCHEDULED)
            self.session.commit()
            return campaign
        except Exception:
            self.session.rollback()
            raise

    def mark_running(self, tenant_id, campaign_id):
        return self.transition(tenant_id, campaign_id, RUNNING)

    def mark_completed(self, tenant_id, campaign_id):
        return self.transition(tenant_id, campaign_id, COMPLETED)

    def mark_failed(self, tenant_id, campaign_id, failure_reason):
        return self.transition(tenant_id, campaign_id, FAILED,
                               failure_reason=failure_reason)

    def cancel(self, tenant_id, campaign_id):
        return self.transition(tenant_id, campaign_id, CANCELLED)

    def archive(self, tenant_id, campaign_id):
        return self.transition(tenant_id, campaign_id, ARCHIVED)

    # ── Read-through helpers ──────────────────────────────────────────────
    def get_campaign(self, tenant_id, campaign_id):
        self._require_tenant(tenant_id)
        return self.repository.get(tenant_id, campaign_id)

    def list_campaigns(self, tenant_id, status=None, limit=50, offset=0):
        self._require_tenant(tenant_id)
        return self.repository.list_for_tenant(
            tenant_id, status=status, limit=limit, offset=offset
        )

    def progress(self, tenant_id, campaign_id) -> dict:
        """Recipient status roll-up for one campaign."""
        self._require_tenant(tenant_id)
        return self.repository.status_breakdown(tenant_id, campaign_id)

    # ── Reconciliation (Phase 8.2C.4) ─────────────────────────────────────

    def reconcile_campaign(self, tenant_id, campaign_id) -> str:
        """Evaluate a running campaign's recipient state and advance lifecycle if complete.

        Called by the worker after every recipient batch. Returns one of:

          "running"   — recipients still pending; status unchanged
          "completed" — all recipients terminal, at least one sent successfully
          "failed"    — all recipients terminal, zero successful sends
          "skipped"   — campaign not found or not currently RUNNING

        Owns the transaction: commits on a status change, rolls back on error.
        Never called while the campaign is in any state other than RUNNING — the
        guard on campaign.status prevents double-transitions.

        Reconciliation rule (exact):
          non-terminal = queued | sending
          successful   = sent | delivered | read
          if any non-terminal → "running"
          elif successful > 0 → "completed"
          else               → "failed"
        """
        self._require_engine()
        self._require_tenant(tenant_id)

        campaign = self.repository.get(tenant_id, campaign_id)
        if campaign is None or campaign.status != RUNNING:
            return "skipped"

        breakdown = self.repository.status_breakdown(tenant_id, campaign_id)
        outcome = self._evaluate_outcome(breakdown)

        if outcome is None:
            return "running"

        try:
            now = self._now()
            if outcome == COMPLETED:
                self.repository.update_status(
                    tenant_id, campaign_id, COMPLETED, completed_at=now
                )
            else:
                self.repository.mark_failed(
                    tenant_id, campaign_id,
                    failure_reason="all recipients failed or cancelled — zero successful sends",
                    completed_at=now,
                )
            self._audit_status_changed(tenant_id, campaign, RUNNING, outcome)
            self.session.commit()
            return outcome
        except Exception:
            self.session.rollback()
            raise

    @staticmethod
    def _evaluate_outcome(breakdown: dict):
        """Pure function: classify a status_breakdown into an outcome.

        Returns COMPLETED, FAILED, or None.

        None means the campaign still has non-terminal recipients and must
        remain RUNNING. COMPLETED / FAILED mean all recipients are terminal.

        The breakdown dict comes from CampaignRepository.status_breakdown()
        which returns {status_string: count} with no "total" key — total is
        computed here as sum(breakdown.values()).
        """
        if not breakdown:
            return None

        if breakdown.get(R_QUEUED, 0) > 0 or breakdown.get(R_SENDING, 0) > 0:
            return None  # non-terminal recipients remain

        total = sum(breakdown.values())
        if total == 0:
            return None

        success = (
            breakdown.get(R_SENT, 0)
            + breakdown.get(R_DELIVERED, 0)
            + breakdown.get(R_READ, 0)
        )
        return COMPLETED if success > 0 else FAILED

    # ── Audit hook points (Phase 8.2B: internal only, NOT wired) ──────────
    # audit_service integration is deliberately deferred. These exist so the
    # call sites are already correct when it is added, and so a subclass or a
    # test can observe lifecycle events today.

    def _audit_campaign_created(self, tenant_id, campaign):
        logger.info("[campaign] created tenant=%s id=%s", tenant_id, campaign.id)

    def _audit_status_changed(self, tenant_id, campaign, from_status, to_status):
        logger.info("[campaign] status tenant=%s id=%s %s -> %s",
                    tenant_id, campaign.id, from_status, to_status)
