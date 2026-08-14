"""Phase RC2.3E-6 — ADMIN is an elevated member of the same tenant.

THE MODEL BEING ENFORCED
------------------------
    Tenant A
    ├── ADMIN  NIBU
    ├── ADMIN  ANJU
    ├── STAFF  KIRAN
    └── STAFF  NISHA

All four are rows in `users` sharing one tenant_id. Membership IS
users.tenant_id — there is no membership table — so a role change cannot move
anyone: the only writers of role/is_active in the whole codebase are the three
paths in crm_staff_management and tenant.py:249, and none of them assigns
tenant_id.

THE TWO DEFECTS THIS CLOSES
---------------------------
1. The registry read was the only asymmetric one on the screen. All three
   write paths resolved with include_admins=True; the READ did not. Promoting
   a staff member through this screen therefore deleted them from it —
   production lost Anju (id=2) on 2026-08-14 04:33:30 — and with no row there
   is no edit modal, so the promotion could not be undone either.

2. Nothing stopped a tenant reaching ZERO active admins. BLOCK_DEACTIVATION
   counts assigned LEADS, and production's 'admin' (id=1) and 'NIBU' (id=18)
   own none, so they sail through it. A tenant with no admin has nobody who
   can promote anyone and no super-admin screen grants a role, so the state is
   unrecoverable from inside the product.

WHY THE TWO SHIP TOGETHER
-------------------------
Defect 2 is UNREACHABLE while defect 1 exists: with no admin row rendered
there is no edit modal and no toggle button, so this screen cannot strip admin
status at all. Fixing the registry is what creates the hazard. Shipping the
guard in the same change is the point, not a convenience.

WHAT IS DELIBERATELY NOT CHANGED
--------------------------------
as_registry()'s DEFAULT stays False. It feeds the assignment dropdowns and the
"Staff Active" card, where an admin genuinely does not belong (RC2.2D I1), and
37 compat tests pin that contract. Only the staff-management CALL SITE asks
for admins. active_display_names(), workload and allocation are untouched:
whether an ADMIN may own leads is a later phase.

Import isolation follows test_display_name_collision_rc23e2b.py.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc23e6_membership.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc23e6-admin-key")
os.environ.setdefault("SECRET_KEY", "rc23e6-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc23e6-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User                                     # noqa: E402
from app.services import staff_service                                  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_PY = os.path.join(ROOT, "app", "routes", "admin.py")
SVC_PY = os.path.join(ROOT, "app", "services", "staff_service.py")
DASH_HTML = os.path.join(ROOT, "templates", "crm_super_dashboard.html")

OX = "t-ox"          # two admins, two staff — the approved target model
SOLO = "t-solo"      # exactly ONE active admin — the last-admin scenarios
INACT = "t-inact"    # one active admin + one INACTIVE admin
URL = "/crm/staff-management"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False


def _mk(tenant, username, role="STAFF", display_name=None, active=True):
    u = User(username=username,
             email=f"{username}.{tenant}@x.test".replace(" ", "_"),
             password_hash=generate_password_hash("pw"), role=role,
             tenant_id=tenant, is_active=active, display_name=display_name,
             require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture()
def seeded():
    """Seeds, then RELEASES the app context before yielding.

    Holding one across test_client requests is the 14B.1 fixture bug, and it
    bit this file during development: flask_login caches the resolved user on
    flask.g, g is bound to the APPLICATION context, so a context held open
    leaked the ADMIN from an edit() request into the next request — the super
    admin dashboard then 403'd because current_user was still the admin. A
    fixture that leaks identity between requests can also PASS a test for the
    wrong reason, which is the more dangerous direction.
    """
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, nm in ((OX, "Oxford"), (SOLO, "Solo"), (INACT, "Inact")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()
        ids = {
            # The approved target model, verbatim.
            "nibu":  _mk(OX, "NIBU", role="ADMIN", display_name="Nibu").id,
            "anju":  _mk(OX, "ANJU", role="ADMIN", display_name="Anju").id,
            "kiran": _mk(OX, "KIRAN", display_name="Kiran").id,
            "nisha": _mk(OX, "NISHA", display_name="Nisha").id,
            # A tenant whose admin is its ONLY one.
            "solo_admin": _mk(SOLO, "SOLOADMIN", role="ADMIN").id,
            "solo_staff": _mk(SOLO, "SOLOSTAFF").id,
            # One active admin beside a DEACTIVATED admin.
            "ia_active":   _mk(INACT, "IAACTIVE", role="ADMIN").id,
            "ia_inactive": _mk(INACT, "IAINACTIVE", role="ADMIN",
                               active=False).id,
        }
    yield ids
    with _APP.app_context():
        db.session.remove()


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def edit(uid, code, role, active=True, display_name=""):
    data = {"action": "edit", "staff_code": code, "role": role,
            "display_name": display_name}
    if active:
        data["active"] = "on"
    return client(uid).post(URL, data=data, follow_redirects=False)


def toggle(uid, code):
    return client(uid).post(URL, data={"action": "toggle", "staff_code": code},
                            follow_redirects=False)


def snap(uid):
    with _APP.app_context():
        u = db.session.get(User, uid)
        return dict(id=u.id, username=u.username, display_name=u.display_name,
                    role=u.role, tenant_id=u.tenant_id, is_active=u.is_active)


def rendered_codes(html):
    """Codes from the TABLE, not the document — the sidebar prints the viewer's
    own name, so a page-wide search finds an admin either way."""
    import re
    body = html.split("<tbody>", 1)[-1].split("</tbody>", 1)[0]
    return set(re.findall(r'font-family: monospace[^>]*>([^<]+)<', body))


def page(uid):
    r = client(uid).get(URL, follow_redirects=True)
    assert r.status_code == 200, r.status_code
    return r.get_data(as_text=True)


def _fn_src(path, name):
    """A function's source with its docstring removed — comments never reach
    the AST, so a mention in prose cannot satisfy an assertion."""
    src = open(path, encoding="utf-8").read()
    node = [n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef) and n.name == name][0]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node = ast.Module(body=node.body[1:], type_ignores=[])
    return ast.unparse(node)


# ═══ 1 — the registry lists both roles ═══════════════════════════════════════

class TestRegistryIncludesAdmins:
    def test_all_four_members_appear(self, seeded):
        assert rendered_codes(page(seeded["nibu"])) == {
            "NIBU", "ANJU", "KIRAN", "NISHA"}

    def test_admins_appear_with_their_role_not_as_staff(self, seeded):
        """The role must be PRESERVED and shown, not flattened to STAFF."""
        with _APP.app_context():
            reg = staff_service.as_registry(OX, include_admins=True)
        assert reg["NIBU"]["role"] == "ADMIN"
        assert reg["ANJU"]["role"] == "ADMIN"
        assert reg["KIRAN"]["role"] == "STAFF"
        assert reg["NISHA"]["role"] == "STAFF"

    def test_the_rendered_page_shows_the_admin_role(self, seeded):
        html = page(seeded["nibu"])
        body = html.split("<tbody>", 1)[-1].split("</tbody>", 1)[0]
        assert "ADMIN" in body
        assert "STAFF" in body

    def test_super_admin_is_never_a_tenant_member(self, seeded):
        with _APP.app_context():
            _mk(OX, "platform_op", role="SUPER_ADMIN")
        assert "PLATFORM_OP" not in rendered_codes(page(seeded["nibu"]))

    def test_service_default_is_untouched(self, seeded):
        """The CALL SITE changed, not as_registry()'s contract — the dropdowns
        and the Staff Active card still exclude admins (RC2.2D I1)."""
        with _APP.app_context():
            assert sorted(staff_service.as_registry(OX)) == ["KIRAN", "NISHA"]

    def test_active_display_names_is_untouched(self, seeded):
        """Explicitly out of scope for this phase."""
        with _APP.app_context():
            assert staff_service.active_display_names(OX) == ["Kiran", "Nisha"]

    def test_call_site_asks_for_admins(self):
        src = _fn_src(ADMIN_PY, "crm_staff_management")
        assert "as_registry(_tenant, include_admins=True)" in src


# ═══ 2 — membership survives every role change ═══════════════════════════════

class TestMembershipInvariants:
    def test_staff_and_admin_share_one_tenant(self, seeded):
        tids = {snap(seeded[k])["tenant_id"]
                for k in ("nibu", "anju", "kiran", "nisha")}
        assert tids == {OX}

    def test_promotion_preserves_membership(self, seeded):
        before = snap(seeded["kiran"])
        assert "err=" not in edit(seeded["nibu"], "KIRAN", "ADMIN",
                                  display_name="Kiran").headers["Location"]
        after = snap(seeded["kiran"])
        assert after["role"] == "ADMIN"
        for field in ("id", "username", "tenant_id", "display_name"):
            assert after[field] == before[field], field

    def test_demotion_preserves_membership(self, seeded):
        assert "err=" not in edit(seeded["nibu"], "ANJU", "STAFF",
                                  display_name="Anju").headers["Location"]
        after = snap(seeded["anju"])
        assert after["role"] == "STAFF"
        assert after["tenant_id"] == OX
        assert after["id"] == seeded["anju"]
        assert after["username"] == "ANJU"

    def test_round_trip_returns_the_same_row(self, seeded):
        before = snap(seeded["kiran"])
        edit(seeded["nibu"], "KIRAN", "ADMIN", display_name="Kiran")
        edit(seeded["nibu"], "KIRAN", "STAFF", display_name="Kiran")
        assert snap(seeded["kiran"]) == before

    def test_promoted_member_stays_in_the_registry(self, seeded):
        """The defect itself: promotion used to delete the row from view."""
        edit(seeded["nibu"], "KIRAN", "ADMIN", display_name="Kiran")
        assert "KIRAN" in rendered_codes(page(seeded["nibu"]))

    def test_demoted_member_stays_in_the_registry(self, seeded):
        edit(seeded["nibu"], "ANJU", "STAFF", display_name="Anju")
        assert "ANJU" in rendered_codes(page(seeded["nibu"]))

    def test_no_tenant_is_created_by_a_role_change(self, seeded):
        with _APP.app_context():
            before = (Tenant.query.count(), User.query.count())
        edit(seeded["nibu"], "KIRAN", "ADMIN", display_name="Kiran")
        with _APP.app_context():
            assert (Tenant.query.count(), User.query.count()) == before

    def test_role_change_never_assigns_tenant_id(self):
        """Static proof, not a sample: the edit branch must not write it."""
        src = _fn_src(ADMIN_PY, "crm_staff_management")
        assert "tenant_id =" not in src.replace("_tenant_id =", "")


# ═══ 3 — multiple admins coexist ═════════════════════════════════════════════

class TestMultipleAdmins:
    def test_two_admins_in_one_tenant(self, seeded):
        with _APP.app_context():
            assert staff_service.active_admin_count(OX) == 2

    def test_promotion_creates_a_third(self, seeded):
        edit(seeded["nibu"], "KIRAN", "ADMIN", display_name="Kiran")
        with _APP.app_context():
            assert staff_service.active_admin_count(OX) == 3
            assert Tenant.query.count() == 3

    def test_all_admins_render_not_just_one(self, seeded):
        codes = rendered_codes(page(seeded["nibu"]))
        assert {"NIBU", "ANJU"} <= codes


# ═══ 4 — last active admin protection ════════════════════════════════════════

class TestLastAdminGuard:
    def test_last_admin_cannot_be_demoted(self, seeded):
        loc = edit(seeded["solo_admin"], "SOLOADMIN", "STAFF")
        assert "err=" in loc.headers.get("Location", "")
        assert snap(seeded["solo_admin"])["role"] == "ADMIN"

    def test_last_admin_cannot_be_deactivated_via_edit(self, seeded):
        loc = edit(seeded["solo_admin"], "SOLOADMIN", "ADMIN", active=False)
        assert "err=" in loc.headers.get("Location", "")
        assert snap(seeded["solo_admin"])["is_active"] is True

    def test_last_admin_cannot_be_deactivated_via_toggle(self, seeded):
        loc = toggle(seeded["solo_admin"], "SOLOADMIN")
        assert "err=" in loc.headers.get("Location", "")
        assert snap(seeded["solo_admin"])["is_active"] is True

    def test_demote_and_deactivate_at_once_is_also_refused(self, seeded):
        """One submission can do both; the guard tests the written values."""
        loc = edit(seeded["solo_admin"], "SOLOADMIN", "STAFF", active=False)
        assert "err=" in loc.headers.get("Location", "")
        after = snap(seeded["solo_admin"])
        assert after["role"] == "ADMIN" and after["is_active"] is True

    def test_the_error_names_the_person(self, seeded):
        loc = edit(seeded["solo_admin"], "SOLOADMIN", "STAFF")
        assert "SOLOADMIN" in loc.headers.get("Location", "")

    def test_non_last_admin_can_be_demoted(self, seeded):
        assert "err=" not in edit(seeded["nibu"], "ANJU", "STAFF",
                                  display_name="Anju").headers["Location"]
        assert snap(seeded["anju"])["role"] == "STAFF"

    def test_non_last_admin_can_be_deactivated_via_edit(self, seeded):
        edit(seeded["nibu"], "ANJU", "ADMIN", active=False, display_name="Anju")
        assert snap(seeded["anju"])["is_active"] is False

    def test_non_last_admin_can_be_deactivated_via_toggle(self, seeded):
        toggle(seeded["nibu"], "ANJU")
        assert snap(seeded["anju"])["is_active"] is False

    def test_demoting_down_to_exactly_one_is_allowed_then_stops(self, seeded):
        """Two admins -> demote one (ok) -> the survivor is now protected."""
        edit(seeded["nibu"], "ANJU", "STAFF", display_name="Anju")
        assert snap(seeded["anju"])["role"] == "STAFF"
        loc = edit(seeded["nibu"], "NIBU", "STAFF", display_name="Nibu")
        assert "err=" in loc.headers.get("Location", "")
        assert snap(seeded["nibu"])["role"] == "ADMIN"

    def test_staff_are_unaffected_by_the_guard(self, seeded):
        """A STAFF row is not an admin and must toggle freely."""
        toggle(seeded["nibu"], "NISHA")
        assert snap(seeded["nisha"])["is_active"] is False

    def test_reactivating_an_admin_is_never_blocked(self, seeded):
        toggle(seeded["nibu"], "ANJU")                 # deactivate (allowed)
        assert snap(seeded["anju"])["is_active"] is False
        toggle(seeded["nibu"], "ANJU")                 # reactivate
        assert snap(seeded["anju"])["is_active"] is True

    def test_promoting_a_staff_member_is_never_blocked(self, seeded):
        assert "err=" not in edit(seeded["solo_admin"], "SOLOSTAFF", "ADMIN",
                                  display_name="Solo Staff").headers["Location"]
        assert snap(seeded["solo_staff"])["role"] == "ADMIN"

    def test_promoting_first_then_demoting_the_original_works(self, seeded):
        """The documented recovery path out of the guard."""
        edit(seeded["solo_admin"], "SOLOSTAFF", "ADMIN",
             display_name="Solo Staff")
        assert "err=" not in edit(seeded["solo_admin"], "SOLOADMIN",
                                  "STAFF").headers["Location"]
        assert snap(seeded["solo_admin"])["role"] == "STAFF"


# ═══ 5 — the counter's contract ══════════════════════════════════════════════

class TestActiveAdminCount:
    def test_inactive_admins_do_not_count(self, seeded):
        with _APP.app_context():
            assert staff_service.active_admin_count(INACT) == 1

    def test_the_last_ACTIVE_admin_is_protected_despite_an_inactive_one(
            self, seeded):
        """An admin who cannot log in cannot administer anything, so it must
        not make the tenant look covered."""
        loc = edit(seeded["ia_active"], "IAACTIVE", "STAFF")
        assert "err=" in loc.headers.get("Location", "")
        assert snap(seeded["ia_active"])["role"] == "ADMIN"

    def test_exclude_user_id_removes_only_that_row(self, seeded):
        with _APP.app_context():
            assert staff_service.active_admin_count(
                OX, exclude_user_id=seeded["nibu"]) == 1

    def test_count_is_tenant_scoped(self, seeded):
        with _APP.app_context():
            assert staff_service.active_admin_count(SOLO) == 1
            assert staff_service.active_admin_count(OX) == 2

    def test_another_tenants_admins_do_not_satisfy_the_guard(self, seeded):
        """Oxford has two admins; SOLO's single admin must still be protected."""
        with _APP.app_context():
            assert staff_service.active_admin_count(
                SOLO, exclude_user_id=seeded["solo_admin"]) == 0
        loc = edit(seeded["solo_admin"], "SOLOADMIN", "STAFF")
        assert "err=" in loc.headers.get("Location", "")

    def test_missing_tenant_fails_closed(self, seeded):
        with _APP.app_context():
            assert staff_service.active_admin_count(None) == 0
            assert staff_service.active_admin_count("") == 0

    def test_super_admin_is_not_counted_as_a_tenant_admin(self, seeded):
        with _APP.app_context():
            _mk(SOLO, "platform2", role="SUPER_ADMIN")
            assert staff_service.active_admin_count(SOLO) == 1

    def test_counter_filters_on_is_active(self):
        src = _fn_src(SVC_PY, "active_admin_count")
        assert "is_active" in src
        assert "ADMIN" in src

    def test_counter_filters_on_tenant(self):
        src = _fn_src(SVC_PY, "active_admin_count")
        assert "tenant_id" in src


