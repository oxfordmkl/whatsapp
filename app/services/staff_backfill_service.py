"""Phase RC2.3C — staff identity backfill.

A one-off migration utility. It populates the DORMANT assigned_user_id columns
added by RC2.3A from the existing assigned_staff strings. It changes no runtime
behaviour: nothing reads assigned_user_id, both feature flags remain OFF, and
app/data/staff_master.json remains the production source of truth.

WRITES EXACTLY TWO COLUMNS
--------------------------
    ConversationState.assigned_user_id
    Task.assigned_user_id

Nothing else is touched — not assigned_staff, not notifications, not
ConversationMessage, not any audit table, not display_name, not User, not the
legacy registry. That restraint is what makes rollback lossless: the legacy
string remains authoritative and untouched, so clearing the FK returns the
system to exactly its pre-backfill state.

RESOLUTION ORDER — strict precedence, first hit wins
----------------------------------------------------
    1. exact username            (case-sensitive, trimmed)
    2. case-insensitive username (trimmed)
    3. case-insensitive display_name (trimmed)

No partial matching, no contains(), no startswith(), no fuzzy matching, no
heuristics, and no "there is only one staff member so it must be them"
fallback. A value that does not match under one of the three rules above is
SKIPPED and reported.

Deliberately NOT reusing staff_service.resolve(): that helper matches username
and display_name in a single pass and treats them as equal, which cannot
express the precedence required here. Its semantics are correct for its own
purpose and are left alone.

TENANT SCOPING
--------------
Every candidate lookup is filtered to the row's own tenant_id before any
comparison. A value that names a user in a different tenant does not match —
it is an UNRESOLVED skip, never a cross-tenant assignment. Production has
'NIBU S S' as a username in four separate tenants, so this is not theoretical.

SKIPPING IS A FEATURE
---------------------
Production contains assigned_staff='Anju_display' (lead id 4), which matches no
username, display_name or email anywhere in the system. The approved operator
decision is to leave it NULL. This module never guesses: an unknown value is
recorded and skipped, and one unknown value never fails its tenant.

IDEMPOTENT
----------
A row whose assigned_user_id is already populated is counted as
already_populated and skipped. Re-running --live performs zero writes.

PER-TENANT TRANSACTIONS
-----------------------
Each tenant is committed independently, so a failure in one cannot roll back
another's completed work — the same contract as tenant_provisioning_service.
"""
import logging

logger = logging.getLogger(__name__)


class TenantBackfillReport:
    """What the backfill did for one tenant, per table."""

    TABLES = ("conversation_state", "tasks")

    def __init__(self, tenant_id):
        self.tenant_id = tenant_id
        # {table: {"resolved": n, "skipped": n, "already": n}}
        self.counts = {t: {"resolved": 0, "skipped": 0, "already": 0}
                       for t in self.TABLES}
        # [(table, row_id, raw_value, reason)]
        self.skipped_rows = []
        self.errors = []

    def record(self, table, outcome):
        self.counts[table][outcome] += 1

    def skip(self, table, row_id, value, reason):
        self.counts[table]["skipped"] += 1
        self.skipped_rows.append((table, row_id, value, reason))

    @property
    def resolved(self):
        return sum(c["resolved"] for c in self.counts.values())

    @property
    def skipped(self):
        return sum(c["skipped"] for c in self.counts.values())

    @property
    def already(self):
        return sum(c["already"] for c in self.counts.values())

    @property
    def changed(self):
        return self.resolved > 0

    def as_dict(self):
        return {
            "tenant_id": self.tenant_id,
            "counts": self.counts,
            "resolved": self.resolved,
            "skipped": self.skipped,
            "already_populated": self.already,
            "skipped_rows": self.skipped_rows,
            "errors": self.errors,
        }

    def __repr__(self):
        return (f"<TenantBackfillReport {self.tenant_id} resolved={self.resolved} "
                f"skipped={self.skipped} already={self.already}>")


def _candidates(tenant_id):
    """Every User in ONE tenant. The only source of resolution targets.

    Tenant filtering happens HERE, before any comparison, so a cross-tenant
    match is not merely rejected later — it is never a candidate.
    """
    from app.models import User
    return User.query.filter(User.tenant_id == tenant_id).all()


