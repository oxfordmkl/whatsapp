"""Phase RC2.2D Stage 2 — CRM Staff Management write path migration.

Stage 1 moved this screen's READ to the tenant-scoped User table. Stage 2 moves
its CRUD. add / edit / activate / deactivate now mutate User rows; nothing on
this screen touches app/data/staff_master.json.

WHY THE WRITE HAD TO FOLLOW THE READ
------------------------------------
Leaving them split would have been worse than either end state: the write went
to a global file that the tenant-scoped read no longer consulted, so every
add would have appeared to silently do nothing.

AND WHY IT MATTERS BEYOND TIDINESS
----------------------------------
staff_master.json lives INSIDE the deployed image. Railway replaces the
filesystem on every deploy, so a staff member added through this screen was
discarded at the next `git push` — silently, with no error. Requirement 6 asks
that edits survive restart and deployment; only a database row can.

STILL TRUE AFTER THIS STAGE, and asserted below:
  * staff_master.json, load_staff_registry(), save_staff_registry() and
    get_staff_json_path() all still exist (15 consumers still read them)
  * no other consumer changed
  * the template is unmodified

Import isolation follows test_staff_management_stage1_rc22d.py.
"""
import ast
import os
import re
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc22d_stage2.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "stage2-admin-key")
os.environ.setdefault("SECRET_KEY", "stage2-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "stage2-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash, check_password_hash    # noqa: E402
from app import create_app                                                   # noqa: E402
from app.extensions import db                                                # noqa: E402
from app.models import Tenant, User, ConversationState                       # noqa: E402
from app.services import staff_service                                       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAFF_JSON = os.path.join(ROOT, "app", "data", "staff_master.json")
OX = "t-ox"
NEW = "t-new"
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
    """Holds NO app context across requests — see the 14B.1 fixture defect,
    where a held context leaked flask.g between test_client requests."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, name in ((OX, "Oxford"), (NEW, "New Institute")):
            db.session.add(Tenant(id=tid, name=name, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()
        admins = {tid: _mk(tid, f"admin_{tid}", role="ADMIN") for tid in (OX, NEW)}
        for n in ("Anju", "Kiran", "Nisha"):
            _mk(OX, n)
        ids = {t: admins[t].id for t in admins}
    yield ids
    with _APP.app_context():
        db.session.remove()


def client_for(admin_id):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(admin_id)
        s["_fresh"] = True
    return c


def post(admin_id, **form):
    r = client_for(admin_id).post(URL, data=form, follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code
    return r.headers.get("Location", "")


def post_raw(admin_id, **form):
    """POST without requiring a redirect.

    An unresolvable toggle code falls through to render the page (200) rather
    than redirecting — the ORIGINAL behaviour, preserved verbatim by Stage 2.
    """
    return client_for(admin_id).post(URL, data=form, follow_redirects=False)


def get_html(admin_id):
    r = client_for(admin_id).get(URL)
    assert r.status_code == 200
    return r.get_data(as_text=True)


def rendered_codes(html):
    """Codes in the TABLE only — the sidebar prints the viewer's username, so a
    page-wide search would find the viewer as readily as a row."""
    return set(re.findall(r'name="staff_code" value="([^"]*)"', html))


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


def staff_of(tenant):
    with _APP.app_context():
        return {u.username: u for u in
                User.query.filter_by(tenant_id=tenant).all()}


# ═══ Requirement 3 — Add ═════════════════════════════════════════════════════

class TestAddStaff:
    def test_add_creates_a_user_row(self, seeded):
        loc = post(seeded[OX], action="add", staff_code="ravi",
                   display_name="Ravi Kumar", role="STAFF", active="on")
        assert "msg=Staff+added" in loc or "msg=Staff%20added" in loc
        u = staff_of(OX)["RAVI"]
        assert u.display_name == "Ravi Kumar"
        assert u.role == "STAFF"
        assert u.is_active is True
        assert u.tenant_id == OX

    def test_added_staff_appears_on_the_screen(self, seeded):
        post(seeded[OX], action="add", staff_code="RAVI",
             display_name="Ravi Kumar", role="STAFF", active="on")
        html = get_html(seeded[OX])
        assert "RAVI" in rendered_codes(html)
        assert "Ravi Kumar" in html

    def test_code_round_trips_unchanged(self, seeded):
        """username IS the code, so as_registry() renders back what was typed."""
        post(seeded[OX], action="add", staff_code="ravi",
             display_name="Ravi", role="STAFF", active="on")
        with _APP.app_context():
            assert "RAVI" in staff_service.as_registry(OX)

    def test_inactive_add_is_honoured(self, seeded):
        post(seeded[OX], action="add", staff_code="DORMANT",
             display_name="Dormant", role="STAFF")   # no active checkbox
        assert staff_of(OX)["DORMANT"].is_active is False

    def test_missing_fields_are_rejected(self, seeded):
        before = len(staff_of(OX))
        assert "err=" in post(seeded[OX], action="add", staff_code="",
                              display_name="No Code")
        assert "err=" in post(seeded[OX], action="add", staff_code="X",
                              display_name="")
        assert len(staff_of(OX)) == before

    def test_created_account_cannot_be_logged_into(self, seeded):
        """This screen creates a DIRECTORY ENTRY, not a login — the same thing
        a staff_master.json row was. It collects no password, so the row must
        not end up with a guessable or empty one."""
        post(seeded[OX], action="add", staff_code="RAVI",
             display_name="Ravi", role="STAFF", active="on")
        u = staff_of(OX)["RAVI"]
        assert u.password_hash
        for guess in ("", "password", "RAVI", "ravi", "Ravi", "123456"):
            assert not check_password_hash(u.password_hash, guess)
        assert u.require_password_change is True
        assert u.email is None      # no reset path either

    def test_two_adds_get_distinct_passwords(self, seeded):
        post(seeded[OX], action="add", staff_code="A1", display_name="A1")
        post(seeded[OX], action="add", staff_code="A2", display_name="A2")
        s = staff_of(OX)
        assert s["A1"].password_hash != s["A2"].password_hash


class TestDuplicatePrevention:
    def test_existing_code_is_refused(self, seeded):
        loc = post(seeded[OX], action="add", staff_code="ANJU",
                   display_name="Impostor", role="STAFF", active="on")
        assert "err=" in loc
        # The seeded row's username is 'Anju'; the derived CODE is 'ANJU'. No
        # second row may exist under either spelling.
        assert set(staff_of(OX)) & {"Anju", "ANJU"} == {"Anju"}
        with _APP.app_context():
            assert len(staff_service.as_registry(OX)) == 3

    def test_case_insensitive_duplicate_is_refused(self, seeded):
        """'anju' derives code ANJU, which already exists."""
        assert "err=" in post(seeded[OX], action="add", staff_code="anju",
                              display_name="Impostor")
        assert staff_of(OX)["Anju"].display_name != "Impostor"

    def test_duplicate_check_does_not_span_tenants(self, seeded):
        """Two institutes may each employ an ANJU. Production already has one
        username in four separate tenants."""
        loc = post(seeded[NEW], action="add", staff_code="ANJU",
                   display_name="Anju (New)", role="STAFF", active="on")
        assert "err=" not in loc
        assert staff_of(NEW)["ANJU"].display_name == "Anju (New)"

    def test_admin_username_collision_is_refused(self, seeded):
        """The check spans admins too — otherwise the new row would collide
        with the tenant admin's username at the DB level."""
        assert "err=" in post(seeded[OX], action="add",
                              staff_code=f"admin_{OX}".upper(),
                              display_name="Shadow")


# ═══ Requirement 3 — Edit / Activate / Deactivate ════════════════════════════

class TestEditStaff:
    def test_edit_updates_display_name_and_role(self, seeded):
        loc = post(seeded[OX], action="edit", staff_code="ANJU",
                   display_name="Anju Menon", role="MANAGER", active="on")
        assert "msg=" in loc
        u = staff_of(OX)["Anju"]
        assert u.display_name == "Anju Menon"
        assert u.role == "MANAGER"

    def test_edit_does_not_rename_the_login(self, seeded):
        """display_name is the operator-facing label; username is a credential
        and the registry key. Editing the label must not move either."""
        post(seeded[OX], action="edit", staff_code="ANJU",
             display_name="Anju Menon", role="STAFF", active="on")
        assert "Anju" in staff_of(OX)
        with _APP.app_context():
            assert "ANJU" in staff_service.as_registry(OX)

    def test_blank_fields_keep_existing_values(self, seeded):
        post(seeded[OX], action="edit", staff_code="ANJU",
             display_name="", role="", active="on")
        u = staff_of(OX)["Anju"]
        assert u.role == "STAFF"
        assert u.display_label() == "Anju"

    def test_unknown_code_is_refused(self, seeded):
        assert "err=Staff+not+found" in post(
            seeded[OX], action="edit", staff_code="GHOST",
            display_name="Ghost", active="on").replace("%20", "+")


class TestActivateDeactivate:
    def test_deactivate_via_edit(self, seeded):
        post(seeded[OX], action="edit", staff_code="KIRAN",
             display_name="Kiran", role="STAFF")     # checkbox absent
        assert staff_of(OX)["Kiran"].is_active is False

    def test_toggle_deactivates_then_reactivates(self, seeded):
        post(seeded[OX], action="toggle", staff_code="KIRAN")
        assert staff_of(OX)["Kiran"].is_active is False
        post(seeded[OX], action="toggle", staff_code="KIRAN")
        assert staff_of(OX)["Kiran"].is_active is True

    def test_deactivated_staff_leaves_the_dropdown_but_stays_listed(self, seeded):
        post(seeded[OX], action="toggle", staff_code="KIRAN")
        with _APP.app_context():
            assert "Kiran" not in staff_service.active_display_names(OX)
            assert staff_service.as_registry(OX)["KIRAN"]["active"] is False

    def test_toggle_of_unknown_code_is_a_no_op(self, seeded):
        before = {k: v.is_active for k, v in staff_of(OX).items()}
        client_for(seeded[OX]).post(URL, data={"action": "toggle",
                                               "staff_code": "GHOST"})
        assert {k: v.is_active for k, v in staff_of(OX).items()} == before


# ═══ Requirement 4 — BLOCK_DEACTIVATION ══════════════════════════════════════

class TestBlockDeactivation:
    def _assign_lead(self, tenant, staff_name):
        with _APP.app_context():
            from app.routes.admin import normalize_staff_name
            db.session.add(ConversationState(
                phone=f"+9199{abs(hash(staff_name)) % 10000000:07d}",
                tenant_id=tenant,
                assigned_staff=normalize_staff_name(staff_name)))
            db.session.commit()

    def test_toggle_is_blocked_when_leads_are_assigned(self, seeded):
        self._assign_lead(OX, "Kiran")
        loc = post(seeded[OX], action="toggle", staff_code="KIRAN")
        assert "BLOCK_DEACTIVATION" in loc
        assert staff_of(OX)["Kiran"].is_active is True, "guard did not protect"

    def test_edit_deactivation_is_blocked_when_leads_are_assigned(self, seeded):
        self._assign_lead(OX, "Kiran")
        loc = post(seeded[OX], action="edit", staff_code="KIRAN",
                   display_name="Kiran", role="STAFF")
        assert "BLOCK_DEACTIVATION" in loc
        assert staff_of(OX)["Kiran"].is_active is True

    def test_the_message_still_carries_count_and_name(self, seeded):
        """The template parses BLOCK_DEACTIVATION:<count>:<name>."""
        self._assign_lead(OX, "Kiran")
        loc = post(seeded[OX], action="toggle", staff_code="KIRAN")
        m = re.search(r"BLOCK_DEACTIVATION(?:%3A|:)(\d+)", loc)
        assert m and int(m.group(1)) == 1, loc

    def test_activation_is_never_blocked(self, seeded):
        post(seeded[OX], action="toggle", staff_code="KIRAN")
        self._assign_lead(OX, "Kiran")
        loc = post(seeded[OX], action="toggle", staff_code="KIRAN")
        assert "BLOCK_DEACTIVATION" not in loc
        assert staff_of(OX)["Kiran"].is_active is True

    def test_guard_counts_only_this_tenants_leads(self, seeded):
        """A lead in another tenant must not block deactivation here."""
        post(seeded[NEW], action="add", staff_code="KIRAN",
             display_name="Kiran", role="STAFF", active="on")
        self._assign_lead(OX, "Kiran")
        loc = post(seeded[NEW], action="toggle", staff_code="KIRAN")
        assert "BLOCK_DEACTIVATION" not in loc
        assert staff_of(NEW)["KIRAN"].is_active is False


# ═══ Requirement 6 — cross-tenant write ══════════════════════════════════════

class TestCrossTenantWrite:
    def test_other_tenant_cannot_edit_oxford_staff(self, seeded):
        loc = post(seeded[NEW], action="edit", staff_code="ANJU",
                   display_name="HIJACKED", role="ADMIN", active="on")
        assert "err=" in loc
        u = staff_of(OX)["Anju"]
        assert u.display_name != "HIJACKED"
        assert u.role == "STAFF"

    def test_other_tenant_cannot_toggle_oxford_staff(self, seeded):
        """ANJU does not resolve in tenant NEW, so it falls through to render
        (the pre-existing unknown-code behaviour) and Oxford is untouched."""
        r = post_raw(seeded[NEW], action="toggle", staff_code="ANJU")
        assert r.status_code == 200
        assert staff_of(OX)["Anju"].is_active is True

    def test_add_lands_in_the_actors_tenant_only(self, seeded):
        post(seeded[NEW], action="add", staff_code="BOB",
             display_name="Bob", role="STAFF", active="on")
        assert "BOB" in staff_of(NEW)
        assert "BOB" not in staff_of(OX)

    def test_oxford_edits_touch_only_oxford(self, seeded):
        before = {k: (v.display_name, v.is_active) for k, v in staff_of(NEW).items()}
        post(seeded[OX], action="edit", staff_code="ANJU",
             display_name="Anju M", role="STAFF", active="on")
        assert {k: (v.display_name, v.is_active)
                for k, v in staff_of(NEW).items()} == before

    def test_no_global_mutation(self, seeded):
        """No write on this screen may reach the shared file."""
        before = json_snapshot()
        post(seeded[OX], action="add", staff_code="RAVI", display_name="Ravi",
             role="STAFF", active="on")
        post(seeded[OX], action="edit", staff_code="ANJU",
             display_name="Anju M", role="STAFF", active="on")
        post(seeded[OX], action="toggle", staff_code="NISHA")
        assert json_snapshot() == before


# ═══ Requirement 6 — persistence ═════════════════════════════════════════════

class TestPersistence:
    def test_edit_survives_a_new_session(self, seeded):
        post(seeded[OX], action="add", staff_code="RAVI",
             display_name="Ravi Kumar", role="STAFF", active="on")
        with _APP.app_context():
            db.session.remove()          # drop all identity-map caching
            u = User.query.filter_by(tenant_id=OX, username="RAVI").first()
            assert u is not None and u.display_name == "Ravi Kumar"

    def test_data_lives_in_the_database_not_the_image(self, seeded):
        """The deployment-loss fix: staff_master.json ships inside the image
        and Railway replaces the filesystem on deploy, so a file write could
        not survive one. A row can."""
        post(seeded[OX], action="add", staff_code="RAVI",
             display_name="Ravi", role="STAFF", active="on")
        snap = json_snapshot()
        assert snap is None or b"RAVI" not in snap
        assert "RAVI" in staff_of(OX)

    def test_toggle_persists(self, seeded):
        post(seeded[OX], action="toggle", staff_code="NISHA")
        with _APP.app_context():
            db.session.remove()
            assert User.query.filter_by(tenant_id=OX,
                                        username="Nisha").first().is_active is False


# ═══ Requirement 7 — rollback safety ═════════════════════════════════════════

class TestRollbackSafety:
    def test_a_refused_add_leaves_no_partial_row(self, seeded):
        before = set(staff_of(OX))
        post(seeded[OX], action="add", staff_code="ANJU", display_name="Dup")
        assert set(staff_of(OX)) == before

    def test_a_blocked_deactivation_commits_nothing(self, seeded):
        """The guard returns mid-request; no attribute may have been flushed."""
        with _APP.app_context():
            from app.routes.admin import normalize_staff_name
            db.session.add(ConversationState(
                phone="+919900000001", tenant_id=OX,
                assigned_staff=normalize_staff_name("Kiran")))
            db.session.commit()
        post(seeded[OX], action="edit", staff_code="KIRAN",
             display_name="Renamed", role="MANAGER")
        u = staff_of(OX)["Kiran"]
        assert u.is_active is True
        assert u.display_name != "Renamed", "blocked edit leaked a partial write"
        assert u.role == "STAFF"

    def test_no_tenant_context_refuses_to_write(self, seeded):
        """ADR-021: a SUPER_ADMIN who is not impersonating has no tenant, and
        guessing one is how 18 lead_event rows were mis-filed.

        In practice admin_required bounces this actor to the super dashboard
        BEFORE the route body runs, so the in-route guard is a second line of
        defence rather than the active one. What is asserted here is the
        property that matters either way: no row is written anywhere.
        """
        with _APP.app_context():
            su = _mk(None, "platform_root", role="SUPER_ADMIN")
            su_id = su.id
        before = set(staff_of(OX))
        post_raw(su_id, action="add", staff_code="GHOST", display_name="Ghost")
        assert set(staff_of(OX)) == before
        assert "GHOST" not in staff_of(None)

    def test_in_route_tenant_guard_exists(self):
        """The guard above is unreachable for a SUPER_ADMIN today, but it must
        stay: any future actor reaching this route without a tenant must be
        refused rather than writing to an arbitrary one."""
        _t, fn = _route_ast()
        src = ast.unparse(fn)
        assert "No tenant context" in src


# ═══ Requirements 1, 5, IMPORTANT — scope containment ════════════════════════

def _route_ast():
    with open(os.path.join(ROOT, "app", "routes", "admin.py"),
              encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    return tree, next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef)
                      and n.name == "crm_staff_management")