# ═══ 6 — the guard is wired to all three mutation paths ══════════════════════

class TestGuardWiring:
    def test_edit_and_toggle_both_call_the_counter(self):
        src = _fn_src(ADMIN_PY, "crm_staff_management")
        assert src.count("active_admin_count") == 2, \
            "expected one call in the edit branch and one in the toggle branch"

    def test_the_guard_excludes_the_row_being_changed(self):
        src = _fn_src(ADMIN_PY, "crm_staff_management")
        assert src.count(
            "active_admin_count(_tenant, exclude_user_id=staff.id)") == 2

    def test_the_guard_is_tenant_scoped(self):
        src = _fn_src(ADMIN_PY, "crm_staff_management")
        assert "active_admin_count(_tenant" in src

    def test_the_lead_guard_still_exists(self):
        """RC2.3E-1 Batch 3's BLOCK_DEACTIVATION must not have been replaced."""
        src = _fn_src(ADMIN_PY, "crm_staff_management")
        assert src.count("BLOCK_DEACTIVATION") == 2

    def test_the_display_name_collision_check_still_exists(self):
        """RC2.3E-2B must survive this edit."""
        src = _fn_src(ADMIN_PY, "crm_staff_management")
        assert src.count("display_name_conflict") == 2


# ═══ 7 — tenant portal is unchanged and cannot reach an admin ════════════════

