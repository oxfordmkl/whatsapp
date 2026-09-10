"""RC2.5.4c-x-6c1 — tenant_id plumbing for single-tenant customer surfaces.

WHAT THIS PHASE IS
------------------
Three customer-facing functions could not be made tenant-aware because they
never received a tenant:

    screens.main_menu(name)
    screens.legacy_main_menu_reply(name)
    ai_service.smart_fallback(name, msg)

The x-6c audit established that every one of their callers ALREADY holds an
authoritative tenant_id -- `router.smart_reply`'s own parameter, threaded down
through `_try_navigation`, `_nearest_menu` and `_enter_main_menu`. Nothing had
to be discovered, inferred or looked up; the value was simply being dropped.

WHAT THIS PHASE IS NOT
----------------------
tenant_id is accepted and NEVER READ. Not one byte of customer output changes.
The institute name, the Rutronix/PSC/NORKA wording, the EMI claims, the
default catalogue and the Oxford identity fallback are all exactly as they
were -- those are separate, tracked defects (x-6b, x-6c) with their own
unresolved business questions.

That is deliberate, and it is what these tests assert. A phase that threaded
the parameter AND changed the payload at the same time would make it
impossible to tell a plumbing bug from a wording bug when something broke.
So the acceptance criterion here is the strongest one available: the output
must be BYTE-IDENTICAL with the parameter present, absent or None.

The tests therefore fall into three groups:

  1. SIGNATURE   -- the three functions accept tenant_id, optional and last.
  2. CALL SITES  -- all seven audited calls pass it, and it comes from the
                    enclosing function's OWN parameter, never a global.
  3. NEUTRALITY  -- output is byte-identical across every calling form, with
                    anti-vacuity canaries proving the functions still produce
                    their real content rather than empty strings.
"""
import ast
import os
import sys
import tempfile

import pytest

# Suites in this repo each rewrite DATABASE_URL and path-load `app` under
# different shapes, so a stale `app` in sys.modules resolves to a non-package
# here. Purge before importing, exactly as every other suite does, and run one
# file per pytest process.
for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc254cx6c1.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc254cx6c1-admin-key")
os.environ.setdefault("SECRET_KEY", "rc254cx6c1-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc254cx6c1-broadcast-key")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app.bot import screens as _screens          # noqa: E402
from app.services import ai_service as _ai       # noqa: E402

ROUTER_PY = os.path.join(ROOT, "app", "bot", "router.py")
SCREENS_PY = os.path.join(ROOT, "app", "bot", "screens.py")
AI_PY = os.path.join(ROOT, "app", "services", "ai_service.py")


