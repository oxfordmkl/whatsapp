"""Phase 10.9B — Sales Transition Engine.

Answers one question: may this lead move from status A to status B?

Returns a verdict. Never mutates, never commits, never writes an audit row,
never raises on a business rejection. Deciding what a rejection *means* belongs
to the caller — the CSV importer needs a per-row error, a form path needs a
flash message, and a warn-only rollout needs neither. This mirrors the
record_stage_change() contract in sales_pipeline_service.

NOT WIRED INTO ANYTHING. Phase 10.9B.1 delivers the engine alone: no route
integration, no enforcement, no warn-only logging. Those are 10.9B.2+.

Framework independence
----------------------
No flask import, direct or deferred. Callers pass tenant_id and the actor's
override decision explicitly; this module never reads `request`, `session` or
`current_user`. sales_pipeline_service sets the precedent (it imports only
logging) and the same discipline applies here, one step further: stage metadata
arrives through an injectable provider, so the rule logic is testable with no
database at all.

Rules are DERIVED, not enumerated
---------------------------------
Every sales stage row already carries order_index, stage_category, is_entry and
is_terminal, per tenant — 100 stage rows across 10 tenants in production. A
hardcoded matrix would duplicate data the database already owns, and the moment
one tenant renames or reorders a stage the literal and the rows disagree, with
the literal silently winning. Phase 10.4 rejected a hardcoded status tuple for
this reason; stages are already per-tenant rows, so the argument is stronger
here.

Why the matrix is permissive
----------------------------
lead_stage_history contains ONE operator transition (Lead -> Contacted). A
10x10 matrix of "legitimate" paths would be invention, not design: a wrong
"allowed" cell is invisible, but a wrong "denied" cell blocks a real sale at the
moment someone is trying to close it.

So there is exactly one hard rule — you may not LEAVE a terminal stage without
an override — and everything else is allowed. Backward movement is allowed and
merely flagged (severity "warn"), because deals genuinely regress and the point
of this phase is to MEASURE movement, not to prevent it. When the history table
can justify more, the structure here carries a richer matrix without change.

Deliberately NOT enforced in the model setter
---------------------------------------------
ConversationState.lead_status's setter is the single choke point every write
passes through, which makes it look like the ideal enforcement site. It is the
wrong one: the seeder, the WhatsApp inbound path (state.py) and the admission
auto-promotion all flow through that setter and none of them is an operator
transition. Enforcing there would break the seeder and could stop a WhatsApp
lead being created at all. Enforcement belongs at operator entry points.
"""
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ── Contexts ─────────────────────────────────────────────────────────────────
#
# WHERE a transition came from. Not a role and not an actor: STAFF and ADMIN
# both submit OPERATOR_FORM, and what separates them is the `override`
# parameter, which the caller decides.
CONTEXT_OPERATOR_FORM = "operator_form"     # crm_lead_update
CONTEXT_OPERATOR_MOVE = "operator_move"     # crm_lead_move_stage
CONTEXT_CSV_IMPORT = "csv_import"           # crm_leads_import
CONTEXT_AUTO_ADMISSION = "auto_admission"   # is_admitted -> Enrolled promotion
CONTEXT_BOT_INBOUND = "bot_inbound"         # state.py lead creation
CONTEXT_SEEDER = "seeder"                   # sales_pipeline_seed backfill

# Contexts no operator initiated. A rule that blocks admission is a rule that
# costs revenue, and a rule that blocks the bot stops leads being created at
# all — neither is a transition anyone chose to make.
EXEMPT_CONTEXTS = frozenset({
    CONTEXT_AUTO_ADMISSION,
    CONTEXT_BOT_INBOUND,
    CONTEXT_SEEDER,
})

VALID_CONTEXTS = frozenset({
    CONTEXT_OPERATOR_FORM,
    CONTEXT_OPERATOR_MOVE,
    CONTEXT_CSV_IMPORT,
}) | EXEMPT_CONTEXTS


