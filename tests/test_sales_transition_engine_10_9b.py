"""Phase 10.9B.1 — Sales Transition Engine.

The engine is deliberately NOT wired into anything yet, so these tests are the
only thing standing behind it. They are organised in two tiers:

  Tier 1 (the bulk) — pure rules with an INJECTED stage provider. No database,
  no app context, no SQLAlchemy session. This is what makes asserting the full
  matrix cell-by-cell affordable rather than sampling a few pairs.

  Tier 2 — real models against a real database, proving that the default
  provider derives the same rules from actual PipelineStage rows, and that two
  tenants with DIFFERENT pipelines get different rules.

Import isolation follows test_pipeline_foundation_10_6.py; see that file for
the note on tests/conftest.py stubbing `app` at collection time. Tier 1 needs
none of it, but the module is shared.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_10_9b_test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("ADMIN_KEY", "testkey")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("SECRET_KEY", "testsecret")
os.environ.setdefault("BROADCAST_API_KEY", "testbroadcast")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app.services import sales_transition_service as sts                # noqa: E402
from app.services.sales_transition_service import (                     # noqa: E402
    StageInfo, TransitionVerdict, can_transition,
    describe_allowed_transitions, transition_matrix,
    CONTEXT_OPERATOR_FORM, CONTEXT_OPERATOR_MOVE, CONTEXT_CSV_IMPORT,
    CONTEXT_AUTO_ADMISSION, CONTEXT_BOT_INBOUND, CONTEXT_SEEDER,
    EXEMPT_CONTEXTS, VALID_CONTEXTS, ALL_CODES, ALL_RULES,
    CODE_UNKNOWN_TARGET, CODE_TERMINAL_EXIT, CODE_NOOP, CODE_UNKNOWN_SOURCE,
    CODE_FORWARD, CODE_REGRESSION, CODE_EXEMPT, CODE_OVERRIDE,
    RULE_UNKNOWN_TARGET, RULE_TERMINAL_EXIT, RULE_REGRESSION, RULE_FORWARD,
    SEVERITY_OK, SEVERITY_WARN, SEVERITY_BLOCK,
)

TENANT = "t-10-9b"

# The production pipeline, as seeded for all 10 tenants.
STANDARD = [
    StageInfo(1,  "lead",           "Lead",           "open", 0, is_entry=True),
    StageInfo(2,  "contacted",      "Contacted",      "open", 1),
    StageInfo(3,  "interested",     "Interested",     "open", 2),
    StageInfo(4,  "demo_scheduled", "Demo Scheduled", "open", 3),
    StageInfo(5,  "demo_done",      "Demo Done",      "open", 4),
    StageInfo(6,  "proposal_sent",  "Proposal Sent",  "open", 5),
    StageInfo(7,  "negotiation",    "Negotiation",    "open", 6),
    StageInfo(8,  "enrolled",       "Enrolled",       "won",  7, is_terminal=True),
    StageInfo(9,  "lost",           "Lost",           "lost", 8, is_terminal=True),
    StageInfo(10, "not_interested", "Not Interested", "lost", 9, is_terminal=True),
]

OPEN = [s.display_name for s in STANDARD if not s.is_terminal]
TERMINAL = [s.display_name for s in STANDARD if s.is_terminal]
NAMES = [s.display_name for s in STANDARD]


def provider(stages=STANDARD):
    """An injected stage provider — the reason Tier 1 needs no database."""
    return lambda _tenant_id: stages


def verdict(frm, to, **kw):
    kw.setdefault("tenant_id", TENANT)
    kw.setdefault("stage_provider", provider())
    return can_transition(frm, to, **kw)


# ═══ Tier 1 — pure rules ═════════════════════════════════════════════════════

class TestVerdictShape:
    def test_returns_a_transition_verdict(self):
        assert isinstance(verdict("Lead", "Contacted"), TransitionVerdict)

    def test_verdict_is_truthy_when_allowed(self):
        assert verdict("Lead", "Contacted")
        assert not verdict("Enrolled", "Lead")

    def test_rule_id_is_always_a_declared_rule(self):
        for frm in NAMES + ["fresh", None, ""]:
            for to in NAMES + ["bogus"]:
                v = verdict(frm, to)
                assert v.rule_id in ALL_RULES, f"{frm}->{to} gave {v.rule_id}"

    def test_code_is_always_a_declared_code(self):
        for frm in NAMES + ["fresh", None]:
            for to in NAMES + ["bogus"]:
                assert verdict(frm, to).code in ALL_CODES

    def test_severity_is_always_declared(self):
        for frm in NAMES + ["fresh"]:
            for to in NAMES + ["bogus"]:
                assert verdict(frm, to).severity in {
                    SEVERITY_OK, SEVERITY_WARN, SEVERITY_BLOCK}

    def test_blocked_verdicts_carry_block_severity(self):
        for frm in NAMES + ["fresh"]:
            for to in NAMES + ["bogus"]:
                v = verdict(frm, to)
                if not v.allowed:
                    assert v.severity == SEVERITY_BLOCK

    def test_verdict_carries_the_statuses_and_stage_ids(self):
        v = verdict("Lead", "Interested")
        assert v.from_status == "Lead" and v.to_status == "Interested"
        assert v.from_stage_id == 1 and v.to_stage_id == 3

    def test_verdict_is_immutable(self):
        v = verdict("Lead", "Contacted")
        with pytest.raises(Exception):
            v.allowed = False

    def test_reason_is_a_non_empty_sentence(self):
        for frm in NAMES + ["fresh"]:
            for to in NAMES + ["bogus"]:
                assert verdict(frm, to).reason.strip()


class TestForwardAndRegression:
    @pytest.mark.parametrize("frm,to", [
        ("Lead", "Contacted"), ("Contacted", "Interested"),
        ("Interested", "Demo Scheduled"), ("Negotiation", "Enrolled"),
    ])
    def test_adjacent_forward_allowed(self, frm, to):
        v = verdict(frm, to)
        assert v.allowed and v.code == CODE_FORWARD
        assert v.severity == SEVERITY_OK

    def test_skipping_stages_is_allowed(self):
        """A walk-in who enrols on the spot. Blocking this would be wrong."""
        v = verdict("Lead", "Enrolled")
        assert v.allowed and v.code == CODE_FORWARD

    @pytest.mark.parametrize("frm,to", [
        ("Contacted", "Lead"), ("Negotiation", "Contacted"),
        ("Demo Done", "Interested"),
    ])
    def test_backward_is_allowed_but_warns(self, frm, to):
        v = verdict(frm, to)
        assert v.allowed, "deals genuinely regress — must not be blocked"
        assert v.code == CODE_REGRESSION
        assert v.severity == SEVERITY_WARN, "regression must be measurable"

    def test_open_to_open_is_never_blocked_in_either_direction(self):
        for frm in OPEN:
            for to in OPEN:
                assert verdict(frm, to).allowed, f"{frm} -> {to}"

    def test_entering_a_terminal_stage_is_always_allowed(self):
        """Closing a deal must never require permission."""
        for frm in OPEN:
            for to in TERMINAL:
                assert verdict(frm, to).allowed, f"{frm} -> {to}"


class TestNoop:
    @pytest.mark.parametrize("name", NAMES)
    def test_same_stage_is_a_permitted_noop(self, name):
        v = verdict(name, name)
        assert v.allowed and v.code == CODE_NOOP

    def test_noop_applies_to_terminal_stages_too(self):
        """Enrolled -> Enrolled must not read as an illegal terminal exit."""
        v = verdict("Enrolled", "Enrolled")
        assert v.allowed and v.code == CODE_NOOP

    def test_case_variant_of_the_same_stage_is_a_noop(self):
        assert verdict("lead", "LEAD").code == CODE_NOOP


class TestUnknownAndLegacyStatus:
    @pytest.mark.parametrize("legacy", ["fresh", "Dropped", "bogus", "won", "-"])
    def test_unknown_source_may_always_move(self, legacy):
        """A legacy row must stay correctable — refusing to move it out of a
        bad value would strand it there permanently."""
        for to in NAMES:
            v = verdict(legacy, to)
            assert v.allowed, f"{legacy} -> {to}"
            assert v.code == CODE_UNKNOWN_SOURCE

    @pytest.mark.parametrize("blank", [None, "", "   "])
    def test_blank_source_may_always_move(self, blank):
        v = verdict(blank, "Contacted")
        assert v.allowed and v.code == CODE_UNKNOWN_SOURCE

    def test_unknown_source_may_move_into_a_terminal_stage(self):
        assert verdict("fresh", "Lost").allowed

    @pytest.mark.parametrize("bad", ["fresh", "bogus", "Dropped", "", None])
    def test_unknown_target_is_blocked(self, bad):
        v = verdict("Lead", bad)
        assert not v.allowed
        assert v.code == CODE_UNKNOWN_TARGET

    def test_unknown_target_blocked_even_from_unknown_source(self):
        """Target validity is checked FIRST — otherwise a legacy row could be
        moved to a garbage value, clearing sales_stage_id."""
        v = verdict("fresh", "alsofresh")
        assert not v.allowed and v.code == CODE_UNKNOWN_TARGET

    def test_target_matching_is_case_and_whitespace_insensitive(self):
        """Must match the model adapter, or the engine could allow a
        transition the model then fails to link."""
        for raw in ("contacted", "CONTACTED", "  Contacted  "):
            assert verdict("Lead", raw).allowed


class TestTerminalStages:
    @pytest.mark.parametrize("frm", TERMINAL)
    def test_leaving_a_terminal_stage_is_blocked(self, frm):
        for to in OPEN:
            v = verdict(frm, to)
            assert not v.allowed, f"{frm} -> {to}"
            assert v.code == CODE_TERMINAL_EXIT
            assert v.rule_id == RULE_TERMINAL_EXIT

    def test_terminal_to_terminal_is_blocked(self):
        """Enrolled -> Lost is still leaving a closed stage."""
        v = verdict("Enrolled", "Lost")
        assert not v.allowed and v.code == CODE_TERMINAL_EXIT

    def test_terminality_comes_from_the_stage_row_not_the_frozenset(self):
        """LEAD_TERMINAL_STATUSES contains the legacy value "Dropped", which
        has no stage row. Reading terminality from that frozenset would make
        Dropped both unreachable and unexitable."""
        from app.models import LEAD_TERMINAL_STATUSES
        assert "Dropped" in LEAD_TERMINAL_STATUSES
        assert "Dropped" not in NAMES
        assert verdict("Dropped", "Lead").allowed, "must stay correctable"

    def test_a_stage_marked_terminal_by_data_is_treated_as_terminal(self):
        """Derivation, not hardcoding: mark an ordinary stage terminal and the
        rule follows the data."""
        custom = [
            StageInfo(1, "lead", "Lead", "open", 0, is_entry=True),
            StageInfo(2, "parked", "Parked", "open", 1, is_terminal=True),
            StageInfo(3, "won", "Won", "won", 2, is_terminal=True),
        ]
        v = can_transition("Parked", "Lead", tenant_id=TENANT,
                           stage_provider=provider(custom))
        assert not v.allowed and v.code == CODE_TERMINAL_EXIT

    def test_a_pipeline_with_no_terminal_stages_blocks_nothing(self):
        custom = [
            StageInfo(1, "a", "A", "open", 0, is_entry=True),
            StageInfo(2, "b", "B", "open", 1),
        ]
        for frm in ("A", "B"):
            for to in ("A", "B"):
                assert can_transition(frm, to, tenant_id=TENANT,
                                      stage_provider=provider(custom)).allowed


class TestOverride:
    def test_override_permits_a_terminal_exit(self):
        v = verdict("Enrolled", "Lead", override=True)
        assert v.allowed and v.code == CODE_OVERRIDE

    def test_override_records_what_it_bypassed(self):
        """An override that does not name the rule it bypassed is not an audit
        trail."""
        v = verdict("Enrolled", "Lead", override=True)
        assert v.overridden_rule == RULE_TERMINAL_EXIT

    def test_override_carries_warn_severity(self):
        assert verdict("Lost", "Contacted", override=True).severity == SEVERITY_WARN

    def test_override_is_NOT_reported_when_nothing_was_bypassed(self):
        """Claiming an override on a move that needed none makes the real
        overrides unfindable."""
        v = verdict("Lead", "Contacted", override=True)
        assert v.code == CODE_FORWARD
        assert v.overridden_rule is None

    def test_override_cannot_write_an_unknown_target(self):
        """No override makes clearing sales_stage_id a good outcome."""
        v = verdict("Lead", "fresh", override=True)
        assert not v.allowed and v.code == CODE_UNKNOWN_TARGET

    def test_override_defaults_to_false(self):
        assert not verdict("Enrolled", "Lead").allowed

    def test_engine_never_decides_who_may_override(self):
        """It is a parameter, not a role lookup — that is what keeps the
        module framework-free."""
        import inspect
        params = inspect.signature(can_transition).parameters
        assert "override" in params
        assert params["override"].default is False
        assert not any(p in params for p in ("actor", "user", "role", "request"))


class TestContextsAndExemptions:
    @pytest.mark.parametrize("ctx", sorted(EXEMPT_CONTEXTS))
    def test_exempt_contexts_bypass_a_terminal_block(self, ctx):
        v = verdict("Enrolled", "Lead", context=ctx)
        assert v.allowed and v.code == CODE_EXEMPT
        assert v.overridden_rule == RULE_TERMINAL_EXIT

    @pytest.mark.parametrize("ctx", [CONTEXT_OPERATOR_FORM,
                                     CONTEXT_OPERATOR_MOVE,
                                     CONTEXT_CSV_IMPORT])
    def test_operator_contexts_do_not_bypass(self, ctx):
        assert not verdict("Enrolled", "Lead", context=ctx).allowed

    def test_admission_promotion_is_never_blocked_from_any_open_stage(self):
        """A rule that blocks admission is a rule that costs revenue. Covers
        the four stages _PROMOTE_STATUSES omits (Demo Scheduled onward)."""
        for frm in OPEN:
            v = verdict(frm, "Enrolled", context=CONTEXT_AUTO_ADMISSION)
            assert v.allowed, f"admission blocked from {frm}"

    def test_admission_promotion_allowed_even_from_a_terminal_stage(self):
        v = verdict("Lost", "Enrolled", context=CONTEXT_AUTO_ADMISSION)
        assert v.allowed and v.code == CODE_EXEMPT

    def test_exempt_context_is_NOT_reported_when_nothing_was_bypassed(self):
        v = verdict("Lead", "Enrolled", context=CONTEXT_AUTO_ADMISSION)
        assert v.code == CODE_FORWARD
        assert v.overridden_rule is None

    def test_exempt_context_cannot_write_an_unknown_target(self):
        v = verdict("Lead", "fresh", context=CONTEXT_SEEDER)
        assert not v.allowed and v.code == CODE_UNKNOWN_TARGET

    def test_bot_and_seeder_contexts_exist_and_are_exempt(self):
        assert CONTEXT_BOT_INBOUND in EXEMPT_CONTEXTS
        assert CONTEXT_SEEDER in EXEMPT_CONTEXTS

    def test_every_exempt_context_is_a_valid_context(self):
        assert EXEMPT_CONTEXTS <= VALID_CONTEXTS

    def test_default_context_is_an_operator_context(self):
        import inspect
        default = inspect.signature(can_transition).parameters["context"].default
        assert default == CONTEXT_OPERATOR_FORM
        assert default not in EXEMPT_CONTEXTS, "default must not be exempt"


class TestUnseededTenant:
    def test_no_stages_means_no_opinion(self):
        """A tenant that has never been seeded must still work its leads."""
        for frm in ("Lead", "fresh", None):
            for to in ("Contacted", "anything"):
                assert can_transition(frm, to, tenant_id=TENANT,
                                      stage_provider=provider([])).allowed

    def test_a_failing_provider_fails_open(self):
        """A stage-metadata failure must not surface as a failed lead edit."""
        def boom(_tenant_id):
            raise RuntimeError("db down")
        assert can_transition("Enrolled", "Lead", tenant_id=TENANT,
                              stage_provider=boom).allowed

    def test_missing_tenant_id_yields_no_opinion(self):
        assert can_transition("Enrolled", "Lead", tenant_id=None,
                              stage_provider=provider([])).allowed


class TestDescribeAllowedTransitions:
    def test_open_stage_can_reach_everything(self):
        got = describe_allowed_transitions("Interested", tenant_id=TENANT,
                                           stage_provider=provider())
        assert got == NAMES

    def test_terminal_stage_can_reach_only_itself(self):
        got = describe_allowed_transitions("Enrolled", tenant_id=TENANT,
                                           stage_provider=provider())
        assert got == ["Enrolled"], "only the no-op survives"

    def test_override_reopens_everything_for_a_terminal_stage(self):
        got = describe_allowed_transitions("Enrolled", tenant_id=TENANT,
                                           override=True,
                                           stage_provider=provider())
        assert got == NAMES

    def test_unknown_status_can_reach_everything(self):
        got = describe_allowed_transitions("fresh", tenant_id=TENANT,
                                           stage_provider=provider())
        assert got == NAMES

    def test_results_are_in_pipeline_order(self):
        got = describe_allowed_transitions("Lead", tenant_id=TENANT,
                                           stage_provider=provider())
        assert got == sorted(
            got, key=lambda n: [s.display_name for s in
                                sorted(STANDARD, key=lambda x: x.order_index)].index(n))

    def test_unseeded_tenant_returns_empty(self):
        assert describe_allowed_transitions("Lead", tenant_id=TENANT,
                                            stage_provider=provider([])) == []

    def test_every_described_target_actually_passes_can_transition(self):
        for frm in NAMES + ["fresh"]:
            for to in describe_allowed_transitions(frm, tenant_id=TENANT,
                                                   stage_provider=provider()):
                assert verdict(frm, to).allowed, f"{frm} -> {to} disagrees"


class TestTransitionMatrix:
    def test_matrix_is_square_over_the_stages(self):
        m = transition_matrix(tenant_id=TENANT, stage_provider=provider())
        assert set(m) == set(NAMES)
        for row in m.values():
            assert set(row) == set(NAMES)

    def test_matrix_agrees_with_can_transition_in_every_cell(self):
        m = transition_matrix(tenant_id=TENANT, stage_provider=provider())
        for frm in NAMES:
            for to in NAMES:
                assert m[frm][to] == verdict(frm, to).code

    def test_matrix_terminal_rows_are_blocked_except_the_noop(self):
        m = transition_matrix(tenant_id=TENANT, stage_provider=provider())
        for frm in TERMINAL:
            for to in NAMES:
                expected = CODE_NOOP if to == frm else CODE_TERMINAL_EXIT
                assert m[frm][to] == expected

    def test_matrix_open_rows_are_never_blocked(self):
        m = transition_matrix(tenant_id=TENANT, stage_provider=provider())
        for frm in OPEN:
            for to in NAMES:
                assert m[frm][to] != CODE_TERMINAL_EXIT

    def test_exactly_one_hard_rule_fires_across_the_whole_matrix(self):
        """The engine's entire restrictive surface, stated as a number: of 100
        cells only terminal exits block. If a future change blocks anything
        else, this test says so."""
        m = transition_matrix(tenant_id=TENANT, stage_provider=provider())
        blocked = {(f, t) for f in NAMES for t in NAMES
                   if m[f][t] == CODE_TERMINAL_EXIT}
        codes = {m[f][t] for f in NAMES for t in NAMES}
        assert codes <= {CODE_NOOP, CODE_FORWARD, CODE_REGRESSION,
                         CODE_TERMINAL_EXIT}
        assert len(blocked) == len(TERMINAL) * (len(NAMES) - 1) == 27

    def test_unseeded_tenant_yields_an_empty_matrix(self):
        assert transition_matrix(tenant_id=TENANT,
                                 stage_provider=provider([])) == {}


class TestPerTenantDerivation:
    """Two tenants, two pipelines, two rule sets — from the same code."""

    CUSTOM = [
        StageInfo(21, "new", "New Enquiry", "open", 0, is_entry=True),
        StageInfo(22, "trial", "Trial Class", "open", 1),
        StageInfo(23, "signed", "Signed Up", "won", 2, is_terminal=True),
    ]

    def test_a_renamed_pipeline_gets_its_own_stage_names(self):
        got = describe_allowed_transitions("New Enquiry", tenant_id="other",
                                           stage_provider=provider(self.CUSTOM))
        assert got == ["New Enquiry", "Trial Class", "Signed Up"]

    def test_the_standard_pipelines_names_are_unknown_to_the_custom_tenant(self):
        v = can_transition("New Enquiry", "Negotiation", tenant_id="other",
                           stage_provider=provider(self.CUSTOM))
        assert not v.allowed and v.code == CODE_UNKNOWN_TARGET

    def test_terminal_rule_follows_the_custom_pipeline(self):
        v = can_transition("Signed Up", "Trial Class", tenant_id="other",
                           stage_provider=provider(self.CUSTOM))
        assert not v.allowed and v.code == CODE_TERMINAL_EXIT

    def test_reordering_stages_reverses_forward_and_regression(self):
        """Direction is derived from order_index, not from name or position in
        a literal."""
        reversed_order = [
            StageInfo(1, "a", "A", "open", 5, is_entry=True),
            StageInfo(2, "b", "B", "open", 1),
        ]
        v = can_transition("A", "B", tenant_id="other",
                           stage_provider=provider(reversed_order))
        assert v.code == CODE_REGRESSION
        v2 = can_transition("B", "A", tenant_id="other",
                            stage_provider=provider(reversed_order))
        assert v2.code == CODE_FORWARD

    def test_two_tenants_matrices_differ(self):
        a = transition_matrix(tenant_id=TENANT, stage_provider=provider())
        b = transition_matrix(tenant_id="other",
                              stage_provider=provider(self.CUSTOM))
        assert set(a) != set(b)


# ═══ Guard tests — the architectural contracts ═══════════════════════════════

SERVICE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "services", "sales_transition_service.py")


def _module_ast():
    with open(SERVICE_PATH, encoding="utf-8") as fh:
        return ast.parse(fh.read())


class TestFrameworkIndependence:
    def test_no_flask_import_anywhere(self):
        """AST, not string matching — the docstrings mention Flask on purpose,
        which is exactly the false positive that caught me in Phase 10.5."""
        offenders = []
        for node in ast.walk(_module_ast()):
            if isinstance(node, ast.Import):
                offenders += [a.name for a in node.names
                              if a.name.split(".")[0] in {"flask", "flask_login",
                                                          "werkzeug"}]
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in {"flask", "flask_login", "werkzeug"}:
                    offenders.append(node.module)
        assert offenders == [], f"framework imports found: {offenders}"

    def test_module_level_imports_are_stdlib_only(self):
        tree = _module_ast()
        top = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                top += [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                top.append(node.module.split(".")[0])
        assert set(top) <= {"logging", "dataclasses", "typing"}, top

    def test_no_request_or_current_user_references(self):
        names = {n.id for n in ast.walk(_module_ast()) if isinstance(n, ast.Name)}
        assert not names & {"request", "current_user", "session", "current_app", "g"}

    def test_importable_without_an_app_context(self):
        """It was imported at module scope above, outside any app context."""
        assert sts.can_transition is can_transition

    def test_pure_rules_need_no_database(self):
        assert verdict("Lead", "Enrolled").allowed


class TestScopeContainment:
    def test_engine_is_wired_only_where_phase_10_9b_2_approved(self):
        """Updated in Phase 10.9B.2, which wired the engine into the three
        approved operator entry points. The assertion is not weakened, it is
        re-aimed: 10.9B.1 asserted "nowhere", this asserts "nowhere else".

        Route integration is verified in test_transition_warn_only_10_9b2.py;
        here we only guard that no OTHER module picked it up.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        allowed = {os.path.join("app", "routes", "admin.py"),
                   os.path.join("app", "services", "sales_transition_service.py")}
        offenders = []
        for dirpath, _dirs, files in os.walk(os.path.join(root, "app")):
            for name in files:
                if not name.endswith(".py"):
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root)
                if rel in allowed:
                    continue
                with open(full, encoding="utf-8") as fh:
                    if "sales_transition_service" in fh.read():
                        offenders.append(rel)
        assert offenders == [], f"engine used outside approved sites: {offenders}"

    def test_no_enforcement_in_the_model_setter(self):
        """The lead_status setter is the single choke point every write passes
        through, which makes it look like the ideal enforcement site. It is the
        wrong one: the seeder and the WhatsApp inbound path both flow through
        it and neither is an operator transition."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app", "models.py"), encoding="utf-8") as fh:
            assert "sales_transition_service" not in fh.read()
            assert "can_transition" not in fh.read()

    def test_engine_writes_nothing(self):
        """No commit, no add, no flush anywhere in the module."""
        calls = {n.func.attr for n in ast.walk(_module_ast())
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not calls & {"commit", "add", "flush", "delete", "merge"}

    def test_status_vocabulary_unchanged(self):
        from app.models import LEAD_STATUSES
        assert len(LEAD_STATUSES) == 10

    def test_audit_vocabulary_unchanged(self):
        """No new audit action: this phase adds none."""
        from app.services.audit_service import VALID_ACTIONS
        assert len(VALID_ACTIONS) == 15


# ═══ Tier 2 — real models, real database ═════════════════════════════════════

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import PipelineStage, Tenant                            # noqa: E402
from app.services.sales_pipeline_seed import SalesPipelineSeeder        # noqa: E402

DB_TENANT = "t-10-9b-db"
DB_OTHER = "t-10-9b-other"
_APP = create_app()


@pytest.fixture()
def ctx():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=DB_TENANT, name="A", slug=DB_TENANT))
        db.session.add(Tenant(id=DB_OTHER, name="B", slug=DB_OTHER))
        db.session.commit()
        SalesPipelineSeeder(dry_run=False).run()
        yield
        db.session.remove()


class TestDefaultProviderAgainstRealRows:
    def test_provider_derives_stages_from_the_database(self, ctx):
        stages = sts._default_stage_provider(DB_TENANT)
        assert [s.display_name for s in stages] == NAMES
        assert all(isinstance(s, StageInfo) for s in stages)

    def test_provider_reads_terminal_and_category_from_the_rows(self, ctx):
        by_name = {s.display_name: s for s in sts._default_stage_provider(DB_TENANT)}
        assert by_name["Enrolled"].is_terminal and by_name["Enrolled"].is_won
        assert by_name["Lost"].is_terminal and by_name["Lost"].is_lost
        assert not by_name["Interested"].is_terminal
        assert by_name["Lead"].is_entry

    def test_real_rows_produce_the_same_matrix_as_the_fixture(self, ctx):
        """Tier 1's STANDARD fixture is a faithful model of production."""
        assert (transition_matrix(tenant_id=DB_TENANT)
                == transition_matrix(tenant_id=TENANT, stage_provider=provider()))

    def test_terminal_exit_blocked_against_real_rows(self, ctx):
        v = can_transition("Enrolled", "Lead", tenant_id=DB_TENANT)
        assert not v.allowed and v.code == CODE_TERMINAL_EXIT

    def test_override_works_against_real_rows(self, ctx):
        v = can_transition("Enrolled", "Lead", tenant_id=DB_TENANT, override=True)
        assert v.allowed and v.overridden_rule == RULE_TERMINAL_EXIT

    def test_stage_ids_in_the_verdict_are_real_row_ids(self, ctx):
        v = can_transition("Lead", "Contacted", tenant_id=DB_TENANT)
        assert db.session.get(PipelineStage, v.from_stage_id).display_name == "Lead"
        assert db.session.get(PipelineStage, v.to_stage_id).display_name == "Contacted"

    def test_unknown_tenant_yields_no_opinion(self, ctx):
        assert can_transition("Enrolled", "Lead", tenant_id="no-such-tenant").allowed


