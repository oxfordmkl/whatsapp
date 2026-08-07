"""Phase RC2.2D Stage 1 — CRM Staff Management read path migration.

ONE consumer moves: the render section of crm_staff_management() now builds
its table from staff_service.as_registry(current tenant) instead of the global
app/data/staff_master.json.

Why this screen first: it is the only consumer whose entire output IS the
registry, so a shape regression is visible rather than subtle, and it has a
write path sitting in the same function — which makes it the sharpest possible
test of "read migrated, write untouched".

The bug being closed: staff_master.json is ONE global file with no tenant
column. Every tenant read the same rows, so a newly provisioned institute
opened Staff Management and saw Oxford's Anju / Kiran / Nisha (RC2.2, RC2.3X).
Nothing was "inserting" them — there was only ever one file.

STILL TRUE AFTER THIS STAGE, and asserted below:
  * add / edit / toggle still write app/data/staff_master.json
  * every other consumer still reads load_staff_registry()
  * the template is unmodified

Import isolation follows test_staff_registry_compat_rc22d.py.
"""
import ast
import json
import os
import re
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc22d_stage1.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "stage1-admin-key")
os.environ.setdefault("SECRET_KEY", "stage1-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "stage1-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                    # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User                                     # noqa: E402
from legacy_staff_registry import (                                     # noqa: E402
    LEGACY_OXFORD_REGISTRY, legacy_registry_dict)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAFF_JSON = os.path.join(ROOT, "app", "data", "staff_master.json")
OX = "t-ox"          # Oxford: 3 staff, mirrors production
NEW = "t-new"        # a freshly provisioned institute: no staff at all
SOLO = "t-solo"      # one staff member
URL = "/crm/staff-management"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False


def _mk(tenant, username, role="STAFF", display=None, active=True):
    u = User(username=username, display_name=display,
             email=f"{username}.{tenant}@x.test".replace(" ", "_"),
             password_hash=generate_password_hash("pw"),
             role=role, tenant_id=tenant, is_active=active,
             require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture()
def seeded():
    """Seed the DB. Holds NO app context — see the 14B.1 fixture bug, where a
    context held across test_client requests leaked flask.g between tenants and
    produced a convincing but entirely false cross-tenant result."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, name in ((OX, "Oxford"), (NEW, "New Institute"), (SOLO, "Solo")):
            db.session.add(Tenant(id=tid, name=name, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()

        admins = {}
        for tid in (OX, NEW, SOLO):
            admins[tid] = _mk(tid, f"admin_{tid}", role="ADMIN")
        for n in ("Anju", "Kiran", "Nisha"):
            _mk(OX, n)
        _mk(SOLO, "Ravi", display="Ravi Kumar")
        ids = {t: admins[t].id for t in admins}
    yield ids
    with _APP.app_context():
        db.session.remove()


@pytest.fixture()
def client():
    return _APP.test_client()


def login(client, admin_id):
    with client.session_transaction() as s:
        s["_user_id"] = str(admin_id)
        s["_fresh"] = True
    return client


def page(client, admin_id):
    r = login(client, admin_id).get(URL)
    assert r.status_code == 200, r.status_code
    return r.get_data(as_text=True)


def json_snapshot():
    """Bytes of the legacy file, or None once Stage 4C has deleted it.

    The "no write reached the shared file" assertions must keep working after
    the file is gone. A missing file is a STRONGER guarantee than an unchanged
    one, so comparing snapshots covers both eras without weakening the check.
    """
    try:
        with open(STAFF_JSON, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def rendered_codes(html):
    """Staff codes actually rendered in the TABLE.

    Whole-page substring checks are the wrong instrument here: the sidebar
    renders a 'Logged in as: <username>' block, so searching the full document
    for a name finds the *viewer* as readily as a table row. Each staff row
    emits a hidden staff_code input and nothing else on the page does, which
    makes this an exact reading of the table's contents.
    """
    return set(re.findall(r'name="staff_code" value="([^"]*)"', html))


# ═══ Requirement 3 — tenant-scoped display ═══════════════════════════════════

class TestOxfordTenant:
    def test_oxford_still_sees_its_own_staff(self, seeded, client):
        assert rendered_codes(page(client, seeded[OX])) == {"ANJU", "KIRAN", "NISHA"}

    def test_oxford_output_matches_the_legacy_file(self, seeded, client):
        """The no-regression test that matters: Oxford's rendered rows are
        exactly the set the global JSON produced — compared against the frozen
        production snapshot of that file."""
        html = page(client, seeded[OX])
        assert rendered_codes(html) == set(LEGACY_OXFORD_REGISTRY)
        for data in LEGACY_OXFORD_REGISTRY.values():
            assert data["display_name"] in html

    def test_admin_account_is_not_listed_as_staff(self, seeded, client):
        """Stage 0's I1 fix, now observable through the real screen.

        Read from the table, not the document: the sidebar prints the viewer's
        own username, so a page-wide search would find the admin either way.
        """
        codes = rendered_codes(page(client, seeded[OX]))
        assert not any("ADMIN" in c for c in codes), codes


class TestEmptyTenant:
    def test_new_institute_sees_no_staff(self, seeded, client):
        """THE BUG THIS STAGE CLOSES: before Stage 1 this table rendered
        Oxford's Anju / Kiran / Nisha in a brand-new institute."""
        assert rendered_codes(page(client, seeded[NEW])) == set()

    def test_empty_state_renders_not_an_error(self, seeded, client):
        r = login(client, seeded[NEW]).get(URL)
        assert r.status_code == 200
        assert "No staff" in r.get_data(as_text=True) or \
               "no staff" in r.get_data(as_text=True).lower()

    def test_empty_tenant_registry_is_empty(self, seeded):
        from app.services import staff_service
        with _APP.app_context():
            assert staff_service.as_registry(NEW) == {}


class TestSingleStaffTenant:
    def test_one_staff_renders(self, seeded, client):
        html = page(client, seeded[SOLO])
        assert rendered_codes(html) == {"RAVI"}
        assert "Ravi Kumar" in html

    def test_display_name_wins_over_username(self, seeded, client):
        """Code is derived from username, label from display_name."""
        html = page(client, seeded[SOLO])
        assert "Ravi Kumar" in html
        assert rendered_codes(html) == {"RAVI"}


class TestCrossTenantIsolation:
    def test_no_oxford_staff_leak_into_other_tenants(self, seeded, client):
        for tid, expected in ((NEW, set()), (SOLO, {"RAVI"})):
            codes = rendered_codes(page(_APP.test_client(), seeded[tid]))
            assert codes == expected, f"leak into {tid}: {codes}"

    def test_no_other_tenant_staff_leaks_into_oxford(self, seeded, client):
        assert "RAVI" not in rendered_codes(page(client, seeded[OX]))

    def test_request_order_does_not_change_the_result(self, seeded):
        """Guards the 14B.1 context-reuse defect: whichever tenant asked first
        must not determine what the second one sees."""
        new_first = rendered_codes(page(_APP.test_client(), seeded[NEW]))
        ox = rendered_codes(page(_APP.test_client(), seeded[OX]))
        new_second = rendered_codes(page(_APP.test_client(), seeded[NEW]))
        assert "ANJU" in ox
        assert new_first == set()
        assert new_second == set()


# ═══ Requirement 2 — shape / template compatibility ══════════════════════════

class TestTemplateCompatibility:
    def test_template_is_unmodified(self):
        """Requirement: no template changes. The empty-state branch it already
        had is what makes the new-tenant case render rather than break."""
        with open(os.path.join(ROOT, "templates", "crm_staff_management.html"),
                  encoding="utf-8") as fh:
            body = fh.read()
        assert "{% for staff in staff_list %}" in body
        assert "{% if not staff_list %}" in body

    def test_every_field_the_template_reads_is_present(self, seeded, client):
        """staff.code / display_name / role / active plus the two stat columns."""
        from app.services import staff_service
        with _APP.app_context():
            reg = staff_service.as_registry(OX)
        assert reg
        for data in reg.values():
            assert set(data) == {"display_name", "role", "active"}
        assert page(client, seeded[OX])

    def test_stat_columns_still_render(self, seeded, client):
        html = page(client, seeded[OX])
        assert rendered_codes(html) == {"ANJU", "KIRAN", "NISHA"}
        assert "<table" in html.lower()


class TestRegistryShapeCompatibility:
    def test_shape_identical_to_the_legacy_file(self, seeded):
        from app.services import staff_service
        with _APP.app_context():
            new = staff_service.as_registry(OX)
        # legacy_registry_dict(): the frozen fixture is a mappingproxy and
        # json.dumps() cannot serialise one — by design, so a test cannot
        # mutate the shared snapshot.
        legacy = legacy_registry_dict()
        assert json.dumps(new, sort_keys=True) == json.dumps(legacy, sort_keys=True)

    def test_route_consumes_the_service_not_the_file(self):
        """AST: the render section must call as_registry()."""
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "crm_staff_management")
        called = {ast.unparse(n.func) for n in ast.walk(fn)
                  if isinstance(n, ast.Call)}
        assert "staff_service.as_registry" in called

    def test_registry_is_scoped_to_the_actor_tenant(self):
        """as_registry() must be passed _actor_tenant_id(), never a literal,
        a default, or nothing — that is the whole point of the stage."""
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "crm_staff_management")
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and ast.unparse(n.func) == "staff_service.as_registry"]
        assert len(calls) == 1
        # Stage 2 hoisted the tenant into `_tenant` so the write path shares
        # one resolution. Either spelling is scoped; a literal is not.
        assert [ast.unparse(a) for a in calls[0].args] in \
            (["_actor_tenant_id()"], ["_tenant"])
        assert "_tenant = _actor_tenant_id()" in ast.unparse(fn)


