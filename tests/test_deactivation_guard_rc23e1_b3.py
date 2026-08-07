"""Phase RC2.3E-1 Batch 3 — the deactivation guard reads through the helper.

FIRST RUNTIME CONSUMER of staff_identity_service.owner_filter(). Until this
phase every read helper in that module was dormant.

WHAT THIS FIXES
---------------
BLOCK_DEACTIVATION counted a staff member's leads with

    ConversationState.assigned_staff == normalize_staff_name(name)

which is case-SENSITIVE against a title-cased name. Leads stored in any other
spelling were invisible to the guard. Production holds both 'Kiran' and
'kiran': the count read 24 where the truth was 27. A staff member whose leads
were ALL lowercase would have counted 0 and been deactivated while still
owning live leads — the exact outcome the guard exists to prevent.

BOTH REGIMES
------------
owner_filter() spans the flip: with STAFF_IDENTITY_READ_FK off it compares
lower(trim(assigned_staff)) to the display label; with it on, assigned_user_id
to the user id. Every behavioural test below therefore runs under BOTH flag
states — that is the point of a dual-read helper, and a test that only ever
exercised one regime would not notice the other breaking.

TWO CALL SITES
--------------
The edit path and the toggle path carry the same guard. They were already an
exact duplicate; a fix applied to one only would be worse than neither, so the
tests exercise both and a structural test pins that neither keeps the old
case-sensitive predicate.

Import isolation follows test_assignment_reject_h3_1b_a.py.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc23e1b3_guard.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc23e1b3-admin-key")
os.environ.setdefault("SECRET_KEY", "rc23e1b3-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc23e1b3-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, ConversationState                  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OX = "t-ox"
OTHER = "t-other"
URL = "/crm/staff-management"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False

# Every behavioural test runs twice — once per regime.
REGIMES = [pytest.param(False, id="flag_off_names"),
           pytest.param(True, id="flag_on_fk")]


@pytest.fixture()
def regime(request):
    """Set STAFF_IDENTITY_READ_FK for one test, restore it after."""
    before = os.environ.get("STAFF_IDENTITY_READ_FK")
    os.environ["STAFF_IDENTITY_DUAL_WRITE"] = "true"
    if request.param:
        os.environ["STAFF_IDENTITY_READ_FK"] = "true"
    else:
        os.environ.pop("STAFF_IDENTITY_READ_FK", None)
    yield request.param
    if before is None:
        os.environ.pop("STAFF_IDENTITY_READ_FK", None)
    else:
        os.environ["STAFF_IDENTITY_READ_FK"] = before


def _mk(tenant, username, role="STAFF", active=True):
    u = User(username=username, email=f"{username}.{tenant}@x.test".replace(" ", "_"),
             password_hash=generate_password_hash("pw"), role=role,
             tenant_id=tenant, is_active=active, require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u


def _lead(phone, tenant, staff, uid):
    db.session.add(ConversationState(
        phone=phone, tenant_id=tenant, name=f"L{phone[-3:]}",
        lead_status="Lead", assigned_staff=staff, assigned_user_id=uid))


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, nm in ((OX, "Oxford"), (OTHER, "Other")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()
        ids = {"kiran": _mk(OX, "Kiran").id,
               "anju": _mk(OX, "Anju").id,
               "idle": _mk(OX, "Idle").id,
               "lower": _mk(OX, "Lower").id,
               "ravi": _mk(OTHER, "Ravi").id,
               "admin": _mk(OX, "admin_ox", role="ADMIN").id}

        # Kiran: mixed spellings — the production shape.
        _lead("919200000001", OX, "Kiran", ids["kiran"])
        _lead("919200000002", OX, "kiran", ids["kiran"])
        _lead("919200000003", OX, "  KIRAN ", ids["kiran"])
        # Lower: EVERY lead lowercase — counted 0 by the old predicate.
        _lead("919200000004", OX, "lower", ids["lower"])
        _lead("919200000005", OX, "lower", ids["lower"])
        # Anju: single canonical spelling.
        _lead("919200000006", OX, "Anju", ids["anju"])
        # Another tenant's lead named for an Oxford staff member.
        _lead("919200000077", OTHER, "Kiran", None)
        db.session.commit()
        yield ids
        db.session.remove()


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def toggle(uid, code):
    return client(uid).post(URL, data={"action": "toggle", "staff_code": code},
                            follow_redirects=False)


def edit(uid, code, active, display_name="X", role="STAFF"):
    data = {"action": "edit", "staff_code": code,
            "display_name": display_name, "role": role}
    if active:
        data["active"] = "on"
    return client(uid).post(URL, data=data, follow_redirects=False)


def blocked(resp):
    return "BLOCK_DEACTIVATION" in resp.headers.get("Location", "")


def blocked_count(resp):
    loc = resp.headers.get("Location", "")
    if "BLOCK_DEACTIVATION" not in loc:
        return None
    import urllib.parse
    err = urllib.parse.unquote_plus(loc.split("err=")[1])
    return int(err.split(":")[1])


def is_active(uid):
    with _APP.app_context():
        return User.query.get(uid).is_active


# ═══ the defect this batch fixes ═════════════════════════════════════════════

class TestCaseInsensitiveOwnership:

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_all_lowercase_owner_is_still_protected(self, seeded, regime):
        """THE defect. 'Lower' owns two leads, both spelled 'lower'. The old
        case-sensitive predicate counted 0 and let the deactivation through."""
        r = toggle(seeded["admin"], "LOWER")
        assert blocked(r), "a staff member with live leads was deactivated"
        assert is_active(seeded["lower"]) is True

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_mixed_spellings_are_all_counted(self, seeded, regime):
        """Kiran owns 3 leads spelled 'Kiran', 'kiran' and '  KIRAN '. The old
        predicate reported 1 — the production undercount, in miniature."""
        r = toggle(seeded["admin"], "KIRAN")
        assert blocked(r)
        assert blocked_count(r) == 3

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_count_is_identical_in_both_regimes(self, seeded, regime):
        """The flip must not move the number."""
        assert blocked_count(toggle(seeded["admin"], "KIRAN")) == 3
        assert blocked_count(toggle(seeded["admin"], "ANJU")) == 1


# ═══ the guard's ordinary contract ═══════════════════════════════════════════

class TestGuardBehaviour:

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_staff_with_no_leads_can_be_deactivated(self, seeded, regime):
        r = toggle(seeded["admin"], "IDLE")
        assert not blocked(r)
        assert is_active(seeded["idle"]) is False

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_edit_path_blocks_too(self, seeded, regime):
        r = edit(seeded["admin"], "KIRAN", active=False)
        assert blocked(r)
        assert is_active(seeded["kiran"]) is True

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_edit_path_allows_a_staff_member_with_no_leads(self, seeded, regime):
        r = edit(seeded["admin"], "IDLE", active=False)
        assert not blocked(r)
        assert is_active(seeded["idle"]) is False

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_both_paths_agree(self, seeded, regime):
        """They were an exact duplicate; they must stay in agreement."""
        assert blocked(toggle(seeded["admin"], "KIRAN")) is \
               blocked(edit(seeded["admin"], "KIRAN", active=False))
        assert blocked(toggle(seeded["admin"], "IDLE")) is \
               blocked(edit(seeded["admin"], "IDLE", active=False))

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_activating_is_never_blocked(self, seeded, regime):
        r = edit(seeded["admin"], "KIRAN", active=True)
        assert not blocked(r)

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_error_message_shape_is_unchanged(self, seeded, regime):
        """The template parses BLOCK_DEACTIVATION:<count>:<name>."""
        import urllib.parse
        loc = toggle(seeded["admin"], "KIRAN").headers.get("Location", "")
        err = urllib.parse.unquote_plus(loc.split("err=")[1])
        parts = err.split(":")
        assert parts[0] == "BLOCK_DEACTIVATION"
        assert parts[1].isdigit()
        assert parts[2] == "Kiran", "the displayed name stays normalized"


# ═══ tenant isolation ════════════════════════════════════════════════════════

class TestTenantScoping:

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_another_tenants_leads_do_not_protect_local_staff(self, seeded, regime):
        """The other tenant has a lead named 'Kiran' too. It must not count
        toward Oxford's guard — and must not be counted for Idle either."""
        r = toggle(seeded["admin"], "IDLE")
        assert not blocked(r)

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_count_excludes_foreign_tenant_rows(self, seeded, regime):
        assert blocked_count(toggle(seeded["admin"], "KIRAN")) == 3


