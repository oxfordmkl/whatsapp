"""Phase RC2.3E-10C — /crm/operations is discoverable in the STAFF sidebar.

THE GAP
-------
/crm/operations has been reachable by STAFF all along (check_auth(), no role
gate) and was made STAFF-SAFE by three phases: RC2.3E-3C filtered data_issues /
admission_ready / high_value_ops, RC2.3E-9 the priority_queue, RC2.3E-10A the
four automation panels. The sidebar was never updated, so the page could be
reached only by typing the URL — filtered, but undiscoverable.

WHY A SEPARATE BLOCK, NOT A WIDER GATE
--------------------------------------
The ADMIN Operations section holds three links. Adding 'STAFF' to its gate
would also expose Action Center and Staff Workload, which remain tenant-wide
and unfiltered for STAFF. So RC2.3E-10C adds a second section, gated
`actor.role == 'STAFF'`, containing exactly one link. The two gates are
mutually exclusive, so only one Operations section ever renders.

test_staff_operations_section_has_exactly_one_link is the guard against the
shortcut: it fails the moment a second item appears in the STAFF block.

WHAT THESE TESTS READ
---------------------
The sidebar is the only CRM template using `data-path` on its nav anchors
(tenant/sidebar.html is a different blueprint), so the nav can be extracted
exactly rather than by searching the whole page — page bodies print staff
names, lead names and section headings that would otherwise produce false
matches, the mistake this suite avoids by construction.

Import isolation follows test_automation_isolation_rc23e10a.py.
"""
import os
import re
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc23e10c_nav.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc23e10c-admin-key")
os.environ.setdefault("SECRET_KEY", "rc23e10c-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc23e10c-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User                                     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIDEBAR = os.path.join(ROOT, "templates", "crm_sidebar.html")

OX = "t-ox"
PAGE = "/crm/home"          # any CRM page; they all include the same partial

OPS = "/crm/operations"
ACTION = "/crm/action-center"
WORKLOAD = "/crm/staff-workload"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False


@pytest.fixture()
def seeded():
    """Seeds, then RELEASES the app context before yielding — flask_login
    caches the resolved user on flask.g, bound to the APPLICATION context, so
    a held context leaks identity between test_client requests (14B.1)."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=OX, name="Oxford", slug=OX, status="ACTIVE",
                              billing_exempt=True))
        db.session.commit()

        def mk(username, role):
            u = User(username=username, email=f"{username}@x.test",
                     password_hash=generate_password_hash("pw"), role=role,
                     tenant_id=OX if role != "SUPER_ADMIN" else None,
                     is_active=True, require_password_change=False)
            db.session.add(u)
            db.session.commit()
            return u.id

        ids = {"staff": mk("Kiran", "STAFF"),
               "admin": mk("admin_ox", "ADMIN"),
               "super": mk("platform_op", "SUPER_ADMIN")}
    yield ids
    with _APP.app_context():
        db.session.remove()


def client(uid, impersonate=None):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
        if impersonate:
            s["impersonate_tenant_id"] = impersonate
    return c


def page(uid, impersonate=None):
    r = client(uid, impersonate).get(PAGE, follow_redirects=True)
    assert r.status_code == 200, r.status_code
    return r.get_data(as_text=True)


def nav_paths(html):
    """Every sidebar destination, as a set of data-path values."""
    return set(re.findall(r'data-path="([^"]+)"', html))


def nav_labels(html):
    """Every sidebar link label, in render order."""
    return re.findall(r'<span class="nav-label"[^>]*>(.*?)</span>', html,
                      re.S)


def clean_labels(html):
    out = []
    for raw in nav_labels(html):
        txt = re.sub(r"<[^>]+>", "", raw)
        out.append(" ".join(txt.split()))
    return out


def sections(html):
    return re.findall(r'<div class="nav-section-label">([^<]+)</div>', html)


# ═══ 1-4 — STAFF ═════════════════════════════════════════════════════════════

class TestStaffNavigation:
    def test_staff_sidebar_contains_operations(self, seeded):
        assert OPS in nav_paths(page(seeded["staff"]))

    def test_staff_sidebar_contains_command_center_label(self, seeded):
        assert "Command Center" in clean_labels(page(seeded["staff"]))

    def test_staff_sidebar_has_an_operations_section(self, seeded):
        assert "Operations" in sections(page(seeded["staff"]))

    def test_staff_sidebar_excludes_action_center(self, seeded):
        html = page(seeded["staff"])
        assert ACTION not in nav_paths(html)
        assert "Action Center" not in clean_labels(html)

    def test_staff_sidebar_excludes_staff_workload(self, seeded):
        html = page(seeded["staff"])
        assert WORKLOAD not in nav_paths(html)
        assert "Staff Workload" not in clean_labels(html)

    def test_staff_operations_section_has_exactly_one_link(self, seeded):
        """THE guard against the shortcut this phase was told not to take:
        widening the ADMIN gate instead of adding a STAFF-safe section."""
        html = page(seeded["staff"])
        ops = {p for p in nav_paths(html)
               if p in (OPS, ACTION, WORKLOAD)}
        assert ops == {OPS}, ops


# ═══ 9 — nothing else widened ════════════════════════════════════════════════

class TestNothingElseWidened:
    EXPECTED_STAFF_NAV = {
        "/crm/home",
        "/crm/my-leads",
        "/crm/pipeline",
        "/crm/tasks/my",
        "/crm/operations",       # added by RC2.3E-10C
    }

    def test_staff_nav_is_exactly_the_approved_set(self, seeded):
        """Pins the WHOLE STAFF nav, so a future gate edit cannot widen it
        unnoticed. Update deliberately, never to make a test pass."""
        assert nav_paths(page(seeded["staff"])) == self.EXPECTED_STAFF_NAV

    def test_this_phase_added_exactly_one_destination(self, seeded):
        """The STAFF nav before RC2.3E-10C, reconstructed."""
        before = self.EXPECTED_STAFF_NAV - {OPS}
        assert nav_paths(page(seeded["staff"])) - before == {OPS}

    @pytest.mark.parametrize("path", [
        "/crm/leads",                 # All Leads — tenant-wide, unfiltered
        "/crm/leads/unassigned",      # admin-only; STAFF cannot assign
        "/crm/tasks/admin",
        ACTION,
        WORKLOAD,
        "/crm/staff-allocation",
        "/crm/staff-management",
        "/crm/reassignment-center",
        "/crm/staff-performance",
        "/crm/staff-dashboard",
        "/crm/marketing",
        "/crm/campaigns",
        "/crm/campaigns/center",
        "/crm/campaigns/history",
        "/crm/analytics",
        "/crm/revenue-analytics",
        "/crm/admission-analytics",
        "/crm/source-analytics",
        "/crm/health",
        "/crm/super/dashboard",
        "/panel",
    ])
    def test_admin_only_destination_stays_hidden_from_staff(self, seeded, path):
        assert path not in nav_paths(page(seeded["staff"]))


# ═══ 5-7 — ADMIN unchanged ═══════════════════════════════════════════════════

class TestAdminNavigationUnchanged:
    def test_admin_still_has_command_center(self, seeded):
        assert OPS in nav_paths(page(seeded["admin"]))
        assert "Command Center" in clean_labels(page(seeded["admin"]))

    def test_admin_still_has_action_center(self, seeded):
        assert ACTION in nav_paths(page(seeded["admin"]))
        assert "Action Center" in clean_labels(page(seeded["admin"]))

    def test_admin_still_has_staff_workload(self, seeded):
        assert WORKLOAD in nav_paths(page(seeded["admin"]))
        assert "Staff Workload" in clean_labels(page(seeded["admin"]))

    def test_admin_operations_section_still_has_all_three(self, seeded):
        html = page(seeded["admin"])
        assert {OPS, ACTION, WORKLOAD} <= nav_paths(html)

    def test_admin_sees_exactly_one_operations_section(self, seeded):
        """The two gates are mutually exclusive — an ADMIN must not get the
        STAFF block as well, which would render 'Operations' twice."""
        assert sections(page(seeded["admin"])).count("Operations") == 1

    def test_admin_command_center_appears_once(self, seeded):
        assert clean_labels(page(seeded["admin"])).count("Command Center") == 1

    def test_admin_nav_is_a_strict_superset_of_staff(self, seeded):
        assert nav_paths(page(seeded["staff"])) < nav_paths(page(seeded["admin"]))


# ═══ 8 — SUPER_ADMIN unchanged ═══════════════════════════════════════════════

class TestSuperAdminUnchanged:
    def test_super_admin_keeps_the_full_operations_section(self, seeded):
        html = page(seeded["super"], impersonate=OX)
        assert {OPS, ACTION, WORKLOAD} <= nav_paths(html)

    def test_super_admin_sees_exactly_one_operations_section(self, seeded):
        html = page(seeded["super"], impersonate=OX)
        assert sections(html).count("Operations") == 1

    def test_super_admin_keeps_its_own_dashboard_link(self, seeded):
        assert "/crm/super/dashboard" in nav_paths(page(seeded["super"],
                                                        impersonate=OX))

    def test_super_admin_nav_matches_admin_plus_super_dashboard(self, seeded):
        s = nav_paths(page(seeded["super"], impersonate=OX))
        a = nav_paths(page(seeded["admin"]))
        assert s - a == {"/crm/super/dashboard"}
        assert a - s == set()


# ═══ template structure ══════════════════════════════════════════════════════

class TestTemplateStructure:
    """Static guards — they fail on the wrong EDIT even if a rendering quirk
    hid the behavioural consequence."""

    def _src(self):
        return open(SIDEBAR, encoding="utf-8").read()

    def test_admin_operations_gate_was_not_widened(self):
        src = self._src()
        assert "{% if actor.role in ['ADMIN', 'SUPER_ADMIN', 'STAFF'] %}\n" \
               "    <div class=\"nav-section\">\n" \
               "      <div class=\"nav-section-label\">Operations</div>" not in src, \
            "the ADMIN Operations gate was widened to STAFF instead of adding " \
            "a separate STAFF-safe section"

    def test_a_staff_only_gate_exists(self):
        assert "{% if actor.role == 'STAFF' %}" in self._src()

    def test_the_staff_block_links_only_operations(self):
        """Slice the STAFF gate and assert its contents directly."""
        src = self._src()
        start = src.index("{% if actor.role == 'STAFF' %}")
        end = src.index("{% endif %}", start)
        block = src[start:end]
        assert 'data-path="/crm/operations"' in block
        assert "action-center" not in block
        assert "staff-workload" not in block
        assert block.count('class="nav-item"') == 1

    def test_gate_counts_are_unchanged_apart_from_the_new_one(self):
        """13 pre-existing gates + exactly 1 added."""
        src = self._src()
        assert src.count("{% if actor.role in ['ADMIN', 'SUPER_ADMIN'] %}") == 9
        assert src.count(
            "{% if actor.role in ['ADMIN', 'SUPER_ADMIN', 'STAFF'] %}") == 3
        assert src.count("{% if actor.role == 'SUPER_ADMIN' %}") == 1
        assert src.count("{% if actor.role == 'STAFF' %}") == 1