class TestTenantPortalUnchanged:
    def test_tenant_staff_toggle_is_still_staff_only(self):
        src = open(os.path.join(ROOT, "app", "routes", "tenant.py"),
                   encoding="utf-8").read()
        node = _fn_src(os.path.join(ROOT, "app", "routes", "tenant.py"),
                       "tenant_staff")
        assert "role='STAFF'" in node or 'role="STAFF"' in node

    def test_tenant_portal_cannot_deactivate_an_admin(self, seeded):
        """It filters role='STAFF' on lookup, so an admin id resolves to
        nothing — the guard is not needed there and was not added."""
        c = client(seeded["solo_admin"])
        c.post("/tenant/staff",
               data={"action": "toggle", "user_id": seeded["solo_admin"]},
               follow_redirects=False)
        assert snap(seeded["solo_admin"])["is_active"] is True

    def test_tenant_portal_creates_staff_not_admins(self):
        node = _fn_src(os.path.join(ROOT, "app", "routes", "tenant.py"),
                       "tenant_staff")
        assert "role='STAFF'" in node or 'role="STAFF"' in node


# ═══ 8 — super admin dashboard: one row per tenant, with a count ═════════════

class TestSuperDashboard:
    @pytest.fixture()
    def superuser(self, seeded):
        with _APP.app_context():
            u = User(username="platform_admin", email="p@x.test",
                     password_hash=generate_password_hash("pw"),
                     role="SUPER_ADMIN", tenant_id=None, is_active=True,
                     require_password_change=False)
            db.session.add(u)
            db.session.commit()
            return u.id

    def dash(self, uid):
        r = client(uid).get("/crm/super/dashboard", follow_redirects=True)
        assert r.status_code == 200, r.status_code
        return r.get_data(as_text=True)

    def test_one_row_per_tenant(self, seeded, superuser):
        html = self.dash(superuser)
        body = html.split("<tbody>", 1)[-1].split("</tbody>", 1)[0]
        assert body.count("/t/t-ox") == 1, "Oxford must appear exactly once"
        assert body.count("<tr") == 3, "three tenants, three rows"

    def test_the_count_reflects_every_admin(self, seeded, superuser):
        assert "Admins: 2" in self.dash(superuser)

    def test_the_count_updates_with_a_promotion(self, seeded, superuser):
        edit(seeded["nibu"], "KIRAN", "ADMIN", display_name="Kiran")
        assert "Admins: 3" in self.dash(superuser)

    def test_inactive_admins_are_not_counted(self, seeded, superuser):
        """INACT holds one active and one deactivated admin."""
        html = self.dash(superuser)
        assert "Admins: 1" in html

    def test_a_promotion_creates_no_new_tenant_row(self, seeded, superuser):
        before = self.dash(superuser)
        body_before = before.split("<tbody>", 1)[-1].split("</tbody>", 1)[0]
        edit(seeded["nibu"], "KIRAN", "ADMIN", display_name="Kiran")
        body_after = self.dash(superuser).split(
            "<tbody>", 1)[-1].split("</tbody>", 1)[0]
        assert body_after.count("<tr") == body_before.count("<tr") == 3

    def test_route_passes_a_count_map(self):
        src = _fn_src(ADMIN_PY, "crm_super_dashboard")
        assert "admin_counts" in src

    def test_the_count_is_not_derived_from_the_collapsing_dict(self):
        """tenant_admins keeps ONE admin per tenant; a count taken from it
        would always read 1."""
        src = _fn_src(ADMIN_PY, "crm_super_dashboard")
        assert "len(tenant_admins" not in src

    def test_template_renders_the_count(self):
        body = open(DASH_HTML, encoding="utf-8").read()
        assert "Admins:" in body
        assert "admin_counts" in body

    def test_template_still_loops_tenants_not_admins(self):
        body = open(DASH_HTML, encoding="utf-8").read()
        assert "{% for tenant in tenants %}" in body
        assert "{% for admin in admins %}" not in body

    def test_impersonation_is_still_tenant_level(self):
        src = _fn_src(ADMIN_PY, "crm_super_impersonate")
        assert "impersonate_tenant_id" in src
        assert "impersonate_user_id" not in src


# ═══ 9 — RBAC is unchanged by this phase ═════════════════════════════════════

class TestAuthorizationUnchanged:
    def test_staff_still_cannot_reach_staff_management(self, seeded):
        assert client(seeded["kiran"]).get(URL).status_code == 403

    def test_staff_still_cannot_post_a_role_change(self, seeded):
        assert edit(seeded["kiran"], "NISHA", "ADMIN").status_code == 403
        assert snap(seeded["nisha"])["role"] == "STAFF"

    def test_an_admin_cannot_reach_another_tenants_member(self, seeded):
        """Oxford's admin submitting SOLO's code must resolve to nothing."""
        loc = edit(seeded["nibu"], "SOLOADMIN", "STAFF")
        assert "Staff+not+found" in loc.headers.get("Location", "")
        assert snap(seeded["solo_admin"])["role"] == "ADMIN"
