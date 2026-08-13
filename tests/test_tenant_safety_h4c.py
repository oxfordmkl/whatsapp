"""Phase H4-c — resolve_tenant_id() stops guessing a tenant.

THE TRAP
--------
resolve_tenant_id() had three legs:

    1. explicit tenant_id
    2. PRIMARY_TENANT_ID          (warned)
    3. _get_default_tenant_id()   = Tenant.query.first()  <- ARBITRARY

Leg 3 is the exact mechanism behind TD-P0-1 / the Phase 17.1-C mis-filing
incident. Its own comment said it "never executes in production" — but that
was a claim about configuration, not a guarantee: it fires whenever
PRIMARY_TENANT_ID is unset, and nothing enforces that it is set.

WHAT CHANGED
------------
Leg 3 is gone. Leg 2 now logs at ERROR rather than WARNING. When neither
answers, the function returns None and says so loudly.

WHY None RATHER THAN A REFUSAL OR A GUESS
-----------------------------------------
MessageLog / ConversationMessage / LeadEvent all declare tenant_id
nullable=False, so a None reaches the DB as an IntegrityError, caught by each
writer's own try/except and logged. That costs one log line. The alternative
cost a row silently attributed to another customer. In a multi-tenant CRM the
lost line is recoverable and the cross-tenant write is not.

Making those columns nullable — so unattributed rows become visible orphans
instead of failures, as log_audit already does — is a migration and was
deliberately NOT taken in this phase.

BEHAVIOUR IN PRODUCTION IS UNCHANGED
------------------------------------
PRIMARY_TENANT_ID is configured in production, so leg 2 has always answered
before leg 3 could. This removes a trap, not a working path. The discovery
audit found ZERO rows carrying the leg-3 signature.

log_audit IS NOT TOUCHED
------------------------
It has no fallback at all: it writes tenant_id as given, so platform events
(LOGIN_FAILURE, SUPER_ADMIN logins) legitimately land as NULL. Production
holds 19 such rows and they are correct. A guard there would break platform
login auditing, so this suite asserts it still accepts None.
"""
import ast
import logging
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_h4c_tenant.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "h4c-admin-key")
os.environ.setdefault("SECRET_KEY", "h4c-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "h4c-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-primary")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, AuditLog, LeadEvent                      # noqa: E402
from app.services.log_service import resolve_tenant_id                  # noqa: E402
from app.services import log_service                                    # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGSVC = os.path.join(ROOT, "app", "services", "log_service.py")
AUDITSVC = os.path.join(ROOT, "app", "services", "audit_service.py")

PRIMARY = "t-primary"
OTHER = "t-other"
FIRST = "t-aaa-first"   # sorts first; what Tenant.query.first() would return

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False


@pytest.fixture()
def seeded():
    """THREE tenants, with a non-primary one created FIRST.

    Ordering matters: if leg 3 ever returns, Tenant.query.first() yields
    't-aaa-first', which is neither the explicit tenant nor PRIMARY — so the
    arbitrary guess is distinguishable from both. A single-tenant fixture
    would hide exactly the bug this phase removes.
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


# ═══ the resolution contract ═════════════════════════════════════════════════

class TestResolution:

    def test_explicit_tenant_always_wins(self, seeded):
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = PRIMARY
            assert resolve_tenant_id(OTHER) == OTHER

    def test_falls_back_to_primary_when_configured(self, seeded):
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = PRIMARY
            assert resolve_tenant_id(None) == PRIMARY

    def test_primary_fallback_logs_at_error(self, seeded, caplog):
        """Raised from WARNING so it is alertable, not lost in the noise."""
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = PRIMARY
            with caplog.at_level(logging.ERROR):
                resolve_tenant_id(None)
        assert any("implicit resolution" in r.message for r in caplog.records
                   if r.levelno >= logging.ERROR), caplog.text

    def test_returns_none_when_nothing_resolves(self, seeded):
        """THE FIX. This used to return Tenant.query.first() — an arbitrary
        tenant, and the TD-P0-1 mis-filing mechanism."""
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = ""
            assert resolve_tenant_id(None) is None

    def test_never_returns_the_arbitrary_first_tenant(self, seeded):
        """The specific value leg 3 would have produced."""
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = ""
            assert resolve_tenant_id(None) != FIRST

    def test_unresolved_case_is_logged_loudly(self, seeded, caplog):
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = ""
            with caplog.at_level(logging.ERROR):
                resolve_tenant_id(None)
        assert any("UNRESOLVED" in r.message for r in caplog.records), caplog.text

    def test_explicit_wins_even_with_no_primary(self, seeded):
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = ""
            assert resolve_tenant_id(OTHER) == OTHER


# ═══ the writers must not mis-attribute ══════════════════════════════════════

class TestWritersDoNotMisAttribute:

    def test_lead_event_never_lands_in_an_arbitrary_tenant(self, seeded, caplog):
        """With no tenant and no PRIMARY, the row must NOT appear under
        t-aaa-first. Losing it is acceptable; mis-filing it is not.

        The session is rolled back before asserting because the failed flush
        poisons it — see test_failed_write_poisons_the_session below, which is
        the caveat this phase surfaced.
        """
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = ""
            with caplog.at_level(logging.ERROR):
                log_service.log_lead_event(phone="919900000001",
                                           event_type="TEST_EVENT")
            db.session.rollback()
            assert LeadEvent.query.filter_by(tenant_id=FIRST).count() == 0
            assert LeadEvent.query.filter_by(tenant_id=PRIMARY).count() == 0
            assert LeadEvent.query.filter_by(phone="919900000001").count() == 0

    def test_the_writer_does_not_raise(self, seeded):
        """Each writer wraps its body in try/except, so an unresolved tenant
        never propagates an exception to the caller."""
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = ""
            log_service.log_lead_event(phone="919900000002",
                                       event_type="TEST_EVENT")
            db.session.rollback()
            log_service.log_message(phone="919900000002", direction="out",
                                    message_type="text", message_text="hi")
            db.session.rollback()
            log_service.save_conversation_message(
                phone="919900000002", direction="out", message="hi")

    def test_failed_write_no_longer_poisons_the_session(self, seeded):
        """INVERTED by H4-d, which is exactly what this test asked for.

        It previously asserted the OPPOSITE: that a failed write left the
        session in PendingRollbackError, because log_message and
        log_lead_event caught their exception without rolling back. My H4-c
        discovery had claimed the None return "costs one lost log line" — true
        only for save_conversation_message, which already rolled back. A test
        failure surfaced that, not foresight.

        H4-d added the same guarded rollback to the other two writers under
        its own approval, so the caveat is gone: the write is still lost, but
        the session survives and the rest of the request completes.

        Kept rather than deleted — the history of why those two writers differ
        from the third is worth more than a clean file.
        """
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = ""
            log_service.log_lead_event(phone="919900000004",
                                       event_type="TEST_EVENT")
            # No rollback() here on purpose: the writer must have done it.
            assert LeadEvent.query.filter_by(phone="919900000004").count() == 0

    def test_save_conversation_message_recovers_on_its_own(self, seeded):
        """The one writer that already rolls back — the shape the other two
        should adopt."""
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = ""
            log_service.save_conversation_message(
                phone="919900000005", direction="out", message="hi")
            assert LeadEvent.query.count() >= 0  # session still usable

    def test_explicit_tenant_still_writes_normally(self, seeded):
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = PRIMARY
            log_service.log_lead_event(phone="919900000003",
                                       event_type="TEST_EVENT",
                                       tenant_id=OTHER)
            rows = LeadEvent.query.filter_by(phone="919900000003").all()
            assert rows and all(r.tenant_id == OTHER for r in rows)


# ═══ log_audit is deliberately untouched ═════════════════════════════════════

class TestAuditServiceUnchanged:

    def test_log_audit_still_accepts_a_none_tenant(self, seeded):
        """Platform events legitimately have no tenant: LOGIN_FAILURE has no
        resolved user, and a SUPER_ADMIN has no tenant of their own.
        Production holds 19 such rows and they are CORRECT. A guard here would
        break platform login auditing."""
        from app.services.audit_service import log_audit
        with _APP.app_context():
            log_audit("LOGIN_FAILURE", actor="nobody@x.test",
                      target="/crm/login", detail={"reason": "test"})
            rows = AuditLog.query.filter_by(action="LOGIN_FAILURE").all()
            assert rows, "platform audit row was refused"
            assert all(r.tenant_id is None for r in rows)

    def test_audit_service_has_no_tenant_fallback(self):
        """It must never grow one: guessing a tenant for a login failure would
        attribute an unauthenticated attempt to a real customer."""
        with open(AUDITSVC, encoding="utf-8") as fh:
            src = fh.read()
        assert "resolve_tenant_id" not in src
        assert "_get_default_tenant_id" not in src


# ═══ structural tripwires ════════════════════════════════════════════════════

class TestStructure:

    def _fn(self, path, name):
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        m = ast.parse(ast.unparse(fn)).body[0]
        if (m.body and isinstance(m.body[0], ast.Expr)
                and isinstance(m.body[0].value, ast.Constant)):
            m.body.pop(0)
        return ast.unparse(m)

    def test_arbitrary_leg_is_gone(self):
        src = self._fn(LOGSVC, "resolve_tenant_id")
        assert "_get_default_tenant_id" not in src, \
            "the arbitrary-tenant fallback is back"

    def test_the_arbitrary_resolver_has_no_caller_anywhere(self):
        """It is still DEFINED — deleting it was outside the approved scope —
        so this guards the thing that actually matters: nothing calls it."""
        callers = []
        for dp, _d, fs in os.walk(os.path.join(ROOT, "app")):
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
                for n in ast.walk(tree):
                    if isinstance(n, ast.Call) and \
                            ast.unparse(n.func).split(".")[-1] == "_get_default_tenant_id":
                        callers.append(os.path.relpath(p, ROOT))
        assert callers == [], f"_get_default_tenant_id was wired again: {callers}"

    def test_primary_branch_logs_at_error_not_warning(self):
        src = self._fn(LOGSVC, "resolve_tenant_id")
        i = src.index("implicit resolution")
        window = src[max(0, i - 200):i]
        assert "logging.error" in window, "downgraded back to warning"

    def test_none_is_returned_not_guessed(self):
        src = self._fn(LOGSVC, "resolve_tenant_id")
        assert src.rstrip().endswith("return None")

    def test_no_caller_was_modified(self):
        """H4-c is confined to resolve_tenant_id(). All 42 call sites already
        pass a tenant; none needed changing."""
        import subprocess
        out = subprocess.run(["git", "status", "--porcelain", "--", "app/"],
                             cwd=ROOT, capture_output=True, text=True).stdout
        dirty = sorted(l.split()[-1] for l in out.splitlines()
                       if l.strip() and not l.endswith("screens.py"))
        assert dirty in ([], ["app/services/log_service.py"]), dirty

    def test_h4_route_resolution_still_closed(self):
        """H4-a/H4-b must not regress while H4-c lands."""
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        remaining = {n.name for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)
                     and "getattr(current_user, 'tenant_id'" in ast.unparse(n)}
        assert remaining == {"_actor_tenant_id", "check_billing_status"}, \
            sorted(remaining)

    def test_no_schema_or_migration_change(self):
        import subprocess
        out = subprocess.run(["git", "status", "--porcelain", "--", "migrations/"],
                             cwd=ROOT, capture_output=True, text=True).stdout.strip()
        assert out == "", out

    def test_columns_are_still_not_null(self):
        """Records why None degrades to a lost line rather than an orphan row.
        Making these nullable is the follow-up migration this phase declined."""
        from app.models import MessageLog, ConversationMessage
        for model in (MessageLog, ConversationMessage, LeadEvent):
            assert model.__table__.c.tenant_id.nullable is False, model.__name__
        assert AuditLog.__table__.c.tenant_id.nullable is True
