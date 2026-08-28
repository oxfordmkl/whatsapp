"""Phase RC2.5.1 — the live Gemini conversation path becomes tenant-aware.

THE GAP (RC2.5.0 discovery)
----------------------------
tenant_id was correctly resolved at the webhook and correctly threaded through
smart_reply() -- then dropped at the router -> AI boundary. gemini_reply() took
no tenant parameter at all, so every reply was built from one import-time
GenerateContentConfig (Oxford's AALIZA_PROMPT) and one hardcoded trailing cue,
"Reply as Oxford Nova:", regardless of which tenant's customer was messaging.

Tenant.ai_persona_name and Tenant.ai_prompt_override already existed, were
already written by /tenant/ai, and had ZERO runtime consumers -- a tenant
could save "AI settings saved successfully" and the live AI never changed.

WHAT THIS PHASE DOES
---------------------
gemini_reply() now accepts tenant_id and resolves (persona_name,
generation_config) per request via _resolve_persona(): explicit
Tenant.ai_persona_name / Tenant.ai_prompt_override when set, Oxford's exact
prior defaults otherwise. All three router.py call sites now pass
tenant_id=tenant_id. Fail-open throughout, mirroring
ContextAssembler._fetch_memory()'s established contract: no tenant_id, no
matching row, no override, or any DB error all resolve to Oxford's unchanged
defaults -- a prompt lookup failure must never break the chat.

NOT resolve_tenant_id(): _resolve_persona() has no write-side cross-tenant
risk if it falls back (RC2.4.4b's hard-fail contract governs WRITE paths; a
wrong persona on failure is a content-quality issue, not a data-isolation
one), so it is deliberately NOT required to raise.

OUT OF SCOPE, NOT TOUCHED: business_profile.py, constants.py (courses/fees/
payment links), followup_service.py copy, smart_fallback(), CRM branding,
broadcast, Meta onboarding, billing. Confirmed below.
"""
import ast
import logging
import os
import sys
import tempfile
from unittest.mock import MagicMock

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc251_ai_persona.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc251-admin-key")
os.environ.setdefault("SECRET_KEY", "rc251-secret-key")
os.environ["BROADCAST_API_KEY"] = "rc251-broadcast-key"
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
os.environ.setdefault("GEMINI_API_KEY", "rc251-fake-gemini-key")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant                                           # noqa: E402
from app.services import ai_service                                     # noqa: E402
from app.bot import router as router_mod                                # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AI_SVC_PY = os.path.join(ROOT, "app", "services", "ai_service.py")
ROUTER_PY = os.path.join(ROOT, "app", "bot", "router.py")

OX = "t-ox"     # primary tenant, mirrors production: no override set
TB = "t-beta"   # a second, independently configured education tenant

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False
_APP.config["PRIMARY_TENANT_ID"] = OX

# The exact trailing cue every Oxford reply used to hardcode, byte for byte.
_OXFORD_CUE = "Reply as Oxford Nova:"


