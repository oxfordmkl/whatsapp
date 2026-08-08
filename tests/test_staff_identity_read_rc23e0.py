"""Phase RC2.3E-0 — dual-read owner resolution helper.

Infrastructure only. Nothing consumes staff_identity_service yet, so the
load-bearing tests here are the DORMANCY tests and the PARITY tests:

  * dormancy — no module imports it, and nothing except it reads
    STAFF_IDENTITY_READ_FK. If either breaks, the single-point-of-truth
    property that makes the flag a working rollback switch is gone.
  * parity — with the flag OFF the helper reproduces the LIVE implementations
    (admin.normalize_staff_name, the {normalized: display} mapping
    calculate_workload_scoring builds) rather than a hand-written
    expectation, so the two cannot drift apart unnoticed.

The flag is manipulated via os.environ because app/flags.py re-reads the
environment on every call — which is precisely the property that makes
rollback a toggle rather than a redeploy, so the tests exercise it the same
way production would.

Import isolation follows test_staff_batch3_rc22d.py.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc23e0_dualread.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc23e0-admin-key")
os.environ.setdefault("SECRET_KEY", "rc23e0-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc23e0-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, ConversationState, Task            # noqa: E402
from app.services import staff_identity_service as sid                  # noqa: E402
from app.routes.admin import normalize_staff_name                       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLAG = "STAFF_IDENTITY_READ_FK"
OX = "t-ox"
OTHER = "t-other"
EMPTY = "t-empty"

_APP = create_app()


@pytest.fixture(autouse=True)
def flag_off():
    """Every test starts with the flag OFF and restores it afterwards.

    Autouse so a test that flips the flag cannot leak that state into the
    next — the shared-mutable-fixture failure the RC2.2G Stage 4B fixture was
    made immutable to avoid.
    """
    before = os.environ.get(FLAG)
    os.environ.pop(FLAG, None)
    yield
    if before is None:
        os.environ.pop(FLAG, None)
    else:
        os.environ[FLAG] = before


def on():
    os.environ[FLAG] = "true"


def off():
    os.environ.pop(FLAG, None)


def _mk(tenant, username, display=None, role="STAFF", active=True):
    u = User(username=username, display_name=display,
             email=f"{username}.{tenant}@x.test".replace(" ", "_"),
             password_hash=generate_password_hash("pw"),
             role=role, tenant_id=tenant, is_active=active,
             require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u


def _lead(tenant, phone, staff=None, user=None):
    row = ConversationState(phone=phone, tenant_id=tenant, lead_status="Lead",
                            assigned_staff=normalize_staff_name(staff) if staff else None,
                            assigned_user_id=user.id if user else None)
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, nm in ((OX, "Oxford"), (OTHER, "Other"), (EMPTY, "Empty")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()
        anju = _mk(OX, "Anju")
        kiran = _mk(OX, "Kiran")
        gone = _mk(OX, "Old Staff", active=False)
        ravi = _mk(OTHER, "Ravi")

        _lead(OX, "+919000000001", "Anju", anju)
        _lead(OX, "+919000000002", "Anju", anju)
        _lead(OX, "+919000000003", "Kiran", kiran)
        _lead(OX, "+919000000004", None, None)              # unassigned
        _lead(OX, "+919000000005", "Anju_display", None)    # the phantom
        _lead(OTHER, "+919100000001", "Ravi", ravi)
        yield {"anju": anju.id, "kiran": kiran.id,
               "gone": gone.id, "ravi": ravi.id}
        db.session.remove()


# ═══ The flag is read in exactly one place ═══════════════════════════════════

class TestSinglePointOfTruth:
    def test_read_fk_enabled_reflects_the_environment(self):
        assert sid.read_fk_enabled() is False
        on()
        assert sid.read_fk_enabled() is True
        off()
        assert sid.read_fk_enabled() is False

    def test_flag_is_re_read_per_call_not_cached(self):
        """This is what makes rollback a toggle instead of a redeploy."""
        for expected in (False, True, False, True, False):
            on() if expected else off()
            assert sid.read_fk_enabled() is expected

    def test_no_consumer_reads_the_flag_directly(self):
        """Every migrated reader must resolve through this module, or a
        rollback toggle would revert only some of them.

        AST, not string matching: staff_backfill_service MENTIONS
        STAFF_IDENTITY_READ_FK in its docstring while explaining that it is
        still OFF. Counting that as a read is the same false positive this
        project has hit repeatedly — only a real call or name reference
        counts.
        """
        offenders = []
        for dp, _d, fs in os.walk(os.path.join(ROOT, "app")):
            if "__pycache__" in dp:
                continue
            for f in fs:
                if not f.endswith(".py") or f in ("flags.py",
                                                  "staff_identity_service.py"):
                    continue
                full = os.path.join(dp, f)
                rel = os.path.relpath(full, ROOT)
                try:
                    tree = ast.parse(open(full, encoding="utf-8").read())
                except SyntaxError:
                    continue
                for n in ast.walk(tree):
                    # a call to the accessor, however imported
                    if isinstance(n, ast.Call) and \
                            ast.unparse(n.func).endswith(
                                "staff_identity_read_fk_enabled"):
                        offenders.append(f"{rel}:{n.lineno} call")
                    # importing the accessor or the constant
                    elif isinstance(n, ast.ImportFrom):
                        if any(a.name in ("staff_identity_read_fk_enabled",
                                          "STAFF_IDENTITY_READ_FK")
                               for a in n.names):
                            offenders.append(f"{rel}:{n.lineno} import")
                    # bare reference to the constant in real code
                    elif isinstance(n, ast.Name) and \
                            n.id == "STAFF_IDENTITY_READ_FK":
                        offenders.append(f"{rel}:{n.lineno} name")
        assert offenders == [], f"flag read outside the helper: {offenders}"


# ═══ Dormancy — no consumer yet ══════════════════════════════════════════════

class TestDormancy:
    def test_no_module_imports_the_helper(self):
        """RC2.3E-1 migrates the first consumer. Until then, nothing."""
        offenders = []
        for dp, _d, fs in os.walk(os.path.join(ROOT, "app")):
            if "__pycache__" in dp:
                continue
            for f in fs:
                if not f.endswith(".py") or f == "staff_identity_service.py":
                    continue
                full = os.path.join(dp, f)
                try:
                    tree = ast.parse(open(full, encoding="utf-8").read())
                except SyntaxError:
                    continue
                for n in ast.walk(tree):
                    if isinstance(n, ast.Import):
                        if any("staff_identity_service" in a.name for a in n.names):
                            offenders.append(os.path.relpath(full, ROOT))
                    elif isinstance(n, ast.ImportFrom):
                        if ("staff_identity_service" in (n.module or "")
                                or any(a.name == "staff_identity_service"
                                       for a in n.names)):
                            offenders.append(os.path.relpath(full, ROOT))
        # H3-1B-a imports staff_identity_service into admin.py for the write
        # validator. The READ helpers (owner_key/owner_column/staff_keys) are
        # still consumed by nothing — that is RC2.3E-1 — and
        # test_no_consumer_reads_the_flag_directly still guards the flag.
        # H3-1B-a imported it into admin.py (write validator); H3-1B-b into
        # task_service.py (same validator, task paths).
        #
        # RC2.3E-1 ends the READ dormancy this test used to describe. Batch 3
        # wired owner_filter() into the deactivation guard and Batch 1a wired
        # it into the three ownership filters, which is what adds
        # sales_pipeline_service.py here. The allowlist is EXTENDED rather
        # than relaxed, so an unexpected consumer still fails, and
        # test_no_consumer_reads_the_flag_directly still guards the flag —
        # that invariant is the one that must never move.
        allowed = sorted([os.path.join("app", "routes", "admin.py"),
                          os.path.join("app", "services", "task_service.py"),
                          os.path.join("app", "services",
                                       "sales_pipeline_service.py")])
        assert sorted(set(offenders)) == allowed,             f"unapproved consumer: {set(offenders)}"

    def test_helper_never_writes(self):
        with open(os.path.join(ROOT, "app", "services",
                               "staff_identity_service.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        writes = {"commit", "add", "add_all", "delete", "flush", "merge"}
        offenders = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr in writes:
                recv = n.func.value
                if getattr(recv, "attr", None) or getattr(recv, "id", None) \
                        in ("session", "db", "query"):
                    offenders.add(ast.unparse(n.func))
        assert offenders == set(), offenders

    def test_no_flask_import(self):
        """Framework independence — callers pass tenant_id."""
        with open(os.path.join(ROOT, "app", "services",
                               "staff_identity_service.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                assert "flask" not in (n.module or ""), ast.unparse(n)
            if isinstance(n, ast.Import):
                assert not any("flask" in a.name for a in n.names)


# ═══ Parity with the live implementation, flag OFF ═══════════════════════════

class TestNormParityWithLiveImplementation:
    CASES = ["Anju", "anju", "ANJU", "  Kiran  ", "nibu s s", "Bibin Thomas",
             "Anju_display", "", "   ", None, "o'brien", "jean-luc"]

    def test_norm_matches_normalize_staff_name(self):
        """Pins the mirror. Three copies of this rule now exist; a divergence
        here would silently re-bucket every aggregate."""
        for c in self.CASES:
            assert sid._norm(c) == normalize_staff_name(c), repr(c)

    def test_blank_normalizes_to_the_unassigned_bucket(self):
        """normalize_staff_name returns 'Unassigned', NOT '' — several
        consumers render that as a real heading."""
        for blank in (None, "", "   "):
            assert sid._norm(blank) == "Unassigned"


class TestFlagOffReproducesTodaysBehaviour:
    def test_owner_column_is_the_string(self, seeded):
        assert sid.owner_column(ConversationState) is ConversationState.assigned_staff
        assert sid.owner_column(Task) is Task.assigned_staff

    def test_owner_key_is_the_normalized_name(self, seeded):
        with _APP.app_context():
            row = ConversationState.query.filter_by(phone="+919000000001").first()
            assert sid.owner_key(row) == "Anju"
            assert sid.owner_key(row) == normalize_staff_name(row.assigned_staff)

    def test_unassigned_row_keys_to_the_unassigned_bucket(self, seeded):
        with _APP.app_context():
            row = ConversationState.query.filter_by(phone="+919000000004").first()
            assert sid.owner_key(row) == "Unassigned"
            assert sid.is_unassigned(sid.owner_key(row))

    def test_phantom_owner_keeps_its_own_bucket(self, seeded):
        """'Anju_display' has no User row. Under the string regime it is still
        its own bucket — today's behaviour, preserved."""
        with _APP.app_context():
            row = ConversationState.query.filter_by(phone="+919000000005").first()
            assert sid.owner_key(row) == "Anju_Display"
            assert not sid.is_unassigned(sid.owner_key(row))

    def test_staff_keys_matches_the_workload_scoring_mapping(self, seeded):
        """calculate_workload_scoring builds {normalize(display): display} by
        hand. The helper must produce exactly that."""
        from app.services import staff_service
        with _APP.app_context():
            expected = {normalize_staff_name(n): n
                        for n in staff_service.active_display_names(OX)}
            assert sid.staff_keys(OX) == expected

    def test_key_from_value_matches_owner_key(self, seeded):
        with _APP.app_context():
            for phone in ("+919000000001", "+919000000004", "+919000000005"):
                row = ConversationState.query.filter_by(phone=phone).first()
                assert sid.key_from_value(row.assigned_staff) == sid.owner_key(row)