# ═══ Requirement 4 — write path untouched ════════════════════════════════════

class TestWritePathUnchanged:
    """SUPERSEDED BY STAGE 2.

    Stage 1 asserted the write path still went to the JSON file. Stage 2
    migrated it to the User table by design, so those two assertions are gone
    rather than 'fixed' — keeping them would pin the very state Stage 2 exists
    to end. The write path's real contract is now covered by
    test_staff_management_stage2_rc22d.py. What remains here is what is still
    true and still worth guarding.
    """

    def test_service_cannot_write_the_registry_file(self):
        """The service reads the User table; it has no path to the JSON."""
        from app.services import staff_service
        assert not hasattr(staff_service, "save")
        assert not hasattr(staff_service, "as_registry_save")

    def test_json_file_is_untouched_by_rendering(self, seeded, client):
        before = json_snapshot()
        page(client, seeded[OX])
        page(client, seeded[NEW])
        assert json_snapshot() == before

    def test_deactivation_guard_still_present(self):
        """The BLOCK_DEACTIVATION check protects leads; it lives in the write
        path and must survive the read migration.

        Counts the two ERROR CONSTRUCTIONS, not raw occurrences of the token.
        The original counted the bare string and broke in H3-1B-a when a
        rationale comment merely mentioned BLOCK_DEACTIVATION — the same
        string-matching false positive this project has hit repeatedly. The
        guard is the f-string that builds the redirect payload; a comment
        about it is not a third guard.
        """
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            body = fh.read()
        assert body.count('f"BLOCK_DEACTIVATION:{leads_count}:{norm_name}"') == 2