def _src(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _fn(path, name):
    """The named top-level function's AST node."""
    for node in ast.walk(ast.parse(_src(path))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError("%s not found in %s" % (name, path))


def _params(node):
    return [a.arg for a in node.args.args]


# The seven call sites the x-6c audit enumerated, as (file, line, exact text).
CALL_SITES = [
    (ROUTER_PY, 143, "        return screens.main_menu(name, tenant_id)"),
    (ROUTER_PY, 175, "    return screens.main_menu(name, tenant_id)"),
    (ROUTER_PY, 199, "        return legacy_main_menu_reply(name, tenant_id)"),
    (ROUTER_PY, 265, "            screen = screens.main_menu(name, tenant_id)"),
    (ROUTER_PY, 498,
     '            return (ai or smart_fallback(name, low, tenant_id)), "GOAL"'),
    (ROUTER_PY, 621, '    return smart_fallback(name, raw, tenant_id), "COURSE"'),
    (SCREENS_PY, None,
     "    legacy_body, legacy_preset = legacy_main_menu_reply(name, tenant_id)"),
]


# ═══ 1 — signatures ════════════════════════════════════════════════════════

class TestSignatures:

    @pytest.mark.parametrize("path,name", [
        (SCREENS_PY, "main_menu"),
        (SCREENS_PY, "legacy_main_menu_reply"),
        (AI_PY, "smart_fallback"),
    ])
    def test_accepts_tenant_id(self, path, name):
        assert "tenant_id" in _params(_fn(path, name))

    @pytest.mark.parametrize("path,name", [
        (SCREENS_PY, "main_menu"),
        (SCREENS_PY, "legacy_main_menu_reply"),
        (AI_PY, "smart_fallback"),
    ])
    def test_tenant_id_is_last_and_optional(self, path, name):
        """Optional and trailing, matching the house pattern used by
        course_details, _nearest_menu, _try_navigation and every offer
        handler. Required or keyword-only would break existing positional
        callers -- including a CI-active one, rc252::test_smart_fallback_
        untouched, which calls smart_fallback("Student")."""
        node = _fn(path, name)
        params = _params(node)
        assert params[-1] == "tenant_id", (
            "%s: tenant_id must be the LAST positional parameter" % name)
        assert node.args.kwonlyargs == [], (
            "%s: tenant_id must not be keyword-only" % name)
        # Every parameter from tenant_id's position on must have a default.
        assert len(node.args.defaults) >= 1, "%s: tenant_id has no default" % name
        default = node.args.defaults[-1]
        assert isinstance(default, ast.Constant) and default.value is None, (
            "%s: tenant_id's default must be None" % name)

    def test_signatures_are_exactly_as_audited(self):
        assert "def main_menu(name: str = \"\", tenant_id=None)" in _src(SCREENS_PY)
        assert "def legacy_main_menu_reply(name: str, tenant_id=None)" \
            in _src(SCREENS_PY)
        assert "def smart_fallback(name: str, msg: str = \"\", tenant_id=None)" \
            in _src(AI_PY)


# ═══ 2 — call sites ════════════════════════════════════════════════════════

class TestCallSites:

    @pytest.mark.parametrize("path,lineno,text", CALL_SITES)
    def test_call_site_passes_tenant_id(self, path, lineno, text):
        lines = _src(path).splitlines()
        if lineno is not None:
            assert lines[lineno - 1] == text, (
                "%s:%d is not the audited call: %r"
                % (os.path.basename(path), lineno, lines[lineno - 1]))
        else:
            assert text in lines, (
                "%s does not contain the audited call %r"
                % (os.path.basename(path), text))

    def test_no_bare_call_remains(self):
        """Anti-vacuity for the whole class: if someone reverted a call the
        parametrised test above would fail, but if someone ADDED a new bare
        call this catches it."""
        router = _src(ROUTER_PY)
        for bare in ("screens.main_menu(name)",
                     "legacy_main_menu_reply(name)",
                     "smart_fallback(name, low)",
                     "smart_fallback(name, raw)"):
            assert bare + ")" not in router and bare + "," not in router \
                and (bare + "\n") not in router, (
                "router.py still contains a bare call: %r" % bare)
        assert "legacy_main_menu_reply(name)\n" not in _src(SCREENS_PY)

    @pytest.mark.parametrize("name", [
        "_nearest_menu", "_enter_main_menu", "_try_navigation", "smart_reply",
    ])
    def test_tenant_id_comes_from_the_enclosing_parameter(self, name):
        """The threaded value must be the caller's OWN authoritative tenant.
        If the enclosing function did not take tenant_id, the name would have
        to resolve to a module global -- exactly the inference the audit
        ruled out."""
        assert "tenant_id" in _params(_fn(ROUTER_PY, name))

    def test_main_menu_forwards_to_the_legacy_fallback(self):
        """main_menu builds the legacy body as its transport fallback, so the
        fallback must carry the tenant too -- otherwise the tenant would be
        known on the List Message path and unknown on the plain-text one."""
        node = _fn(SCREENS_PY, "main_menu")
        seg = ast.get_source_segment(_src(SCREENS_PY), node)
        assert "legacy_main_menu_reply(name, tenant_id)" in seg

    @pytest.mark.parametrize("path", [ROUTER_PY, SCREENS_PY, AI_PY])
    def test_no_global_tenant_inference_introduced(self, path):
        """No PRIMARY_TENANT_ID, no current-tenant global, no Oxford fallback
        constant. The audit rejected every one of these as a tenant source."""
        src = _src(path)
        assert "PRIMARY_TENANT_ID" not in src
        assert "current_tenant" not in src


# ═══ 3 — behaviour neutrality ══════════════════════════════════════════════

# A real tenant id shape, and a deliberately unknown one. Neither may change
# the output, because tenant_id is not read.
OX = "af8136356c9743ff95a26be174c69477"
UNKNOWN = "t-does-not-exist"


@pytest.fixture(scope="module")
def screens():
    return _screens


@pytest.fixture(scope="module")
def ai():
    return _ai


class TestByteIdenticalOutput:

    def test_main_menu_identical_across_calling_forms(self, screens):
        a = screens.main_menu("X")
        b = screens.main_menu("X", None)
        c = screens.main_menu("X", OX)
        d = screens.main_menu("X", UNKNOWN)
        for other, label in ((b, "None"), (c, "OX"), (d, "UNKNOWN")):
            assert other.body == a.body, "main_menu body changed for " + label
            assert other.fallback_body == a.fallback_body, \
                "main_menu fallback_body changed for " + label
            assert other.as_sections() == a.as_sections(), \
                "main_menu sections changed for " + label

    def test_legacy_main_menu_reply_identical(self, screens):
        a = screens.legacy_main_menu_reply("X")
        assert screens.legacy_main_menu_reply("X", None) == a
        assert screens.legacy_main_menu_reply("X", OX) == a
        assert screens.legacy_main_menu_reply("X", UNKNOWN) == a

    @pytest.mark.parametrize("msg", [
        "", "fees", "what is the price", "job placement", "hello",
    ])
    def test_smart_fallback_identical(self, ai, msg):
        a = ai.smart_fallback("X", msg)
        assert ai.smart_fallback("X", msg, None) == a
        assert ai.smart_fallback("X", msg, OX) == a
        assert ai.smart_fallback("X", msg, UNKNOWN) == a

    def test_smart_fallback_single_argument_form_still_works(self, ai):
        """rc252::test_smart_fallback_untouched calls smart_fallback("Student")
        with ONE positional argument and is CI-active. Making tenant_id
        required would break it; this proves the form still binds."""
        assert ai.smart_fallback("Student") == ai.smart_fallback("Student", "", None)


class TestAntiVacuity:
    """Every assertion above compares outputs to each other, so all of them
    would pass if the functions returned "" . These canaries prove the real
    content is still being produced.

    NOTE ON WHAT THESE CANARIES PIN: they assert the CURRENT, still-Oxford
    output. That is intentional for this phase -- x-6c1 is behaviour-neutral,
    so the canary IS the neutrality proof. When a later phase makes these
    surfaces genuinely tenant-aware, these canaries must be INVERTED to assert
    the tenant's own values, never deleted.
    """

    def test_main_menu_still_renders_its_real_body(self, screens):
        s = screens.main_menu("Alice", OX)
        assert "Alice" in s.body
        assert len(s.body) > 80
        assert s.as_sections(), "main_menu produced no list sections"

    def test_main_menu_still_carries_its_legacy_fallback(self, screens):
        s = screens.main_menu("Alice", OX)
        assert s.fallback_body and len(s.fallback_body) > 80
        assert s.fallback_preset == "GOAL"

    def test_legacy_reply_still_renders_its_real_text(self, screens):
        text, preset = screens.legacy_main_menu_reply("Alice", OX)
        assert "Alice" in text
        assert preset == "GOAL"
        assert len(text) > 80

    @pytest.mark.parametrize("msg,marker", [
        ("fees", "FEES"),
        ("job", "COURSES"),
        ("hello", "COURSES"),
    ])
    def test_smart_fallback_still_answers(self, ai, msg, marker):
        out = ai.smart_fallback("Alice", msg, OX)
        assert "Alice" in out
        assert marker in out


class TestPayloadUnchangedByThisPhase:
    """x-6c1 must NOT consume tenant_id. These assert the payload defects the
    audits recorded are still exactly as they were -- so that if a later phase
    fixes one, it does so under its own authorisation and its own tests, and
    nobody can mistake this phase for having addressed them.

    Each of these is a KNOWN DEFECT, deliberately preserved here, not an
    endorsement: the institute name, the recognition wording and the blanket
    EMI row are tracked in x-6b/x-6c and remain open.
    """

    def test_identity_is_not_resolved_by_the_threaded_functions(self):
        """The whole point of the phase: accepted, not read."""
        for name in ("main_menu", "legacy_main_menu_reply"):
            seg = ast.get_source_segment(_src(SCREENS_PY), _fn(SCREENS_PY, name))
            assert "resolve_business_identity" not in seg, (
                "%s resolves identity -- that is a LATER phase" % name)
            assert "_identity(" not in seg, (
                "%s resolves identity -- that is a LATER phase" % name)
        seg = ast.get_source_segment(_src(AI_PY), _fn(AI_PY, "smart_fallback"))
        assert "resolve_business_identity" not in seg
        assert "tenant_identity_service" not in seg

    def test_smart_fallback_body_is_byte_identical_to_head(self):
        """Only the signature and the docstring may differ from HEAD; every
        executable line of the body must be unchanged."""
        import subprocess
        head = subprocess.run(["git", "show", "HEAD:app/services/ai_service.py"],
                              cwd=ROOT, capture_output=True)
        assert head.returncode == 0
        old_tree = ast.parse(head.stdout.decode("utf-8"))
        old_fn = next(n for n in ast.walk(old_tree)
                      if isinstance(n, ast.FunctionDef)
                      and n.name == "smart_fallback")
        new_fn = _fn(AI_PY, "smart_fallback")

        def body_without_docstring(node):
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            return [ast.dump(n) for n in body]

        assert body_without_docstring(new_fn) == body_without_docstring(old_fn), (
            "smart_fallback's executable body changed -- this phase is "
            "plumbing only")

    def test_known_payload_defects_are_still_present(self, screens, ai):
        """Preserved deliberately. Inverting these is a later phase's job."""
        s = screens.main_menu("A", OX)
        # as_sections() renders to WhatsApp's wire shape: plain dicts.
        rows = [r.get("description") for sec in s.as_sections()
                for r in sec.get("rows", ())]
        assert any("EMI" in (d or "") for d in rows), (
            "the main-menu EMI row vanished -- x-6c1 may not change payload")
        assert "Oxford Nova" in ai.smart_fallback("A", "", OX), (
            "smart_fallback's persona text changed -- x-6c1 is plumbing only")