# ═══ Flag ON ═════════════════════════════════════════════════════════════════

class TestFlagOn:
    def test_owner_column_is_the_fk(self, seeded):
        on()
        assert sid.owner_column(ConversationState) is ConversationState.assigned_user_id
        assert sid.owner_column(Task) is Task.assigned_user_id

    def test_owner_key_is_the_user_id(self, seeded):
        on()
        with _APP.app_context():
            row = ConversationState.query.filter_by(phone="+919000000001").first()
            assert sid.owner_key(row) == seeded["anju"]

    def test_unassigned_row_keys_to_none(self, seeded):
        on()
        with _APP.app_context():
            row = ConversationState.query.filter_by(phone="+919000000004").first()
            assert sid.owner_key(row) is None
            assert sid.is_unassigned(sid.owner_key(row))

    def test_phantom_becomes_unassigned(self, seeded):
        """THE ONE PRODUCTION BEHAVIOUR CHANGE the flip causes: a row whose
        string owner resolves to no User (production has exactly one,
        'Anju_display') moves from its own bucket to unassigned."""
        on()
        with _APP.app_context():
            row = ConversationState.query.filter_by(phone="+919000000005").first()
            assert row.assigned_staff == "Anju_Display"
            assert sid.owner_key(row) is None
            assert sid.is_unassigned(sid.owner_key(row))

    def test_staff_keys_are_user_ids(self, seeded):
        on()
        with _APP.app_context():
            assert sid.staff_keys(OX) == {seeded["anju"]: "Anju",
                                          seeded["kiran"]: "Kiran"}

    def test_key_from_value_passes_ids_through(self, seeded):
        on()
        assert sid.key_from_value(7) == 7
        assert sid.key_from_value(None) is None

    def test_display_for_key_resolves_through_the_user(self, seeded):
        on()
        with _APP.app_context():
            assert sid.display_for_key(OX, seeded["anju"]) == "Anju"