def _fn_body(path, name):
    tree = ast.parse(open(path, encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    L = open(path, encoding="utf-8").read().splitlines()
    return "\n".join(L[fn.lineno - 1:fn.end_lineno])


@pytest.fixture()
def seeded():
    """Oxford (no AI override -- matches production) and a second tenant
    with a distinct persona + prompt override configured via /tenant/ai."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=OX, name="Oxford", slug=OX, status="ACTIVE",
                              billing_exempt=True))
        db.session.add(Tenant(id=TB, name="Beta Institute", slug=TB,
                              status="ACTIVE", billing_exempt=True,
                              ai_persona_name="Priya",
                              ai_prompt_override="You are Priya, admissions "
                                                  "counselor at Beta Institute."))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


@pytest.fixture()
def fake_client(monkeypatch):
    """Capture the exact (contents, config) Gemini would have received,
    without any network call."""
    captured = {}

    class _Resp:
        text = "mocked reply"

    def _generate_content(model, contents, config):
        captured["model"] = model
        captured["contents"] = contents
        captured["config"] = config
        return _Resp()

    client = MagicMock()
    client.models.generate_content = _generate_content
    monkeypatch.setattr(ai_service, "gemini_client", client)
    return captured


# ═══ GAP A — Oxford's exact prior behaviour is preserved ═══════════════════

class TestOxfordRegressionUnchanged:

    def test_no_tenant_id_produces_the_old_hardcoded_cue(self, seeded, fake_client):
        """The literal string that used to be hardcoded, still produced when
        tenant_id is absent -- the pre-RC2.5.1 call shape."""
        with _APP.app_context():
            out = ai_service.gemini_reply("Hi", "Student")
        assert out == "mocked reply"
        assert fake_client["contents"].rstrip().endswith(_OXFORD_CUE)

    def test_oxford_tenant_id_produces_byte_identical_cue(self, seeded, fake_client):
        """Oxford has no ai_persona_name/ai_prompt_override set (matches
        production today) -- passing its real tenant_id must change nothing."""
        with _APP.app_context():
            ai_service.gemini_reply("Hi", "Student", tenant_id=OX)
        assert fake_client["contents"].rstrip().endswith(_OXFORD_CUE)

    def test_oxford_uses_the_original_default_config_object(self, seeded, fake_client):
        """Not just equal content -- the SAME config object, proving no
        override branch was taken for Oxford."""
        with _APP.app_context():
            ai_service.gemini_reply("Hi", "Student", tenant_id=OX)
        assert fake_client["config"] is ai_service._DEFAULT_GENERATION_CONFIG

    def test_oxford_system_instruction_is_still_aaliza_prompt(self, seeded, fake_client):
        from app.bot.prompts import AALIZA_PROMPT
        with _APP.app_context():
            ai_service.gemini_reply("Hi", "Student", tenant_id=OX)
        assert fake_client["config"].system_instruction == AALIZA_PROMPT

    def test_prompt_prefix_and_suffix_unchanged(self, seeded, fake_client):
        """Only the trailing persona name may vary; everything else in the
        constructed prompt is byte-identical to before."""
        with _APP.app_context():
            ai_service.gemini_reply("hello there", "Ravi", tenant_id=OX)
        c = fake_client["contents"]
        assert 'Student name: Ravi' in c
        assert 'Student says: "hello there"' in c
        assert c.rstrip().endswith(_OXFORD_CUE)


# ═══ GAP B — tenant isolation: B never receives Oxford's prompt content ═══

class TestTenantContentIsolation:

    def test_tenant_b_does_not_receive_aaliza_prompt(self, seeded, fake_client):
        from app.bot.prompts import AALIZA_PROMPT
        with _APP.app_context():
            ai_service.gemini_reply("Hi", "Student", tenant_id=TB)
        assert fake_client["config"].system_instruction != AALIZA_PROMPT
        assert "Oxford" not in fake_client["config"].system_instruction

    def test_tenant_b_does_not_receive_oxford_cue(self, seeded, fake_client):
        with _APP.app_context():
            ai_service.gemini_reply("Hi", "Student", tenant_id=TB)
        assert _OXFORD_CUE not in fake_client["contents"]
        assert "Oxford Nova" not in fake_client["contents"]

    def test_tenant_b_gets_its_own_persona_and_prompt(self, seeded, fake_client):
        with _APP.app_context():
            ai_service.gemini_reply("Hi", "Student", tenant_id=TB)
        assert fake_client["contents"].rstrip().endswith("Reply as Priya:")
        assert "Beta Institute" in fake_client["config"].system_instruction

    def test_oxford_and_beta_produce_different_configs_same_request(self, seeded, fake_client):
        """Same call, two tenants, back to back -- proves resolution is
        per-request, not cached/leaked from a prior call."""
        with _APP.app_context():
            ai_service.gemini_reply("Hi", "Student", tenant_id=OX)
            ox_config = fake_client["config"]
            ai_service.gemini_reply("Hi", "Student", tenant_id=TB)
            tb_config = fake_client["config"]
        assert ox_config is not tb_config
        assert ox_config.system_instruction != tb_config.system_instruction


# ═══ GAP C — persona override only ══════════════════════════════════════

class TestPersonaOverrideOnly:

    def test_persona_name_changes_the_cue_without_a_prompt_override(self, seeded, fake_client):
        with _APP.app_context():
            t = Tenant.query.get(TB)
            t.ai_prompt_override = None
            t.ai_persona_name = "Rahul"
            db.session.commit()
            ai_service.gemini_reply("Hi", "Student", tenant_id=TB)
        assert fake_client["contents"].rstrip().endswith("Reply as Rahul:")

    def test_persona_only_now_also_reaches_the_system_instruction(self, seeded, fake_client):
        """TRIPWIRE INVERTED BY RC2.5.2 (was:
        test_persona_only_still_uses_default_system_instruction).

        RC2.5.1 applied a custom persona ONLY to the trailing cue, leaving the
        system instruction saying "You are Oxford Nova" while the cue said
        "Reply as Rahul:" -- internally contradictory. RC2.5.2 composes the
        system instruction per tenant, so the persona now reaches both.

        This assertion is inverted, not deleted: it still pins the boundary,
        now from the other side. Oxford is unaffected (it sets no persona) --
        test_oxford_uses_the_original_default_config_object above still proves
        the unconfigured path returns the untouched module-level object.
        """
        from app.bot.prompts import AALIZA_PROMPT
        with _APP.app_context():
            t = Tenant.query.get(TB)
            t.ai_prompt_override = None
            t.ai_persona_name = "Rahul"
            db.session.commit()
            ai_service.gemini_reply("Hi", "Student", tenant_id=TB)
        cfg = fake_client["config"]
        assert cfg is not ai_service._DEFAULT_GENERATION_CONFIG
        assert cfg.system_instruction != AALIZA_PROMPT
        assert "You are Rahul," in cfg.system_instruction
        assert "You are Oxford Nova," not in cfg.system_instruction
        # Everything except the persona is still the baseline body.
        assert cfg.system_instruction == AALIZA_PROMPT.replace(
            "Oxford Nova", "Rahul")
        assert fake_client["contents"].rstrip().endswith("Reply as Rahul:")


# ═══ GAP D — prompt override only ═════════════════════════════════════════

class TestPromptOverrideOnly:

    def test_prompt_override_changes_system_instruction_without_persona(self, seeded, fake_client):
        with _APP.app_context():
            t = Tenant.query.get(TB)
            t.ai_persona_name = None
            t.ai_prompt_override = "You are the Beta Institute assistant."
            db.session.commit()
            ai_service.gemini_reply("Hi", "Student", tenant_id=TB)
        assert fake_client["config"].system_instruction == "You are the Beta Institute assistant."

    def test_no_persona_falls_back_to_default_persona_name(self, seeded, fake_client):
        """Prompt override set, persona NOT set -> cue still says the
        default persona name, not a blank or None."""
        with _APP.app_context():
            t = Tenant.query.get(TB)
            t.ai_persona_name = None
            t.ai_prompt_override = "You are the Beta Institute assistant."
            db.session.commit()
            ai_service.gemini_reply("Hi", "Student", tenant_id=TB)
        assert fake_client["contents"].rstrip().endswith(f"Reply as {ai_service._DEFAULT_PERSONA_NAME}:")


# ═══ GAP E — fallback / default behaviour, never raises ═══════════════════

class TestFallbackNeverRaises:

    def test_none_tenant_id_does_not_raise(self, seeded, fake_client):
        with _APP.app_context():
            out = ai_service.gemini_reply("Hi", "Student", tenant_id=None)
        assert out == "mocked reply"

    def test_unknown_tenant_id_falls_back_to_default(self, seeded, fake_client):
        """A tenant_id with no matching row (e.g. deleted) must never crash
        the chat -- resolves to Oxford's defaults, exactly like no tenant_id
        at all."""
        with _APP.app_context():
            ai_service.gemini_reply("Hi", "Student", tenant_id="does-not-exist")
        assert fake_client["contents"].rstrip().endswith(_OXFORD_CUE)
        assert fake_client["config"] is ai_service._DEFAULT_GENERATION_CONFIG

    def test_db_error_during_resolution_falls_back_and_does_not_raise(self, seeded, fake_client, monkeypatch):
        """Simulates a DB failure during persona resolution -- must fail
        open to Oxford's defaults, mirroring ContextAssembler's contract,
        never propagate to break the reply.

        Tenant.query is a fresh Query proxy on every access (Flask-SQLAlchemy
        class-level property), so patching one instance's .get is a no-op --
        the class attribute itself must be replaced."""
        class _BoomQuery:
            def get(self, *a, **k):
                raise RuntimeError("simulated DB failure")
        with _APP.app_context():
            monkeypatch.setattr(Tenant, "query", _BoomQuery())
            out = ai_service.gemini_reply("Hi", "Student", tenant_id=TB)
        assert out == "mocked reply"
        assert fake_client["contents"].rstrip().endswith(_OXFORD_CUE)

    def test_gemini_client_absent_still_returns_none_before_resolution(self, seeded, monkeypatch):
        """Pre-existing contract (unchanged): if the client itself is
        unconfigured, bail out before ever touching tenant resolution."""
        monkeypatch.setattr(ai_service, "gemini_client", None)
        with _APP.app_context():
            assert ai_service.gemini_reply("Hi", "Student", tenant_id=TB) is None


# ═══ GAP F — structural regression: all 3 router call sites wired ════════

class TestRouterCallSitesRegression:

    @staticmethod
    def _smart_reply_node():
        tree = ast.parse(open(ROUTER_PY, encoding="utf-8").read())
        return next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "smart_reply")

    def test_all_three_call_sites_pass_tenant_id(self):
        fn = self._smart_reply_node()
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and ast.unparse(n.func).split(".")[-1] == "gemini_reply"]
        assert len(calls) == 3, f"expected 3 gemini_reply() call sites, found {len(calls)}"
        for c in calls:
            kw = {k.arg for k in c.keywords}
            assert "tenant_id" in kw, f"call at line {c.lineno} omits tenant_id"

    def test_no_call_site_hardcodes_a_literal_tenant_id(self):
        """Every call site must forward the request's own tenant_id
        variable, never a fixed string."""
        fn = self._smart_reply_node()
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and ast.unparse(n.func).split(".")[-1] == "gemini_reply"]
        for c in calls:
            tid_kw = next(k for k in c.keywords if k.arg == "tenant_id")
            assert isinstance(tid_kw.value, ast.Name) and tid_kw.value.id == "tenant_id", \
                f"call at line {c.lineno} does not forward the tenant_id variable"


# ═══ GAP G — end-to-end: smart_reply() actually forwards tenant_id ════════

class TestSmartReplyEndToEnd:
    """Drives the real state machine to router.py's "not_sure" branch
    (stage == "goal_selection", input "5"), the simplest of the three
    gemini_reply() call sites in smart_reply(), by pre-seeding
    ConversationState via the real get_or_create_state() -- exactly what
    every prior turn of a real conversation would have done."""

    @staticmethod
    def _seed_goal_selection(phone, tenant_id):
        from app.state import get_or_create_state
        st = get_or_create_state(phone, "Student", tenant_id=tenant_id)
        st["stage"] = "goal_selection"

    def test_smart_reply_forwards_real_tenant_persona_to_gemini(self, seeded, fake_client):
        """The full path: smart_reply(tenant_id=TB) -> the "not_sure" branch
        -> gemini_reply(tenant_id=TB) -> _resolve_persona(TB) -> Beta's own
        config. Proves the wiring holds end-to-end, not just at the unit
        level."""
        phone = "919000099001"
        with _APP.app_context():
            self._seed_goal_selection(phone, TB)
            reply, preset = router_mod.smart_reply(
                "5", "Student", phone, is_new_lead=False, tenant_id=TB)
        assert preset == "GOAL"
        assert reply == "mocked reply"
        assert fake_client["contents"].rstrip().endswith("Reply as Priya:")
        assert "Beta Institute" in fake_client["config"].system_instruction

    def test_smart_reply_oxford_unchanged_end_to_end(self, seeded, fake_client):
        phone = "919000099002"
        with _APP.app_context():
            self._seed_goal_selection(phone, OX)
            reply, preset = router_mod.smart_reply(
                "5", "Student", phone, is_new_lead=False, tenant_id=OX)
        assert preset == "GOAL"
        assert fake_client["contents"].rstrip().endswith(_OXFORD_CUE)
        assert fake_client["config"] is ai_service._DEFAULT_GENERATION_CONFIG


# ═══ scope: nothing else touched ═══════════════════════════════════════════

class TestOutOfScopeUntouched:

    def test_smart_fallback_unchanged(self):
        """smart_fallback() is explicitly out of RC2.5.1 scope (P1 in the
        RC2.5.0 backlog) -- still hardcodes Oxford, unchanged."""
        src = open(AI_SVC_PY, encoding="utf-8").read()
        assert '"Njan Oxford Nova — The Oxford Computers-nte counselor.\\n"' in src

    def test_business_profile_module_untouched(self):
        src = open(os.path.join(ROOT, "app", "bot", "business_profile.py"),
                   encoding="utf-8").read()
        assert 'INSTITUTE_NAME = "The Oxford Computers"' in src

    def test_course_catalog_untouched(self):
        src = open(os.path.join(ROOT, "app", "bot", "constants.py"),
                   encoding="utf-8").read()
        assert "COURSE_PAYMENT_LINKS" in src
        assert "PGDCA" in src

    def test_followup_copy_untouched(self):
        src = open(os.path.join(ROOT, "app", "services", "followup_service.py"),
                   encoding="utf-8").read()
        assert "The Oxford Computers" in src

    def test_no_migration_files_added(self):
        import subprocess
        out = subprocess.run(["git", "status", "--porcelain", "--", "migrations/"],
                             capture_output=True, text=True, cwd=ROOT).stdout
        assert out.strip() == "", f"unexpected migrations/ changes: {out}"


# ═══ upstream phases remain intact ══════════════════════════════════════

class TestUpstreamPhasesIntact:

    def test_resolve_tenant_id_untouched(self):
        """RC2.5.1 must not touch RC2.4.4b's hard-fail resolver."""
        src = open(os.path.join(ROOT, "app", "services", "log_service.py"),
                   encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "resolve_tenant_id")
        body = ast.parse(ast.unparse(fn)).body[0]
        if (body.body and isinstance(body.body[0], ast.Expr)
                and isinstance(body.body[0].value, ast.Constant)):
            body.body.pop(0)
        code = ast.unparse(body)
        assert "raise ValueError" in code, "RC2.4.4b leg-2 hard failure was removed"
        assert "return tenant_id" in code
        assert code.rstrip().endswith("return None")

    def test_six_rc244a_write_guards_still_present(self):
        src = open(os.path.join(ROOT, "app", "routes", "admin.py"),
                   encoding="utf-8").read()
        tree = ast.parse(src)
        six = ["crm_lead_send", "crm_lead_update", "crm_course_admissions",
               "crm_marketing_start_job", "campaign_send", "crm_tasks_complete"]
        L = src.splitlines()
        for name in six:
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            body = "\n".join(L[fn.lineno - 1:fn.end_lineno])
            assert "if not _tid:" in body, f"{name} lost its RC2.4.4a guard"

    def test_ai_service_does_not_import_tenant_query_or_filter(self):
        """This phase reads Tenant.query.get(tenant_id) directly -- it must
        NOT reach for tenant_query()/tenant_filter() (those are request-
        actor-scoped helpers in admin.py; ai_service resolves an EXPLICIT
        webhook-supplied tenant_id, a different and correct pattern, matching
        _get_waba_credentials()'s own Tenant.query.get() usage)."""
        src = open(AI_SVC_PY, encoding="utf-8").read()
        assert "tenant_query" not in src
        assert "tenant_filter" not in src