# ── Verdict codes ────────────────────────────────────────────────────────────
CODE_UNKNOWN_TARGET = "BLOCKED_UNKNOWN_TARGET"
CODE_TERMINAL_EXIT = "BLOCKED_TERMINAL_EXIT"
CODE_NOOP = "OK_NOOP"
CODE_UNKNOWN_SOURCE = "OK_UNKNOWN_SOURCE"
CODE_FORWARD = "OK_FORWARD"
CODE_REGRESSION = "OK_REGRESSION"
CODE_EXEMPT = "OK_EXEMPT"
CODE_OVERRIDE = "OK_OVERRIDE"

ALL_CODES = frozenset({
    CODE_UNKNOWN_TARGET, CODE_TERMINAL_EXIT, CODE_NOOP, CODE_UNKNOWN_SOURCE,
    CODE_FORWARD, CODE_REGRESSION, CODE_EXEMPT, CODE_OVERRIDE,
})

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_BLOCK = "block"


# ── Rule identifiers ─────────────────────────────────────────────────────────
#
# Stable ids, quoted in verdicts so a log line or audit detail names the exact
# rule that fired rather than paraphrasing it. Numbered in EVALUATION order.
#
# Note one deliberate departure from the 10.9B.1 discovery document, which
# listed unknown-SOURCE before unknown-TARGET. Evaluating it in that order means
# a legacy row (unknown source) would be allowed to move to a garbage target,
# because the first match wins. Target validity is therefore checked first: a
# value with no stage in this tenant's pipeline is never writable, whatever the
# lead currently holds.
RULE_UNKNOWN_TARGET = "R1_UNKNOWN_TARGET"
RULE_NOOP = "R2_NOOP"
RULE_UNKNOWN_SOURCE = "R3_UNKNOWN_SOURCE"
RULE_TERMINAL_EXIT = "R4_TERMINAL_EXIT"
RULE_FORWARD = "R5_FORWARD"
RULE_REGRESSION = "R6_REGRESSION"
RULE_EXEMPT = "R7_EXEMPT"
RULE_OVERRIDE = "R8_OVERRIDE"

ALL_RULES = frozenset({
    RULE_UNKNOWN_TARGET, RULE_NOOP, RULE_UNKNOWN_SOURCE, RULE_TERMINAL_EXIT,
    RULE_FORWARD, RULE_REGRESSION, RULE_EXEMPT, RULE_OVERRIDE,
})


# ── Value objects ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StageInfo:
    """One sales stage, flattened away from the ORM.

    A plain value object rather than a PipelineStage instance so the rules can
    be exercised with no database, no app context and no SQLAlchemy session.
    That is what makes the full matrix testable in milliseconds instead of one
    Postgres round-trip per cell.
    """
    stage_id: Optional[int]
    internal_key: str
    display_name: str
    stage_category: str          # 'open' | 'won' | 'lost'
    order_index: int
    is_entry: bool = False
    is_terminal: bool = False

    @property
    def is_won(self) -> bool:
        return (self.stage_category or "").strip().lower() == "won"

    @property
    def is_lost(self) -> bool:
        return (self.stage_category or "").strip().lower() == "lost"


@dataclass(frozen=True)
class TransitionVerdict:
    """The engine's answer.

    Structured rather than a bare boolean, and that is what makes a warn-only
    rollout possible: the same call site can record `code` while ignoring
    `allowed` entirely, so observability arrives before enforcement does.

    `overridden_rule` is set only when an override or an exemption actually
    changed the outcome — never when the transition was permitted anyway. An
    audit trail claiming an override on a move that needed none is worse than
    no trail at all, because it makes real overrides unfindable.
    """
    allowed: bool
    code: str
    rule_id: str
    reason: str
    severity: str = SEVERITY_OK
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    from_stage_id: Optional[int] = None
    to_stage_id: Optional[int] = None
    overridden_rule: Optional[str] = None

    def __bool__(self) -> bool:
        """Truthiness is `allowed`, so `if can_transition(...)` reads naturally."""
        return bool(self.allowed)