class TestRealPerTenantDerivation:
    @staticmethod
    def _stage(tenant_id, name):
        from app.models import PipelineDefinition
        return (db.session.query(PipelineStage)
                .join(PipelineDefinition,
                      PipelineStage.pipeline_id == PipelineDefinition.id)
                .filter(PipelineStage.display_name == name,
                        PipelineDefinition.tenant_id == tenant_id)
                .first())

    def test_renaming_one_tenants_stage_changes_only_that_tenant(self, ctx):
        stage = self._stage(DB_OTHER, "Negotiation")
        stage.display_name = "Haggling"
        db.session.commit()

        assert "Haggling" in describe_allowed_transitions("Lead", tenant_id=DB_OTHER)
        assert "Haggling" not in describe_allowed_transitions("Lead", tenant_id=DB_TENANT)
        assert "Negotiation" in describe_allowed_transitions("Lead", tenant_id=DB_TENANT)

    def test_marking_a_stage_terminal_changes_only_that_tenant(self, ctx):
        stage = self._stage(DB_OTHER, "Negotiation")
        stage.is_terminal = True
        db.session.commit()

        assert not can_transition("Negotiation", "Lead", tenant_id=DB_OTHER).allowed
        assert can_transition("Negotiation", "Lead", tenant_id=DB_TENANT).allowed

    def test_engine_does_not_mutate_anything(self, ctx):
        """It answers questions; it does not move leads."""
        before = [(s.id, s.display_name, s.is_terminal, s.order_index)
                  for s in db.session.query(PipelineStage).order_by(PipelineStage.id)]
        for frm in NAMES + ["fresh"]:
            for to in NAMES + ["bogus"]:
                can_transition(frm, to, tenant_id=DB_TENANT)
        transition_matrix(tenant_id=DB_TENANT)
        describe_allowed_transitions("Lead", tenant_id=DB_TENANT)
        db.session.expire_all()
        after = [(s.id, s.display_name, s.is_terminal, s.order_index)
                 for s in db.session.query(PipelineStage).order_by(PipelineStage.id)]
        assert before == after