# ═══ Both regimes agree where they must ══════════════════════════════════════

class TestRegimeEquivalence:
    def test_same_buckets_for_resolvable_rows(self, seeded):
        """The migration's core promise: for a row whose owner resolves, both
        regimes put it with the same PERSON — different key, same display."""
        with _APP.app_context():
            rows = ConversationState.query.filter(
                ConversationState.tenant_id == OX,
                ConversationState.assigned_user_id.isnot(None)).all()
            off()
            by_string = {r.phone: sid.display_for_key(OX, sid.owner_key(r))
                         for r in rows}
            on()
            by_fk = {r.phone: sid.display_for_key(OX, sid.owner_key(r))
                     for r in rows}
        assert by_string == by_fk, (by_string, by_fk)

    def test_unassigned_agrees_in_both_regimes(self, seeded):
        with _APP.app_context():
            row = ConversationState.query.filter_by(phone="+919000000004").first()
            off()
            assert sid.display_for_key(OX, sid.owner_key(row)) == "Unassigned"
            on()
            assert sid.display_for_key(OX, sid.owner_key(row)) == "Unassigned"

    def test_staff_keys_cover_the_same_people(self, seeded):
        with _APP.app_context():
            off()
            names_off = set(sid.staff_keys(OX).values())
            on()
            names_on = set(sid.staff_keys(OX).values())
        assert names_off == names_on == {"Anju", "Kiran"}