# ── Stage provider ───────────────────────────────────────────────────────────

def _default_stage_provider(tenant_id):
    """Load this tenant's sales stages as StageInfo, ordered by order_index.

    The ONLY database access in this module, isolated here so every rule
    function stays pure. Imports are deferred to keep module import free of
    app/ORM dependencies, matching the convention in sales_pipeline_service.

    Returns [] rather than raising when the tenant has no sales pipeline: a
    tenant that has never been seeded must degrade to "no opinion", not to a
    500. Callers see every transition allowed, which is exactly today's
    behaviour for such a tenant.
    """
    from app.models import PipelineDefinition, PipelineStage, SALES_PIPELINE_KEY
    from app.extensions import db

    if not tenant_id:
        return []

    rows = (
        db.session.query(PipelineStage)
        .join(PipelineDefinition, PipelineStage.pipeline_id == PipelineDefinition.id)
        .filter(
            PipelineDefinition.tenant_id == tenant_id,
            PipelineDefinition.internal_key == SALES_PIPELINE_KEY,
        )
        .order_by(PipelineStage.order_index)
        .all()
    )
    return [
        StageInfo(
            stage_id=r.id,
            internal_key=r.internal_key,
            display_name=r.display_name,
            stage_category=r.stage_category,
            order_index=r.order_index,
            is_entry=bool(r.is_entry),
            is_terminal=bool(r.is_terminal),
        )
        for r in rows
    ]


def _load_stages(tenant_id, stage_provider):
    provider = stage_provider or _default_stage_provider
    try:
        return list(provider(tenant_id) or [])
    except Exception:
        # A stage-metadata failure must not surface as a failed lead edit. With
        # no stages the engine has no opinion and allows everything, which is
        # pre-10.9B behaviour — the safe direction to fail in.
        logger.exception("Sales transition: could not load stages for tenant %s",
                         tenant_id)
        return []


def _match(stages, status):
    """Resolve a status string to its stage, or None.

    Case-insensitive on trimmed display_name — deliberately identical to
    ConversationState._sync_sales_stage_link, which resolves sales_stage_id the
    same way. If the two disagreed, the engine could allow a transition the
    model then failed to link, or block one the model would have accepted.
    """
    value = (status or "").strip().lower()
    if not value:
        return None
    for stage in stages:
        if (stage.display_name or "").strip().lower() == value:
            return stage
    return None


# ── The rules ────────────────────────────────────────────────────────────────