# ═══ structural ══════════════════════════════════════════════════════════════

class TestStructure:

    def _fn(self, name="crm_staff_management"):
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        return next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name), tree

    def test_guard_uses_owner_filter_at_both_call_sites(self):
        fn, _ = self._fn()
        assert ast.unparse(fn).count("owner_filter") == 2

    def test_old_case_sensitive_predicate_is_gone(self):
        """The defect must not survive at either call site."""
        fn, _ = self._fn()
        src = ast.unparse(fn)
        assert "assigned_staff == norm_name" not in src

    def test_guard_passes_the_tenant_explicitly(self):
        """Consistency with resolve_code() above and the RC2.2F convention.

        Deliberately a STRUCTURAL assertion only, because there is no
        behavioural difference to assert: tenant_query()'s SUPER_ADMIN branch
        returns before it reads the argument, and for a normal admin
        `tenant_id or current_user.tenant_id` gives the same value. Asserting
        a behaviour here would be asserting a fiction.
        """
        fn, _ = self._fn()
        src = ast.unparse(fn)
        assert src.count("tenant_query(ConversationState, _tenant)") == 2

    def test_helper_remains_the_only_flag_reader(self):
        """The dual-read contract: consumers must never test the flag
        themselves. Three suites already assert this; Batch 3 is the first
        runtime consumer and so the first real chance to break it."""
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "crm_staff_management":
                assert "STAFF_IDENTITY_READ_FK" not in ast.unparse(n)
                assert "read_fk_enabled" not in ast.unparse(n)

    def test_no_schema_or_migration_change(self):
        versions = os.path.join(ROOT, "migrations", "versions")
        assert not [f for f in os.listdir(versions)
                    if "rc23e" in f.lower() or "h3" in f.lower()]

    def test_flag_default_is_still_off(self):
        from app import flags
        before = os.environ.pop("STAFF_IDENTITY_READ_FK", None)
        try:
            assert flags.staff_identity_read_fk_enabled() is False
        finally:
            if before is not None:
                os.environ["STAFF_IDENTITY_READ_FK"] = before