# ═══ Tenant isolation ════════════════════════════════════════════════════════

class TestTenantIsolation:
    def test_staff_keys_are_tenant_scoped_in_both_regimes(self, seeded):
        with _APP.app_context():
            for flip in (off, on):
                flip()
                assert set(sid.staff_keys(OX).values()) == {"Anju", "Kiran"}
                assert set(sid.staff_keys(OTHER).values()) == {"Ravi"}
                assert sid.staff_keys(EMPTY) == {}

    def test_missing_tenant_fails_closed(self, seeded):
        with _APP.app_context():
            for flip in (off, on):
                flip()
                assert sid.staff_keys(None) == {}
                assert sid.staff_keys("no-such-tenant") == {}

    def test_display_for_key_will_not_render_another_tenants_user(self, seeded):
        """A key from another tenant must resolve to nothing, not to that
        tenant's staff name."""
        on()
        with _APP.app_context():
            assert sid.display_for_key(OX, seeded["ravi"]) is None
            assert sid.display_for_key(OTHER, seeded["anju"]) is None

    def test_owner_filter_is_tenant_safe_by_construction(self, seeded):
        """owner_filter narrows to ONE user; tenant scoping remains the
        caller's tenant_query()/tenant_filter() responsibility, and the
        predicate cannot widen it."""
        with _APP.app_context():
            ravi = User.query.get(seeded["ravi"])
            for flip in (off, on):
                flip()
                rows = ConversationState.query.filter(
                    ConversationState.tenant_id == OX,
                    sid.owner_filter(ConversationState, ravi)).all()
                assert rows == []