def _evaluate(from_stage, to_stage, from_status, to_status):
    """The base matrix, before exemptions and overrides are considered.

    Split out so exemption and override can be applied as bypasses of a
    concrete rule — the engine knows WHAT was bypassed, not merely that
    something was.
    """
    def verdict(allowed, code, rule_id, reason, severity=SEVERITY_OK):
        return TransitionVerdict(
            allowed=allowed, code=code, rule_id=rule_id, reason=reason,
            severity=severity,
            from_status=from_status, to_status=to_status,
            from_stage_id=from_stage.stage_id if from_stage else None,
            to_stage_id=to_stage.stage_id if to_stage else None,
        )

    # R1 — the target must be a real stage of THIS tenant's sales pipeline.
    # Checked first: an unresolvable status has no stage, so the model adapter
    # would clear sales_stage_id and drop the lead out of the pipeline. Phase
    # 10.9A blocks that at the form paths; this states the same rule as data.
    #
    # A tenant with no stages at all yields no opinion (handled by the caller),
    # so this cannot fire for an unseeded tenant.
    if to_stage is None:
        return verdict(
            False, CODE_UNKNOWN_TARGET, RULE_UNKNOWN_TARGET,
            f"{to_status!r} is not a stage in this pipeline.",
            severity=SEVERITY_BLOCK,
        )

    # R2 — same stage. Not an error; simply nothing to do.
    if from_stage is not None and from_stage.stage_id == to_stage.stage_id:
        return verdict(True, CODE_NOOP, RULE_NOOP,
                       f"Lead is already in {to_stage.display_name}.")

    # R3 — the lead holds a legacy or unrecognised status. It MUST stay
    # correctable: refusing to move a row out of a bad value would strand it
    # there permanently, which is the opposite of what validation is for.
    if from_stage is None:
        return verdict(
            True, CODE_UNKNOWN_SOURCE, RULE_UNKNOWN_SOURCE,
            f"Current status {from_status!r} is not a known stage; "
            f"moving to {to_stage.display_name} is always permitted.",
        )

    # R4 — the one hard rule. Leaving a closed deal is a re-opening: a Lost
    # lead who returns, or a mis-clicked Enrolled. Both are legitimate, which
    # is why this is override-gated rather than forbidden.
    #
    # Terminality is read from the stage row, NOT from LEAD_TERMINAL_STATUSES.
    # That frozenset is a reporting concept (who is excluded from workload) and
    # includes the legacy value "Dropped", which has no stage row at all —
    # conflating them would make Dropped both unreachable and unexitable,
    # stranding any row still carrying it.
    if from_stage.is_terminal:
        return verdict(
            False, CODE_TERMINAL_EXIT, RULE_TERMINAL_EXIT,
            f"{from_stage.display_name} is a closed stage; re-opening this "
            f"lead requires an administrator.",
            severity=SEVERITY_BLOCK,
        )

    # R5 — forward. Skipping stages is allowed on purpose: a walk-in who
    # enrols on the spot goes Lead -> Enrolled, and blocking that would be
    # actively wrong.
    if to_stage.order_index > from_stage.order_index:
        return verdict(True, CODE_FORWARD, RULE_FORWARD,
                       f"{from_stage.display_name} -> {to_stage.display_name}.")

    # R6 — backward. Allowed, because deals genuinely regress: a Negotiation
    # lead that goes quiet belongs back in Contacted. Carries severity "warn"
    # so regressions become measurable without being prevented — if the history
    # later shows a pattern of accidental ones, that is the evidence needed to
    # tighten this rule. Measure first, restrict second.
    return verdict(
        True, CODE_REGRESSION, RULE_REGRESSION,
        f"{from_stage.display_name} -> {to_stage.display_name} moves the lead "
        f"backwards.",
        severity=SEVERITY_WARN,
    )


# ── Public interface ─────────────────────────────────────────────────────────

