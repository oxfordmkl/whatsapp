"""Phase RC2.4.3 — _get_default_tenant_id() is deleted, not merely uncalled.

WHAT THIS PHASE DID
-------------------
_get_default_tenant_id() was the Phase 12-C1 emergency hotfix:

    Tenant.query.first()        # no filter, no ORDER BY -> ARBITRARY row

It is the mechanism behind TD-P0-1 / the Phase 17.1-C mis-filing incident.
Phase H4-c removed it from resolve_tenant_id()'s leg 3 but left the function
DEFINED, because deleting it was outside that phase's approved scope. The
RC2.4.3 discovery then proved, by AST inventory over 184 files, that it had
1 definition, 0 callers and 0 imports — so deleting it changes no runtime
path at all. This phase deletes it.

WHY DELETE SOMETHING THAT WAS ALREADY HARMLESS
----------------------------------------------
An uncalled function is one careless import away from being called again, and
its own docstring recommended it ("safe while only one tenant exists" — false
since the platform reached 12 tenants). Removing the definition converts a
convention into an impossibility.

WHAT THIS PHASE DELIBERATELY DID **NOT** DO
-------------------------------------------
RC2.4.3 does NOT retire resolve_tenant_id() leg 2 — the branch that answers
PRIMARY_TENANT_ID when a caller passes tenant_id=None. That is the surviving
implicit-default mechanism and it remains LIVE and REACHABLE by design; ten
non-outbound callers still resolve through it. RC2.4.1 closed the equivalent
leg for OUTBOUND transport only. Retiring leg 2 is a separate, larger phase.

The tests below therefore assert that leg 2 STILL WORKS. If a future phase
retires it, these assertions are the ones to invert — do not delete them.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc243_tenant.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc243-admin-key")
os.environ.setdefault("SECRET_KEY", "rc243-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc243-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-primary")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant                                           # noqa: E402
from app.services.log_service import resolve_tenant_id                  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPDIR = os.path.join(ROOT, "app")
LOGSVC = os.path.join(APPDIR, "services", "log_service.py")

GONE = "_get_default_tenant_id"

PRIMARY = "t-primary"
OTHER = "t-other"
FIRST = "t-aaa-first"   # sorts first; what Tenant.query.first() would return

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False


def _app_sources():
    """(relpath, ast.Module) for every parseable .py under app/."""
    out = []
    for dp, _d, fs in os.walk(APPDIR):
        if "__pycache__" in dp:
            continue
        for f in fs:
            if not f.endswith(".py"):
                continue
            p = os.path.join(dp, f)
            try:
                tree = ast.parse(open(p, encoding="utf-8").read())
            except SyntaxError:
                continue
            out.append((os.path.relpath(p, ROOT), tree))
    return out


@pytest.fixture()
def seeded():
    """Three tenants, the non-primary one created FIRST.

    Ordering is the point: if the arbitrary resolver ever returns,
    Tenant.query.first() yields 't-aaa-first' — neither the explicit tenant nor
    PRIMARY — so a guess is distinguishable from both legitimate answers. A
    single-tenant fixture would hide the exact bug this phase removes.
    """
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, nm in ((FIRST, "First"), (PRIMARY, "Primary"), (OTHER, "Other")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()
        yield


# ═══ the function is GONE ════════════════════════════════════════════════════

class TestDefinitionRemoved:

    def test_no_definition_anywhere_in_app(self):
        """The strong property. H4-c could only assert 'uncalled'."""
        defs = []
        for rel, tree in _app_sources():
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and n.name == GONE:
                    defs.append(f"{rel}:{n.lineno}")
        assert defs == [], f"{GONE} was reintroduced: {defs}"

    def test_not_defined_in_log_service_specifically(self):
        """Named separately so the failure points at the file it lived in."""
        tree = ast.parse(open(LOGSVC, encoding="utf-8").read())
        names = [n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        assert GONE not in names, f"{GONE} is back in log_service.py"

    def test_no_call_anywhere_in_app(self):
        calls = []
        for rel, tree in _app_sources():
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and \
                        ast.unparse(n.func).split(".")[-1] == GONE:
                    calls.append(f"{rel}:{n.lineno}")
        assert calls == [], f"{GONE} was wired again: {calls}"

    def test_no_import_anywhere_in_app(self):
        imports = []
        for rel, tree in _app_sources():
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom):
                    if any(a.name == GONE for a in n.names):
                        imports.append(f"{rel}:{n.lineno}")
                elif isinstance(n, ast.Import):
                    if any(a.name.split(".")[-1] == GONE for a in n.names):
                        imports.append(f"{rel}:{n.lineno}")
        assert imports == [], f"{GONE} is imported again: {imports}"

    def test_the_name_survives_only_as_prose(self):
        """It is still NAMED in explanatory docstrings, deliberately — the
        history is why the guard exists. This asserts the distinction the
        discovery had to make by hand: prose is fine, executable code is not.
        A grep-based guard could not tell these apart."""
        textual = [rel for rel, _t in _app_sources()
                   if GONE in open(os.path.join(ROOT, rel), encoding="utf-8").read()]
        executable = []
        for rel, tree in _app_sources():
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and \
                        ast.unparse(n.func).split(".")[-1] == GONE:
                    executable.append(rel)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and n.name == GONE:
                    executable.append(rel)
        assert executable == [], f"executable references remain: {executable}"
        assert textual, "expected the retirement to stay documented in prose"


# ═══ the defect CLASS, not just the one name ═════════════════════════════════

class TestNoUnfilteredTenantFirst:

    def test_no_unfiltered_tenant_query_first_in_app(self):
        """Generalises the guard: the danger was never the function's NAME, it
        was selecting an arbitrary tenant row. Tenant.query.filter_by(...).first()
        is fine and is used by the webhook; bare Tenant.query.first() is not."""
        hits = []
        for rel, tree in _app_sources():
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call):
                    continue
                chain = ast.unparse(n.func)
                if chain in ("Tenant.query.first", "Tenant.query.one_or_none"):
                    hits.append(f"{rel}:{n.lineno}  {chain}()")
        assert hits == [], f"arbitrary-tenant selection reintroduced: {hits}"

    def test_no_bare_session_query_tenant_first_in_app(self):
        """The same defect spelled the other way round."""
        hits = []
        for rel, tree in _app_sources():
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call):
                    continue
                chain = ast.unparse(n.func)
                if chain.endswith(".first") and "query(Tenant)" in chain \
                        and "filter" not in chain and "order_by" not in chain:
                    hits.append(f"{rel}:{n.lineno}  {chain}()")
        assert hits == [], f"arbitrary-tenant selection reintroduced: {hits}"

    def test_filtered_lookups_are_still_permitted(self):
        """Guards the guard: the checks above must not be so broad that they
        forbid the webhook's legitimate identity lookup, which is the ONLY way
        an inbound message finds its tenant."""
        webhook = os.path.join(APPDIR, "routes", "webhook.py")
        tree = ast.parse(open(webhook, encoding="utf-8").read())
        filtered = [n for n in ast.walk(tree)
                    if isinstance(n, ast.Call)
                    and ast.unparse(n.func).endswith(".first")
                    and "filter_by" in ast.unparse(n.func)]
        assert filtered, "expected webhook.py to still resolve a tenant by filter"


# ═══ resolve_tenant_id keeps EXACTLY its three legs ══════════════════════════

class TestResolveTenantIdUnchanged:
    """RC2.4.3 does NOT retire leg 2. These assert it still answers."""

    def test_leg1_explicit_tenant_wins(self, seeded):
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = PRIMARY
            assert resolve_tenant_id(OTHER) == OTHER

    def test_leg2_none_resolves_to_primary(self, seeded):
        """STILL LIVE BY DESIGN. Retiring this is a separate phase; if that
        phase happens, invert this test rather than deleting it."""
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = PRIMARY
            assert resolve_tenant_id(None) == PRIMARY

    def test_leg3_none_without_primary_returns_none(self, seeded):
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = ""
            assert resolve_tenant_id(None) is None

    def test_leg3_never_returns_the_arbitrary_first_row(self, seeded):
        """The precise value the deleted function would have produced."""
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = ""
            got = resolve_tenant_id(None)
            assert got is not FIRST and got != FIRST

    def test_explicit_wins_even_with_no_primary(self, seeded):
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = ""
            assert resolve_tenant_id(OTHER) == OTHER

    def test_deleting_the_helper_did_not_touch_the_resolver(self):
        """Structural: the resolver's body must still branch explicit ->
        primary -> None, and must not reference the deleted helper."""
        tree = ast.parse(open(LOGSVC, encoding="utf-8").read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "resolve_tenant_id")
        body = ast.parse(ast.unparse(fn)).body[0]
        if (body.body and isinstance(body.body[0], ast.Expr)
                and isinstance(body.body[0].value, ast.Constant)):
            body.body.pop(0)
        src = ast.unparse(body)
        assert GONE not in src, "the arbitrary-tenant leg is back"
        assert "PRIMARY_TENANT_ID" in src, "leg 2 was removed - not this phase's scope"
        assert "return tenant_id" in src, "leg 1 was removed"
        assert src.rstrip().endswith("return None"), "leg 3 must still end in None"

    def test_resolver_is_still_the_only_exported_resolver(self):
        """If the helper were reintroduced under a different name, the module's
        public surface would grow. Pin what log_service offers for resolution."""
        tree = ast.parse(open(LOGSVC, encoding="utf-8").read())
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        resolvers = {n for n in names if "tenant_id" in n and "resolve" in n}
        assert resolvers == {"resolve_tenant_id"}, \
            f"unexpected tenant resolver(s) in log_service: {resolvers}"