def resolve_user_id(tenant_id, value, candidates=None):
    """Resolve one assigned_staff string to a User.id within ONE tenant.

    Returns (user_id, rule) on success, or (None, reason) on failure.

    Strict precedence — see the module docstring. Each rule requires EXACTLY
    one match; two matches under the same rule is an ambiguity and is refused
    rather than resolved, because guessing here reassigns a real customer's
    lead. username is unique per tenant by constraint, but display_name carries
    no uniqueness guarantee.
    """
    if not tenant_id:
        return None, "no tenant context"
    raw = value or ""
    if not raw.strip():
        return None, "blank value"

    pool = _candidates(tenant_id) if candidates is None else candidates
    trimmed = raw.strip()
    lowered = trimmed.lower()

    # 1 — exact username (case-sensitive)
    hits = [u for u in pool if (u.username or "").strip() == trimmed]
    if len(hits) == 1:
        return hits[0].id, "exact username"
    if len(hits) > 1:
        return None, "ambiguous: multiple exact username matches"

    # 2 — case-insensitive username
    hits = [u for u in pool if (u.username or "").strip().lower() == lowered]
    if len(hits) == 1:
        return hits[0].id, "case-insensitive username"
    if len(hits) > 1:
        return None, "ambiguous: multiple username matches"

    # 3 — case-insensitive display_name
    hits = [u for u in pool if (u.display_name or "").strip().lower() == lowered]
    if len(hits) == 1:
        return hits[0].id, "case-insensitive display_name"
    if len(hits) > 1:
        return None, "ambiguous: multiple display_name matches"

    return None, "no match in this tenant"


def _backfill_table(model, table_name, tenant_id, report, candidates, dry_run):
    """Resolve and (unless dry_run) set assigned_user_id for one model."""
    rows = (model.query
            .filter(model.tenant_id == tenant_id)
            .filter(model.assigned_staff.isnot(None))
            .filter(model.assigned_staff != "")
            .order_by(model.id)
            .all())

    for row in rows:
        if row.assigned_user_id is not None:
            report.record(table_name, "already")
            continue

        user_id, rule = resolve_user_id(tenant_id, row.assigned_staff,
                                        candidates=candidates)
        if user_id is None:
            report.skip(table_name, row.id, row.assigned_staff, rule)
            logger.info("backfill skip %s id=%s tenant=%s value=%r reason=%s",
                        table_name, row.id, tenant_id, row.assigned_staff, rule)
            continue

        report.record(table_name, "resolved")
        if not dry_run:
            # The ONLY write this module performs. assigned_staff is left
            # exactly as it is, which is what keeps rollback lossless.
            row.assigned_user_id = user_id


def backfill_tenant(tenant_id, dry_run=True):
    """Backfill one tenant. Commits at the end unless dry_run.

    Never raises for an unresolvable value — that is a skip, not an error, so a
    single bad string cannot abandon the rest of the tenant.
    """
    from app.models import ConversationState, Task
    from app.extensions import db

    report = TenantBackfillReport(tenant_id)
    if not tenant_id:
        report.errors.append("missing tenant_id")
        return report

    candidates = _candidates(tenant_id)
    _backfill_table(ConversationState, "conversation_state", tenant_id,
                    report, candidates, dry_run)
    _backfill_table(Task, "tasks", tenant_id, report, candidates, dry_run)

    if not dry_run:
        db.session.commit()
    else:
        # Dry run must leave the session exactly as it found it. Nothing was
        # assigned above, but expunging makes that guarantee explicit rather
        # than incidental.
        db.session.rollback()
    return report


def backfill_all_tenants(dry_run=True):
    """Backfill every tenant. Returns [TenantBackfillReport].

    Each tenant is its own unit of work: a failure is caught, rolled back and
    recorded, and the next tenant proceeds. One tenant's error can never
    discard another tenant's completed backfill.
    """
    from app.models import Tenant
    from app.extensions import db

    reports = []
    for tenant in Tenant.query.order_by(Tenant.created_at).all():
        try:
            reports.append(backfill_tenant(tenant.id, dry_run=dry_run))
        except Exception as exc:                      # noqa: BLE001
            db.session.rollback()
            report = TenantBackfillReport(tenant.id)
            report.errors.append(str(exc))
            reports.append(report)
            logger.exception("Staff backfill failed for tenant %s", tenant.id)
    return reports