def can_transition(from_status, to_status, tenant_id=None,
                   context=CONTEXT_OPERATOR_FORM, override=False,
                   stage_provider=None):
    """May this lead move from `from_status` to `to_status`?

    Returns a TransitionVerdict; never raises for a business rejection, and
    never mutates anything.

    Parameters
    ----------
    from_status : str or None
        The lead's CURRENT status. None/blank/unrecognised is supported and
        always permitted to move (R3) — legacy rows must stay correctable.
    to_status : str
        The proposed status. Matched case-insensitively against this tenant's
        stage display names, exactly as the model adapter matches.
    tenant_id : str
        Required to resolve stages. Rules are derived per tenant, so a tenant
        that renames or reorders its stages gets rules matching ITS pipeline.
    context : str
        One of the CONTEXT_* constants. EXEMPT_CONTEXTS bypass a block.
    override : bool
        An explicit administrator override. The CALLER decides who may set
        this — the engine imports no Flask and cannot read a role. It bypasses
        a block and is always reported, so an override is never silent.
    stage_provider : callable, optional
        tenant_id -> [StageInfo]. Injected by tests to run with no database.

    Override and exemption are applied only when the base rule actually
    BLOCKED. A transition that was permitted anyway reports the rule that
    permitted it, so the audit trail never claims an override that changed
    nothing.
    """
    stages = _load_stages(tenant_id, stage_provider)

    # No sales pipeline for this tenant → no opinion. This is pre-10.9B
    # behaviour and the safe direction to fail in: an unseeded tenant must
    # still be able to work its leads.
    if not stages:
        return TransitionVerdict(
            allowed=True, code=CODE_UNKNOWN_SOURCE, rule_id=RULE_UNKNOWN_SOURCE,
            reason="No sales pipeline is configured for this tenant; "
                   "transition rules do not apply.",
            severity=SEVERITY_OK,
            from_status=from_status, to_status=to_status,
        )

    from_stage = _match(stages, from_status)
    to_stage = _match(stages, to_status)

    base = _evaluate(from_stage, to_stage, from_status, to_status)
    if base.allowed:
        return base

    # ── Bypasses, in precedence order ────────────────────────────────────────
    #
    # An unknown TARGET is never bypassable. Writing a status with no stage
    # clears sales_stage_id and drops the lead out of the pipeline; no context
    # and no override makes that a good outcome, and permitting it would undo
    # the coverage guarantee Phase 10.8C.3 and 10.9A established.
    if base.rule_id == RULE_UNKNOWN_TARGET:
        return base

    if context in EXEMPT_CONTEXTS:
        return TransitionVerdict(
            allowed=True, code=CODE_EXEMPT, rule_id=RULE_EXEMPT,
            reason=f"Permitted: {context} is a system action, not an operator "
                   f"transition (bypassed {base.rule_id}).",
            severity=SEVERITY_OK,
            from_status=from_status, to_status=to_status,
            from_stage_id=base.from_stage_id, to_stage_id=base.to_stage_id,
            overridden_rule=base.rule_id,
        )

    if override:
        return TransitionVerdict(
            allowed=True, code=CODE_OVERRIDE, rule_id=RULE_OVERRIDE,
            reason=f"Permitted by administrator override "
                   f"(bypassed {base.rule_id}).",
            severity=SEVERITY_WARN,
            from_status=from_status, to_status=to_status,
            from_stage_id=base.from_stage_id, to_stage_id=base.to_stage_id,
            overridden_rule=base.rule_id,
        )

    return base


def describe_allowed_transitions(from_status, tenant_id=None,
                                 context=CONTEXT_OPERATOR_FORM, override=False,
                                 stage_provider=None):
    """Which statuses may this lead move to, in pipeline order?

    Exists so a UI can eventually present only reachable options rather than
    letting an operator choose something that will be rejected on submit — a
    rule the operator cannot see is a rule that reads as a bug.

    Returns a list of display names, ordered by order_index. The current stage
    is included when it resolves (moving to it is a permitted no-op).
    """
    stages = _load_stages(tenant_id, stage_provider)
    allowed = []
    for stage in sorted(stages, key=lambda s: s.order_index):
        verdict = can_transition(
            from_status, stage.display_name, tenant_id=tenant_id,
            context=context, override=override,
            stage_provider=stage_provider or (lambda _t, _s=stages: _s),
        )
        if verdict.allowed:
            allowed.append(stage.display_name)
    return allowed


def transition_matrix(tenant_id=None, context=CONTEXT_OPERATOR_FORM,
                      override=False, stage_provider=None):
    """The full derived matrix: {from_display_name: {to_display_name: code}}.

    Makes the derivation inspectable rather than implicit — for tests, and for
    an eventual admin-facing "what are the rules here" view. Since the rules
    are derived from stage rows, this is the only honest way to answer that
    question for a tenant that has customised its pipeline.
    """
    stages = sorted(_load_stages(tenant_id, stage_provider),
                    key=lambda s: s.order_index)
    frozen = (lambda _t, _s=stages: _s)
    return {
        src.display_name: {
            dst.display_name: can_transition(
                src.display_name, dst.display_name, tenant_id=tenant_id,
                context=context, override=override, stage_provider=frozen,
            ).code
            for dst in stages
        }
        for src in stages
    }
