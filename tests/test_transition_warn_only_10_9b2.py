"""Phase 10.9B.2 — warn-only integration of the Sales Transition Engine.

The engine is now consulted at three operator entry points and its verdict is
ignored. That makes this suite's job unusual: the most important tests here
prove that NOTHING CHANGED. A warn-only phase that quietly altered behaviour
would be far worse than one that failed loudly, because the whole argument for
shipping it is that it cannot affect anyone.

So the emphasis is:
  1. the verdict is recorded correctly, and
  2. every transition the system accepted before is still accepted — including
     the ones the engine says it does not like.

Point 2 is tested directly: Enrolled -> Lead produces a BLOCKED verdict, and
the lead moves anyway. If a later phase turns enforcement on, these tests are
exactly the ones that must be updated deliberately rather than by accident.

Import isolation follows test_pipeline_foundation_10_6.py.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_10_9b2_test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("ADMIN_KEY", "testkey")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("SECRET_KEY", "testsecret")
os.environ.setdefault("BROADCAST_API_KEY", "testbroadcast")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import ConversationState, PipelineStage, Tenant         # noqa: E402
from app.services.sales_pipeline_seed import SalesPipelineSeeder        # noqa: E402
from app.services import sales_transition_service as sts                # noqa: E402
from app.routes.admin import transition_verdict, transition_detail      # noqa: E402

TENANT = "t-10-9b2"
_APP = create_app()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_PY = os.path.join(ROOT, "app", "routes", "admin.py")


@pytest.fixture()
def ctx():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=TENANT, name="A", slug=TENANT))
        db.session.commit()
        SalesPipelineSeeder(dry_run=False).run()
        yield
        db.session.remove()


def make_lead(phone, status="Lead"):
    lead = ConversationState(
        phone=phone, name="T", tenant_id=TENANT,
        stage="new", course="", goal="", batch_time="", offer_course="",
        last_msg="", last_text="", lead_status=status,
    )
    db.session.add(lead)
    db.session.commit()
    return lead


# ── The helpers the routes call ──────────────────────────────────────────────

class TestTransitionVerdictHelper:
    def test_returns_a_verdict(self, ctx):
        v = transition_verdict(TENANT, "Lead", "Contacted",
                               sts.CONTEXT_OPERATOR_FORM)
        assert v is not None and v.code == sts.CODE_FORWARD

    def test_reports_a_block_without_raising(self, ctx):
        v = transition_verdict(TENANT, "Enrolled", "Lead",
                               sts.CONTEXT_OPERATOR_FORM)
        assert v is not None and not v.allowed
        assert v.code == sts.CODE_TERMINAL_EXIT

    def test_never_raises_even_when_the_engine_fails(self, ctx, monkeypatch):
        """A lead edit must not fail because a diagnostic did."""
        def boom(*a, **kw):
            raise RuntimeError("engine exploded")
        monkeypatch.setattr(sts, "can_transition", boom)
        assert transition_verdict(TENANT, "Lead", "Contacted",
                                  sts.CONTEXT_OPERATOR_FORM) is None

    def test_unknown_tenant_yields_a_permissive_verdict(self, ctx):
        v = transition_verdict("no-such-tenant", "Enrolled", "Lead",
                               sts.CONTEXT_OPERATOR_FORM)
        assert v.allowed


class TestTransitionDetailHelper:
    def test_records_code_rule_severity_and_context(self, ctx):
        v = transition_verdict(TENANT, "Enrolled", "Lead",
                               sts.CONTEXT_OPERATOR_FORM)
        d = transition_detail(v, sts.CONTEXT_OPERATOR_FORM)
        assert d["transition"] == {
            "code": sts.CODE_TERMINAL_EXIT,
            "rule": sts.RULE_TERMINAL_EXIT,
            "severity": sts.SEVERITY_BLOCK,
            "context": sts.CONTEXT_OPERATOR_FORM,
        }

    def test_none_verdict_yields_an_empty_fragment(self, ctx):
        assert transition_detail(None, sts.CONTEXT_OPERATOR_FORM) == {}

    def test_fragment_is_nested_and_cannot_collide_with_existing_keys(self, ctx):
        v = transition_verdict(TENANT, "Lead", "Contacted",
                               sts.CONTEXT_OPERATOR_FORM)
        detail = {"from": "Lead", "to": "Contacted",
                  "from_stage_id": 1, "to_stage_id": 2}
        before = dict(detail)
        detail.update(transition_detail(v, sts.CONTEXT_OPERATOR_FORM))
        for k, val in before.items():
            assert detail[k] == val, "existing audit keys must survive"
        assert set(detail) == set(before) | {"transition"}

    def test_fragment_is_json_serialisable(self, ctx):
        """It lands in audit_log.detail, which is JSON."""
        import json
        v = transition_verdict(TENANT, "Contacted", "Lead",
                               sts.CONTEXT_OPERATOR_FORM)
        json.dumps(transition_detail(v, sts.CONTEXT_OPERATOR_FORM))

    def test_regression_is_recorded_as_warn(self, ctx):
        v = transition_verdict(TENANT, "Negotiation", "Contacted",
                               sts.CONTEXT_OPERATOR_FORM)
        d = transition_detail(v, sts.CONTEXT_OPERATOR_FORM)["transition"]
        assert d["code"] == sts.CODE_REGRESSION
        assert d["severity"] == sts.SEVERITY_WARN

    @pytest.mark.parametrize("context", [
        sts.CONTEXT_OPERATOR_FORM, sts.CONTEXT_OPERATOR_MOVE,
        sts.CONTEXT_CSV_IMPORT, sts.CONTEXT_AUTO_ADMISSION,
    ])
    def test_context_is_recorded_verbatim(self, ctx, context):
        v = transition_verdict(TENANT, "Lead", "Contacted", context)
        assert transition_detail(v, context)["transition"]["context"] == context


# ── Behaviour is unchanged — the point of the phase ──────────────────────────

class TestBehaviourUnchanged:
    def test_a_blocked_verdict_does_not_stop_the_write(self, ctx):
        """The single most important test here. The engine says no; the lead
        moves anyway, because this phase blocks nothing."""
        lead = make_lead("919000000001", status="Enrolled")
        v = transition_verdict(TENANT, lead.lead_status, "Lead",
                               sts.CONTEXT_OPERATOR_FORM)
        assert not v.allowed, "precondition: the engine must dislike this"

        lead.lead_status = "Lead"          # what the route does regardless
        db.session.commit()
        assert lead.lead_status == "Lead", "warn-only must not prevent the move"

    def test_the_lead_stays_linked_after_a_flagged_transition(self, ctx):
        lead = make_lead("919000000002", status="Enrolled")
        transition_verdict(TENANT, "Enrolled", "Lead", sts.CONTEXT_OPERATOR_FORM)
        lead.lead_status = "Lead"
        db.session.commit()
        assert lead.sales_stage_id is not None
        assert db.session.get(PipelineStage, lead.sales_stage_id).display_name == "Lead"

    @pytest.mark.parametrize("frm,to", [
        ("Lead", "Contacted"), ("Negotiation", "Contacted"),
        ("Lead", "Enrolled"), ("Enrolled", "Lost"), ("Lost", "Interested"),
    ])
    def test_every_transition_still_completes(self, ctx, frm, to):
        lead = make_lead("91900000001" + str(abs(hash((frm, to))) % 10), status=frm)
        transition_verdict(TENANT, frm, to, sts.CONTEXT_OPERATOR_FORM)
        lead.lead_status = to
        db.session.commit()
        assert lead.lead_status == to

    def test_asking_the_engine_writes_nothing(self, ctx):
        lead = make_lead("919000000003", status="Interested")
        before = (lead.lead_status, lead.sales_stage_id, lead._stage,
                  lead.pipeline_stage_id, lead.is_admitted)
        for to in ("Lead", "Enrolled", "Lost", "bogus"):
            transition_verdict(TENANT, lead.lead_status, to,
                               sts.CONTEXT_OPERATOR_FORM)
        db.session.expire_all()
        after = (lead.lead_status, lead.sales_stage_id, lead._stage,
                 lead.pipeline_stage_id, lead.is_admitted)
        assert before == after

    def test_bot_fields_untouched(self, ctx):
        lead = make_lead("919000000004", status="Lead")
        transition_verdict(TENANT, "Lead", "Enrolled", sts.CONTEXT_OPERATOR_FORM)
        db.session.expire_all()
        assert lead._stage == "new"
        assert lead.pipeline_stage_id is None


class TestPipelineCoverageUnchanged:
    def test_coverage_holds_across_flagged_transitions(self, ctx):
        for i in range(6):
            make_lead(f"91900000005{i}", status="Lead")
        total = ConversationState.query.filter_by(tenant_id=TENANT).count()

        for lead in ConversationState.query.filter_by(tenant_id=TENANT).all():
            for to in ("Enrolled", "Lead", "Lost", "Contacted"):
                transition_verdict(TENANT, lead.lead_status, to,
                                   sts.CONTEXT_OPERATOR_FORM)
                lead.lead_status = to
        db.session.commit()

        linked = ConversationState.query.filter(
            ConversationState.tenant_id == TENANT,
            ConversationState.sales_stage_id.isnot(None)).count()
        assert linked == total, "coverage must remain 100%"

    def test_dashboard_still_counts_every_lead(self, ctx):
        from app.services import sales_pipeline_service as sps
        for i in range(4):
            make_lead(f"91900000006{i}", status="Interested")
        lead = ConversationState.query.filter_by(tenant_id=TENANT).first()
        transition_verdict(TENANT, lead.lead_status, "Enrolled",
                           sts.CONTEXT_OPERATOR_FORM)
        lead.lead_status = "Enrolled"
        db.session.commit()

        counted = sum(s["lead_count"] for s in sps.get_pipeline_summary(TENANT, None))
        assert counted == ConversationState.query.filter_by(tenant_id=TENANT).count()


class TestAdmissionExemption:
    def test_admission_promotion_is_recorded_as_exempt_not_blocked(self, ctx):
        """It gets its own AUTO_ADMISSION verdict rather than inheriting the
        form's, because the promotion is the system's transition."""
        v = transition_verdict(TENANT, "Lost", "Enrolled",
                               sts.CONTEXT_AUTO_ADMISSION)
        assert v.allowed and v.code == sts.CODE_EXEMPT

    @pytest.mark.parametrize("frm", ["Lead", "Contacted", "Interested"])
    def test_promotion_from_every_promote_status_is_permitted(self, ctx, frm):
        v = transition_verdict(TENANT, frm, "Enrolled",
                               sts.CONTEXT_AUTO_ADMISSION)
        assert v.allowed


# ── Source-level integration guards ──────────────────────────────────────────

def _admin_ast():
    with open(ADMIN_PY, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _func(name):
    for node in ast.walk(_admin_ast()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in admin.py")


def _calls(func_node, callee):
    return [n for n in ast.walk(func_node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == callee]


class TestInvokedOnlyFromApprovedEntryPoints:
    APPROVED = {"crm_lead_update", "crm_lead_move_stage", "crm_leads_import"}

    def test_all_three_approved_sites_call_the_engine(self):
        for name in self.APPROVED:
            assert _calls(_func(name), "transition_verdict"), f"{name} does not"

    def test_no_other_function_calls_the_engine(self):
        """AST, not string matching — the helper docstrings name the routes."""
        offenders = []
        for node in ast.walk(_admin_ast()):
            if isinstance(node, ast.FunctionDef) and node.name not in self.APPROVED:
                if _calls(node, "transition_verdict"):
                    offenders.append(node.name)
        assert offenders == [], f"unapproved call sites: {offenders}"

    def test_engine_is_not_called_from_the_bot_or_seeder(self):
        for rel in (("app", "state.py"),
                    ("app", "services", "sales_pipeline_seed.py"),
                    ("app", "models.py")):
            with open(os.path.join(ROOT, *rel), encoding="utf-8") as fh:
                body = fh.read()
            assert "sales_transition_service" not in body
            assert "transition_verdict" not in body


class TestNoEnforcement:
    def test_no_call_site_branches_on_allowed(self):
        """Warn-only means the verdict is recorded and otherwise ignored. If a
        route starts reading `.allowed`, that is enforcement and this test is
        the thing that must be changed deliberately."""
        offenders = [
            n.attr for n in ast.walk(_admin_ast())
            if isinstance(n, ast.Attribute) and n.attr == "allowed"
        ]
        assert offenders == [], "a route reads verdict.allowed — that is enforcement"

    def test_no_route_returns_early_on_a_verdict(self):
        """The engine's only outputs in admin.py are the two helpers; nothing
        may redirect or abort based on one."""
        for name in TestInvokedOnlyFromApprovedEntryPoints.APPROVED:
            src = ast.dump(_func(name))
            assert "BLOCKED_" not in src, f"{name} tests a blocked code"

    def test_csv_import_does_not_add_transition_rows_to_errors(self):
        """summary["errors"] is the operator's row report; warn-only must not
        make a successful import look partly failed."""
        node = _func("crm_leads_import")
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "append"):
                dumped = ast.dump(call)
                assert "transition" not in dumped.lower() or "Row" in dumped


class TestAuditVocabularyUnchanged:
    def test_valid_actions_is_still_exactly_fifteen(self):
        from app.services.audit_service import VALID_ACTIONS
        assert len(VALID_ACTIONS) == 15

    def test_no_new_action_constants_referenced(self):
        with open(ADMIN_PY, encoding="utf-8") as fh:
            body = fh.read()
        assert "LEAD_TRANSITION" not in body
        assert "TRANSITION_WARN" not in body

    def test_status_vocabulary_unchanged(self):
        from app.models import LEAD_STATUSES
        assert len(LEAD_STATUSES) == 10


class TestScopeContainment:
    def test_engine_module_unmodified_by_this_phase(self):
        """10.9B.2 integrates the engine; it does not change it. The verdict
        carries no `context` field, which is why the routes pass context to
        transition_detail() separately."""
        assert not hasattr(
            sts.TransitionVerdict(allowed=True, code="x", rule_id="y", reason="z"),
            "context")

    def test_no_transition_logic_in_the_model_layer(self):
        """The warn-only contract: transition rules live at operator entry
        points, never in the model.

        Re-aimed in Phase RC2.3A. This previously shelled out to
        `git status app/models.py migrations/` and asserted the working tree
        was clean — which asserted "no schema change has EVER been made since
        10.9B.2", not "10.9B.2 made none". Any later phase that legitimately
        adds a column or migration failed it, which RC2.3A did.

        The assertion is not weakened: it now checks the actual architectural
        invariant (the model layer holds no transition logic) instead of a
        working-tree state that was only ever a proxy for it, and one that
        could never stay true.
        """
        import ast
        with open(os.path.join(ROOT, "app", "models.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [a.name for a in node.names
                              if "sales_transition_service" in a.name]
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if ("sales_transition_service" in mod
                        or any(a.name in ("sales_transition_service",
                                          "can_transition")
                               for a in node.names)):
                    offenders.append(mod or "can_transition")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ("can_transition", "transition_verdict"):
                    offenders.append(node.func.id)
        assert offenders == [], f"transition logic in the model layer: {offenders}"