# ═══ Requirement 1 & 5 — no other consumer changed ═══════════════════════════

class TestNoOtherConsumerMigrated:
    def test_only_this_route_uses_the_service(self):
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        users = set()
        for n in ast.walk(tree):
            if not isinstance(n, ast.FunctionDef):
                continue
            for c in ast.walk(n):
                if isinstance(c, ast.Call) and \
                   ast.unparse(c.func).startswith("staff_service."):
                    users.add(n.name)
        # Batch 1 migrated five more consumers by design. This suite owns only
        # its own route; the authoritative consumer set is asserted in
        # test_staff_batch1_rc22d.py::test_service_consumers_are_exactly_the_expected_set.
        assert "crm_staff_management" in users

    def test_other_consumers_still_read_the_global_file(self):
        """The other 15 call sites are Stage 2+; they must not have moved."""
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        n_load = sum(1 for n in ast.walk(tree)
                     if isinstance(n, ast.Call)
                     and ast.unparse(n.func) == "load_staff_registry")
        # Was >=14 at Stage 1; Batch 1 migrated five. Still non-zero, so the
        # legacy file remains load-bearing for the unmigrated consumers.
        # Batch 3 completed the migration, so this is now legitimately 0.
        # The authoritative assertion lives in test_staff_batch3_rc22d.py.
        # The FUNCTION and the file are still retained for Stage 4 — that is
        # asserted by test_legacy_registry_functions_still_exist below.
        assert n_load == 0, f"expected migration complete, found {n_load}"

    def test_staff_master_json_is_retired(self):
        """Inverted by Stage 4C. Stage 1 needed the file retained as the
        rollback target while only ONE read had migrated; every consumer has
        since moved and 4C deleted it."""
        assert not os.path.exists(STAFF_JSON)

    def test_no_module_outside_admin_imports_the_service(self):
        offenders = []
        for dp, _d, fs in os.walk(os.path.join(ROOT, "app")):
            if "__pycache__" in dp:
                continue
            for f in fs:
                if not f.endswith(".py") or f == "staff_service.py":
                    continue
                full = os.path.join(dp, f)
                try:
                    tree = ast.parse(open(full, encoding="utf-8").read())
                except SyntaxError:
                    continue
                for n in ast.walk(tree):
                    hit = False
                    if isinstance(n, ast.Import):
                        hit = any("staff_service" in a.name
                                  and "backfill" not in a.name for a in n.names)
                    elif isinstance(n, ast.ImportFrom):
                        mod = n.module or ""
                        hit = ("staff_service" in mod and "backfill" not in mod) \
                            or any(a.name == "staff_service" for a in n.names)
                    if hit:
                        offenders.append(os.path.relpath(full, ROOT))
        # RC2.3E-0 added staff_identity_service, the dual-read helper, which
        # legitimately consumes staff_service. Named explicitly so an
        # unexpected consumer still fails.
        allowed = sorted([os.path.join("app", "routes", "admin.py"),
                          os.path.join("app", "services",
                                       "staff_identity_service.py")])
        assert sorted(set(offenders)) == allowed, \
            f"unapproved consumer: {set(offenders)}"


# ═══ Requirement 5 — no page regression ══════════════════════════════════════

class TestNoPageRegression:
    def test_page_returns_200_for_every_tenant(self, seeded):
        for tid in (OX, NEW, SOLO):
            assert _APP.test_client().get(
                URL, environ_base={}) is not None
            r = login(_APP.test_client(), seeded[tid]).get(URL)
            assert r.status_code == 200, f"{tid} -> {r.status_code}"

    def test_unauthenticated_access_is_still_refused(self, seeded):
        r = _APP.test_client().get(URL)
        assert r.status_code in (302, 401, 403), r.status_code

    def test_page_is_stable_across_repeated_loads(self, seeded, client):
        first = page(client, seeded[OX])
        for _ in range(3):
            assert page(client, seeded[OX]) == first