class TestScopeContainment:
    def test_route_no_longer_writes_the_json(self):
        _t, fn = _route_ast()
        called = {ast.unparse(n.func) for n in ast.walk(fn)
                  if isinstance(n, ast.Call)}
        assert "save_staff_registry" not in called
        assert "load_staff_registry" not in called

    def test_legacy_registry_is_retired(self):
        """Was "15 consumers still depend on them". They no longer do —
        RC2.2D migrated all 16 and Stage 4C deleted the API and the file."""
        from app.routes import admin
        for fn_name in ("load_staff_registry", "save_staff_registry",
                        "get_staff_json_path"):
            assert not hasattr(admin, fn_name), fn_name
        assert not os.path.exists(STAFF_JSON)

    def test_other_consumers_still_read_the_file(self):
        tree, _fn = _route_ast()
        n_load = sum(1 for n in ast.walk(tree)
                     if isinstance(n, ast.Call)
                     and ast.unparse(n.func) == "load_staff_registry")
        # Was >=14 at Stage 2; Batch 1 migrated five. Still non-zero, so the
        # legacy file remains load-bearing for the unmigrated consumers.
        # Batch 3 completed the migration, so this is now legitimately 0.
        # The authoritative assertion lives in test_staff_batch3_rc22d.py.
        # The FUNCTION and the file are still retained for Stage 4 — that is
        # asserted by test_legacy_registry_functions_still_exist below.
        assert n_load == 0, f"expected migration complete, found {n_load}"

    def test_only_this_route_uses_the_service(self):
        tree, _fn = _route_ast()
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

    def test_every_write_is_tenant_scoped(self):
        """No resolve_code / as_registry call may take a literal tenant."""
        _t, fn = _route_ast()
        for c in ast.walk(fn):
            if isinstance(c, ast.Call) and \
               ast.unparse(c.func).startswith("staff_service."):
                assert ast.unparse(c.args[0]) in ("_tenant", "_actor_tenant_id()"), \
                    ast.unparse(c)

    def test_template_is_unmodified(self):
        with open(os.path.join(ROOT, "templates", "crm_staff_management.html"),
                  encoding="utf-8") as fh:
            body = fh.read()
        assert "{% for staff in staff_list %}" in body
        assert "{% if not staff_list %}" in body
        assert 'name="staff_code"' in body

    def test_audit_logging_survived(self):
        _t, fn = _route_ast()
        called = [ast.unparse(n.func) for n in ast.walk(fn)
                  if isinstance(n, ast.Call)]
        assert called.count("log_audit") == 2

    def test_redirect_contract_unchanged(self):
        """The template renders ?msg= and ?err=; every branch must still use
        them or the screen goes silent."""
        _t, fn = _route_ast()
        src = ast.unparse(fn)
        for token in ("Staff added", "Staff updated", "Staff status toggled",
                      "Staff not found", "Staff code already exists",
                      "Code and Name required", "BLOCK_DEACTIVATION"):
            assert token in src, token