# ═══ owner_filter ════════════════════════════════════════════════════════════

class TestOwnerFilter:
    def test_selects_the_users_rows_in_both_regimes(self, seeded):
        with _APP.app_context():
            anju = User.query.get(seeded["anju"])
            for flip in (off, on):
                flip()
                rows = ConversationState.query.filter(
                    ConversationState.tenant_id == OX,
                    sid.owner_filter(ConversationState, anju)).all()
                assert {r.phone for r in rows} == {"+919000000001",
                                                   "+919000000002"}, flip

    def test_none_user_matches_nothing(self, seeded):
        with _APP.app_context():
            for flip in (off, on):
                flip()
                rows = ConversationState.query.filter(
                    sid.owner_filter(ConversationState, None)).all()
                assert rows == []

    def test_string_regime_is_case_and_whitespace_insensitive(self, seeded):
        """Reproduces the lower(trim()) idiom _build_leads_query uses, so
        ownership resolves identically to today."""
        off()
        with _APP.app_context():
            u = _mk(OX, "  MiXeD  ")
            _lead(OX, "+919000000009", "mixed", None)
            rows = ConversationState.query.filter(
                ConversationState.tenant_id == OX,
                sid.owner_filter(ConversationState, u)).all()
            assert {r.phone for r in rows} == {"+919000000009"}


# ═══ Rollback ════════════════════════════════════════════════════════════════

class TestRollbackBehaviour:
    def test_toggling_the_flag_switches_regimes_immediately(self, seeded):
        with _APP.app_context():
            row = ConversationState.query.filter_by(phone="+919000000001").first()
            off()
            assert sid.owner_key(row) == "Anju"
            on()
            assert sid.owner_key(row) == seeded["anju"]
            off()
            assert sid.owner_key(row) == "Anju", "rollback did not restore"

    def test_rollback_restores_the_phantoms_bucket(self, seeded):
        """The one row the flip changes must come BACK on rollback."""
        with _APP.app_context():
            row = ConversationState.query.filter_by(phone="+919000000005").first()
            off()
            before = sid.owner_key(row)
            on()
            assert sid.owner_key(row) is None
            off()
            assert sid.owner_key(row) == before == "Anju_Display"

    def test_no_state_is_cached_between_toggles(self, seeded):
        with _APP.app_context():
            for _ in range(3):
                off()
                a = sid.staff_keys(OX)
                on()
                b = sid.staff_keys(OX)
                assert set(a) != set(b)
                assert set(a.values()) == set(b.values())

    def test_flag_values_are_interpreted_consistently(self):
        for truthy in ("true", "True", "1", "yes", "on"):
            os.environ[FLAG] = truthy
            assert sid.read_fk_enabled() is True, truthy
        for falsy in ("false", "False", "0", "no", "off", ""):
            os.environ[FLAG] = falsy
            assert sid.read_fk_enabled() is False, falsy


# ═══ Scope containment ═══════════════════════════════════════════════════════

class TestScopeContainment:
    def test_no_authorization_helper_is_exposed(self):
        """RC2.3E discovery blocked permissions: get_current_actor() carries no
        user_id, and the legacy completion path has no Task row. Shipping an
        authorization helper here would invite exactly that migration."""
        for name in ("authorize", "authorize_owner", "can_edit", "is_owner",
                     "owner_permits"):
            assert not hasattr(sid, name), name

    def test_no_schema_or_migration_change(self):
        versions = os.path.join(ROOT, "migrations", "versions")
        assert not [f for f in os.listdir(versions) if "rc23e" in f.lower()]

    def test_dual_write_untouched(self):
        from app.services import staff_backfill_service as bf
        assert callable(bf.sync_assigned_user)
        from app import flags
        assert callable(flags.staff_identity_dual_write_enabled)

    def test_public_api_is_the_documented_surface(self):
        expected = {"read_fk_enabled", "owner_column", "owner_key",
                    "key_from_value", "is_unassigned", "staff_keys",
                    "display_for_key", "owner_filter", "UNASSIGNED"}
        public = {n for n in dir(sid) if not n.startswith("_")
                  and n not in ("logging", "logger")}
        assert expected <= public, expected - public
