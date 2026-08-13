"""Phase H4-d — a failed log write costs a log line, not the request.

THE DEFECT
----------
log_message() and log_lead_event() caught their exception and logged it, but
did NOT roll back. A failed flush leaves the SQLAlchemy 2.x Session inactive,
so every later query in the SAME app context raises PendingRollbackError.

Flask-SQLAlchemy 3.1 scopes one session per app context (_app_ctx_id), so a
daemon thread — which opens its own context — is self-contained. The damage
lands on IN-REQUEST calls, where the route shares its session with the log
write. Four lead-assignment routes do further DB work after logging:

    crm_lead_update
    crm_unassigned_assign
    crm_auto_assign_confirm
    crm_reassignment_confirm

REACHABILITY IS WIDER THAN THE UNRESOLVED-TENANT CASE
-----------------------------------------------------
ANY exception in those try blocks poisons the session. Only the TEXT bodies
are truncated (_MAX_TEXT); the bounded VARCHARs are written untruncated —
wa_message_id(100), staff_name(100), event_type(50), phone(20), direction(10),
message_type(20) — so an overlong value raises DataError on Postgres. A
deleted tenant raises IntegrityError on the FK. Transient DB faults do it too.
None of those need PRIMARY_TENANT_ID to be unset.

THE FIX
-------
The guarded rollback save_conversation_message() has always had, copied
verbatim into the other two. The nested try matters: a failing rollback must
not mask the original error. Rollback runs BEFORE the log call, because
logging is not what breaks — the next query is.

Success paths are untouched: the rollback only executes on a path that has
already failed.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_h4d_rollback.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "h4d-admin-key")
os.environ.setdefault("SECRET_KEY", "h4d-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "h4d-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import (Tenant, ConversationState, LeadEvent,           # noqa: E402
                        MessageLog, ConversationMessage)
from app.services import log_service                                    # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGSVC = os.path.join(ROOT, "app", "services", "log_service.py")
OX = "t-ox"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=OX, name="Oxford", slug=OX, status="ACTIVE",
                              billing_exempt=True))
        db.session.add(ConversationState(
            phone="919990000001", tenant_id=OX, name="Lead",
            lead_status="Lead"))
        db.session.commit()
        yield
        db.session.remove()


def _force_failure():
    """Make the next log write fail on the NOT NULL tenant_id column.

    resolve_tenant_id() returns None with no explicit tenant and no PRIMARY,
    which is the cheapest reproducible failure. The point of these tests is
    the SESSION STATE afterwards, not this particular cause — a DataError from
    an overlong VARCHAR would poison it identically.
    """
    _APP.config["PRIMARY_TENANT_ID"] = ""


def _restore():
    _APP.config["PRIMARY_TENANT_ID"] = OX


# ═══ the session survives a failed write ═════════════════════════════════════

class TestSessionSurvives:

    def test_log_lead_event_leaves_the_session_usable(self, seeded):
        """THE fix. This raised PendingRollbackError before H4-d."""
        with _APP.app_context():
            _force_failure()
            log_service.log_lead_event(phone="919990000001",
                                       event_type="TEST_EVENT")
            _restore()
            assert LeadEvent.query.count() == 0          # would raise if poisoned

    def test_log_message_leaves_the_session_usable(self, seeded):
        with _APP.app_context():
            _force_failure()
            log_service.log_message(phone="919990000001", direction="out",
                                    message_type="text", message_text="hi")
            _restore()
            assert MessageLog.query.count() == 0

    def test_save_conversation_message_still_leaves_it_usable(self, seeded):
        """Regression guard on the writer that ALREADY had the rollback and
        was deliberately not modified."""
        with _APP.app_context():
            _force_failure()
            log_service.save_conversation_message(
                phone="919990000001", direction="out", message="hi")
            _restore()
            assert ConversationMessage.query.count() == 0

    def test_the_failed_row_is_not_written(self, seeded):
        """Rolling back must not smuggle the row in."""
        with _APP.app_context():
            _force_failure()
            log_service.log_lead_event(phone="919990000001",
                                       event_type="TEST_EVENT")
            _restore()
            assert LeadEvent.query.filter_by(phone="919990000001").count() == 0

    def test_subsequent_writes_still_commit(self, seeded):
        """The session must be usable for WRITES, not just reads — the four
        at-risk routes commit after logging."""
        with _APP.app_context():
            _force_failure()
            log_service.log_lead_event(phone="919990000001",
                                       event_type="TEST_EVENT")
            _restore()
            db.session.add(ConversationState(
                phone="919990000002", tenant_id=OX, name="After",
                lead_status="Lead"))
            db.session.commit()
            assert ConversationState.query.filter_by(
                phone="919990000002").count() == 1

    def test_a_failure_does_not_discard_pending_work(self, seeded):
        """HONEST LIMIT, recorded rather than glossed.

        rollback() discards the WHOLE transaction, so uncommitted changes a
        route made before the log call are lost too. That is inherent to
        rolling back a shared session and is still far better than poisoning
        it — but it is a real property, so it is pinned rather than implied.
        """
        with _APP.app_context():
            _force_failure()
            db.session.add(ConversationState(
                phone="919990000003", tenant_id=OX, name="Pending",
                lead_status="Lead"))
            log_service.log_lead_event(phone="919990000001",
                                       event_type="TEST_EVENT")
            _restore()
            assert ConversationState.query.filter_by(
                phone="919990000003").count() == 0, \
                "pending row survived — update this test if that changes"


# ═══ the four at-risk routes complete their work ═════════════════════════════

class TestAtRiskRoutesComplete:
    """The concrete blast radius from discovery: routes doing DB work AFTER a
    log call. Exercised at the service level — the routes' own suites cover
    their HTTP behaviour."""

    def test_write_then_log_then_write(self, seeded):
        with _APP.app_context():
            lead = ConversationState.query.filter_by(
                phone="919990000001").first()
            lead.lead_status = "Contacted"
            db.session.commit()

            _force_failure()
            log_service.log_lead_event(phone="919990000001",
                                       event_type="LEAD_REASSIGNED")
            _restore()

            lead = ConversationState.query.filter_by(
                phone="919990000001").first()
            lead.assigned_staff = "Anju"
            db.session.commit()
            assert ConversationState.query.filter_by(
                phone="919990000001").first().assigned_staff == "Anju"

    def test_multiple_failed_logs_in_one_context(self, seeded):
        """A route may log several times; each failure must self-heal."""
        with _APP.app_context():
            _force_failure()
            for i in range(3):
                log_service.log_lead_event(phone="919990000001",
                                           event_type=f"EVT_{i}")
                log_service.log_message(phone="919990000001", direction="out",
                                        message_type="text", message_text="x")
            _restore()
            assert LeadEvent.query.count() == 0


# ═══ success paths unchanged ═════════════════════════════════════════════════

class TestSuccessPathUnchanged:

    def test_lead_event_still_writes(self, seeded):
        with _APP.app_context():
            log_service.log_lead_event(phone="919990000001",
                                       event_type="OK_EVENT", tenant_id=OX)
            rows = LeadEvent.query.filter_by(event_type="OK_EVENT").all()
            assert len(rows) == 1 and rows[0].tenant_id == OX

    def test_message_still_writes(self, seeded):
        with _APP.app_context():
            log_service.log_message(phone="919990000001", direction="out",
                                    message_type="text", message_text="hello",
                                    tenant_id=OX)
            rows = MessageLog.query.all()
            assert len(rows) == 1 and rows[0].message_text == "hello"

    def test_conversation_message_still_writes(self, seeded):
        with _APP.app_context():
            log_service.save_conversation_message(
                phone="919990000001", direction="out", message="hello",
                tenant_id=OX)
            assert ConversationMessage.query.count() == 1

    def test_no_rollback_on_the_success_path(self, seeded):
        """The rollback lives in `except`, so a successful write must not
        discard other pending work in the same session."""
        with _APP.app_context():
            db.session.add(ConversationState(
                phone="919990000004", tenant_id=OX, name="Pending",
                lead_status="Lead"))
            log_service.log_lead_event(phone="919990000001",
                                       event_type="OK_EVENT", tenant_id=OX)
            db.session.commit()
            assert ConversationState.query.filter_by(
                phone="919990000004").count() == 1


# ═══ structural ══════════════════════════════════════════════════════════════

class TestStructure:

    def _fn(self, name):
        with open(LOGSVC, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        m = ast.parse(ast.unparse(fn)).body[0]
        if (m.body and isinstance(m.body[0], ast.Expr)
                and isinstance(m.body[0].value, ast.Constant)):
            m.body.pop(0)
        return ast.unparse(m)

    @pytest.mark.parametrize("name", ["log_message", "log_lead_event",
                                      "save_conversation_message"])
    def test_all_three_writers_roll_back(self, name):
        assert "db.session.rollback()" in self._fn(name)

    @pytest.mark.parametrize("name", ["log_message", "log_lead_event",
                                      "save_conversation_message"])
    def test_rollback_is_guarded_by_its_own_try(self, name):
        """A failing rollback must not mask the original error, which is why
        the existing pattern nests it."""
        src = self._fn(name)
        i = src.index("db.session.rollback()")
        window = src[max(0, i - 200):i]
        assert "try:" in window, f"{name} rollback is not guarded"

    @pytest.mark.parametrize("name", ["log_message", "log_lead_event"])
    def test_rollback_runs_before_the_failure_log(self, name):
        """Ordering is STYLISTIC here, not load-bearing — stated plainly.

        logging.exception() does not touch the session, so recovering before
        or after it makes no behavioural difference; moving the rollback after
        the log is an EQUIVALENT mutant. This test pins the order only so the
        three writers keep one shape.

        The first version of this test was also simply wrong: it compared
        against rindex("logging.exception"), which after a reorder finds the
        ROLLBACK-FAILURE log nested inside the guard rather than the writer's
        own failure log — so it passed against the very mutation it claimed to
        catch. It now anchors on the writer's own message.
        """
        src = self._fn(name)
        rb = src.index("db.session.rollback()")
        own_log = src.index("Failed to log ")
        assert rb < own_log, f"{name} logs its failure before rolling back"

    def test_save_conversation_message_was_not_modified(self):
        """Explicitly out of scope for H4-d."""
        import subprocess
        head = subprocess.run(
            ["git", "show", "HEAD:app/services/log_service.py"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout
        old = next(n for n in ast.walk(ast.parse(head))
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "save_conversation_message")
        assert ast.unparse(old) == self._fn_with_doc("save_conversation_message")

    def _fn_with_doc(self, name):
        with open(LOGSVC, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        return ast.unparse(next(n for n in ast.walk(tree)
                                if isinstance(n, ast.FunctionDef) and n.name == name))

    def test_resolve_tenant_id_untouched(self):
        """H4-c's work must not be disturbed."""
        src = self._fn("resolve_tenant_id")
        assert "_get_default_tenant_id" not in src
        assert src.rstrip().endswith("return None")

    def test_only_log_service_changed(self):
        import subprocess
        out = subprocess.run(["git", "status", "--porcelain", "--", "app/"],
                             cwd=ROOT, capture_output=True, text=True).stdout
        dirty = sorted(l.split()[-1] for l in out.splitlines()
                       if l.strip() and not l.endswith("screens.py"))
        assert dirty in ([], ["app/services/log_service.py"]), dirty

    def test_no_schema_or_migration_change(self):
        import subprocess
        out = subprocess.run(["git", "status", "--porcelain", "--", "migrations/"],
                             cwd=ROOT, capture_output=True, text=True).stdout.strip()
        assert out == "", out

    def test_columns_are_still_not_null(self):
        """Unchanged: making these nullable is the migration H4-c declined and
        H4-d did not take either."""
        for model in (MessageLog, ConversationMessage, LeadEvent):
            assert model.__table__.c.tenant_id.nullable is False, model.__name__
