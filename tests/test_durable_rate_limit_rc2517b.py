"""Phase RC2.5.17 Gate B: the durable OTP rate limiter.

WHY THIS SUITE EXISTS
---------------------
The limiter this application already has is a process-local dict. It resets on
every deploy and is per-worker, so it guards nothing that must survive a
release. The new limiter's whole value is that it is durable and atomic, and
both properties are invisible in ordinary use -- a broken limiter and a
correct one behave identically until two requests arrive at once or a process
restarts. That is what this suite exists to make visible.

WHAT THIS PHASE IS NOT
----------------------
No OTP routes. No WhatsApp. No phone login or signup. No wiring of the limiter
or the OTP primitive into registration or login. No change to /register's
existing 5/IP/hour throttle. No migration of the six process-local call sites.
Several tests below pin those absences so a later change cannot arrive
unannounced under cover of "foundation".

WHAT SQLITE CANNOT PROVE
------------------------
SQLite serialises writers, so no test in the SQLite sections may be read as
evidence for the concurrency property. Section 9 runs the same assertions
against a real PostgreSQL database and is the authoritative one; it SKIPS when
no disposable database is supplied, and a skip is not a pass.
"""
import ast
import os
import re
import sys
import tempfile
import threading
from datetime import datetime, timedelta

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc2517b_ratelimit.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc2517b-admin-key")
os.environ.setdefault("SECRET_KEY", "rc2517b-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc2517b-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ["PRIMARY_TENANT_ID"] = "t-a"
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import app.services.followup_service as _fs                                   # noqa: E402
import app.marketing.campaign_worker as _cw                                   # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from app import create_app                                                    # noqa: E402
from app.extensions import db                                                 # noqa: E402
from app.models import RateLimitCounter, Tenant                               # noqa: E402
from app.services import rate_limit_service as rl                             # noqa: E402
from app.services import phone_service as ph                                  # noqa: E402

A = "t-a"
DEST = "919847312534"
DEST2 = "919999000111"
SIGNUP = "tenant_signup"
LOGIN = "phone_login"
CHANGE = "phone_change"

_APP = create_app()
_APP.config["TESTING"] = True

_OWN_MODULES = {k: v for k, v in sys.modules.items()
                if k == "app" or k.startswith("app.")}


@pytest.fixture(autouse=True)
def _pin_own_modules():
    sys.modules.update(_OWN_MODULES)
    yield


def _read(rel):
    with open(os.path.join(_ROOT, *rel.split("/")), encoding="utf-8") as fh:
        return fh.read()


def _code_only(rel):
    """Source with docstrings and comments stripped.

    Same trap RC2.5.16 hit four separate times: a substring scan for
    "log_audit" or "_RATE_LIMITS" matches the PROSE explaining why that thing
    is deliberately absent, so the assertion fails on its own explanation.
    """
    src = _read(rel)
    tree = ast.parse(src)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docs.add(doc)
    for doc in docs:
        src = src.replace(doc, "")
    return "\n".join(l.split("#", 1)[0] for l in src.splitlines())


@pytest.fixture()
def ctx():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=A, name=A, slug="rc2517b-a",
                              status="ACTIVE", billing_exempt=True))
        db.session.commit()
        yield
        db.session.remove()
        db.drop_all()


def _t(h=12, m=0, s=0):
    """A fixed clock. Every test passes `now` explicitly rather than sleeping.

    Wall-clock sleeps would make the cooldown tests take minutes and would
    make them flaky on a loaded CI runner. The limiter takes `now` as a
    parameter precisely so time can be a test input.
    """
    return datetime(2026, 9, 23, h, m, s)


def _rows(scope=None):
    q = RateLimitCounter.query
    if scope:
        q = q.filter_by(scope=scope)
    return q.all()


# ── 1. The bucket identity ──────────────────────────────────────────────────

class TestWindow:

    def test_window_truncates_to_the_hour(self):
        assert rl.window_start_for(_t(12, 59, 59)) == _t(12, 0, 0)
        assert rl.window_start_for(_t(12, 0, 0)) == _t(12, 0, 0)

    def test_window_is_naive_utc(self):
        assert rl.window_start_for(_t()).tzinfo is None

    def test_default_window_uses_utcnow_not_localtime(self):
        # The whole schema is naive UTC; a limiter reading local time would
        # bucket correctly only on a UTC host, which is exactly the kind of
        # coincidence RC2.5.16 flagged in models.py.
        src = _code_only("app/services/rate_limit_service.py")
        assert "datetime.utcnow()" in src
        assert "datetime.now()" not in src

    def test_a_fresh_bucket_is_created_per_hour(self, ctx):
        assert rl.check_create(DEST, SIGNUP, now=_t(12, 0))
        assert rl.check_create(DEST, SIGNUP, now=_t(13, 0))
        starts = {r.window_start for r in _rows(rl.SCOPE_CREATE)}
        assert starts == {_t(12, 0), _t(13, 0)}


# ── 2. Aggregate budgets ────────────────────────────────────────────────────

class TestAggregateBudgets:

    def test_create_allows_exactly_three_per_hour(self, ctx):
        # Spaced past the cooldown so the cooldown is not what denies the 4th.
        for i in range(3):
            d = rl.check_create(DEST, SIGNUP, now=_t(12, 2 * i))
            assert d, d.reason
        d = rl.check_create(DEST, SIGNUP, now=_t(12, 30))
        assert not d
        assert d.reason == rl.DENIED_AGGREGATE
        assert d.count == 4 and d.limit == 3

    def test_verify_allows_exactly_ten_per_hour(self, ctx):
        for i in range(10):
            assert rl.check_verify(DEST, LOGIN, now=_t(12, i))
        d = rl.check_verify(DEST, LOGIN, now=_t(12, 30))
        assert not d
        assert d.reason == rl.DENIED_AGGREGATE

    def test_budget_resets_at_the_next_window(self, ctx):
        for i in range(3):
            assert rl.check_create(DEST, SIGNUP, now=_t(12, 2 * i))
        assert not rl.check_create(DEST, SIGNUP, now=_t(12, 30))
        assert rl.check_create(DEST, SIGNUP, now=_t(13, 30))

    def test_budgets_are_per_subject(self, ctx):
        for i in range(3):
            assert rl.check_create(DEST, SIGNUP, now=_t(12, 2 * i))
        assert not rl.check_create(DEST, SIGNUP, now=_t(12, 30))
        assert rl.check_create(DEST2, SIGNUP, now=_t(12, 30))

    def test_create_and_verify_budgets_are_independent(self, ctx):
        for i in range(3):
            assert rl.check_create(DEST, SIGNUP, now=_t(12, 2 * i))
        assert not rl.check_create(DEST, SIGNUP, now=_t(12, 30))
        # Exhausting creation must not also lock out verification of a code
        # that was already sent -- that would strand a legitimate user.
        assert rl.check_verify(DEST, SIGNUP, now=_t(12, 30))

    def test_returned_count_is_post_increment(self, ctx):
        d = rl.check_create(DEST, SIGNUP, now=_t(12, 0))
        assert d.count == 1, "first call must report its own unit as counted"


# ── 3. Per-purpose budgets ──────────────────────────────────────────────────

class TestPerPurposeBudgets:

    def test_every_per_purpose_budget_is_within_the_aggregate(self):
        for p, v in rl.PER_PURPOSE_PER_HOUR.items():
            assert v <= rl.VERIFY_AGGREGATE_PER_HOUR, p

    def test_the_approved_values_are_what_is_implemented(self):
        assert rl.PER_PURPOSE_PER_HOUR == {
            'tenant_signup': 5, 'phone_login': 10,
            'phone_change': 5, 'sensitive_action': 5,
        }
        assert rl.CREATE_AGGREGATE_PER_HOUR == 3
        assert rl.VERIFY_AGGREGATE_PER_HOUR == 10
        assert rl.RESEND_COOLDOWN_SECONDS == 60
        assert rl.RETENTION_DAYS == 7

    def test_per_purpose_denies_before_the_aggregate(self, ctx):
        # phone_change is 5/h against a verify aggregate of 10/h.
        for i in range(5):
            assert rl.check_verify(DEST, CHANGE, now=_t(12, i))
        d = rl.check_verify(DEST, CHANGE, now=_t(12, 30))
        assert not d
        assert d.reason == rl.DENIED_PER_PURPOSE
        assert d.scope == f"{rl.SCOPE_VERIFY}:{CHANGE}"

    def test_aggregate_is_the_hard_ceiling_across_purposes(self, ctx):
        # 5 phone_change + 5 tenant_signup would be within each per-purpose
        # budget but exceeds the aggregate of 10 on the 11th.
        for i in range(5):
            assert rl.check_verify(DEST, CHANGE, now=_t(12, i))
        for i in range(5):
            assert rl.check_verify(DEST, SIGNUP, now=_t(12, 10 + i))
        d = rl.check_verify(DEST, LOGIN, now=_t(12, 40))
        assert not d
        assert d.reason == rl.DENIED_AGGREGATE

    def test_per_purpose_counters_are_separate_rows(self, ctx):
        rl.check_verify(DEST, CHANGE, now=_t(12, 0))
        scopes = {r.scope for r in _rows()}
        assert scopes == {rl.SCOPE_VERIFY, f"{rl.SCOPE_VERIFY}:{CHANGE}"}

    def test_an_unknown_purpose_is_denied_not_waved_through(self, ctx):
        d = rl.check_create(DEST, "not_a_purpose", now=_t(12, 0))
        assert not d
        assert d.reason == rl.DENIED_MALFORMED
        assert _rows() == [], "a malformed call must consume no budget"

    def test_an_empty_destination_is_denied(self, ctx):
        for bad in ("", None, "   "):
            d = rl.check_create(bad, SIGNUP, now=_t(12, 0))
            assert not d and d.reason == rl.DENIED_MALFORMED


# ── 4. A denied request consumes nothing ────────────────────────────────────

class TestDeniedConsumesNothing:

    def test_per_purpose_denial_rolls_back_the_aggregate(self, ctx):
        for i in range(5):
            assert rl.check_verify(DEST, CHANGE, now=_t(12, i))
        agg = _rows(rl.SCOPE_VERIFY)[0].count
        assert agg == 5
        d = rl.check_verify(DEST, CHANGE, now=_t(12, 30))
        assert d.reason == rl.DENIED_PER_PURPOSE
        # If the aggregate had been left incremented, a caller stuck on one
        # purpose would burn the ceiling for every OTHER purpose -- a denial
        # of service against the same user's other flows.
        assert _rows(rl.SCOPE_VERIFY)[0].count == 5
        assert rl.check_verify(DEST, LOGIN, now=_t(12, 31))

    def test_aggregate_denial_rolls_back_the_aggregate_itself(self, ctx):
        for i in range(10):
            assert rl.check_verify(DEST, LOGIN, now=_t(12, i))
        assert not rl.check_verify(DEST, LOGIN, now=_t(12, 30))
        assert _rows(rl.SCOPE_VERIFY)[0].count == 10, (
            "a refused unit must not be recorded, or repeated refusals would "
            "inflate the counter without bound")

    def test_aggregate_denial_does_not_touch_the_per_purpose_counter(self, ctx):
        for i in range(10):
            assert rl.check_verify(DEST, LOGIN, now=_t(12, i))
        assert not rl.check_verify(DEST, SIGNUP, now=_t(12, 30))
        assert _rows(f"{rl.SCOPE_VERIFY}:{SIGNUP}") == []


# ── 5. Resend cooldown ──────────────────────────────────────────────────────

class TestCooldown:

    def test_a_second_create_within_sixty_seconds_is_refused(self, ctx):
        assert rl.check_create(DEST, SIGNUP, now=_t(12, 0, 0))
        d = rl.check_create(DEST, SIGNUP, now=_t(12, 0, 30))
        assert not d and d.reason == rl.DENIED_COOLDOWN

    def test_a_create_after_sixty_seconds_is_allowed(self, ctx):
        assert rl.check_create(DEST, SIGNUP, now=_t(12, 0, 0))
        assert rl.check_create(DEST, SIGNUP, now=_t(12, 1, 0))

    def test_a_cooled_down_create_consumes_no_budget(self, ctx):
        assert rl.check_create(DEST, SIGNUP, now=_t(12, 0, 0))
        assert not rl.check_create(DEST, SIGNUP, now=_t(12, 0, 30))
        assert _rows(rl.SCOPE_CREATE)[0].count == 1

    def test_a_refused_create_does_not_restart_the_cooldown_clock(self, ctx):
        assert rl.check_create(DEST, SIGNUP, now=_t(12, 0, 0))
        assert not rl.check_create(DEST, SIGNUP, now=_t(12, 0, 30))
        # If the refused attempt had bumped updated_at, hammering Resend would
        # push the cooldown out forever and lock the user out of their own
        # signup.
        assert rl.check_create(DEST, SIGNUP, now=_t(12, 1, 0))

    def test_the_cooldown_is_not_bypassable_at_the_hour_boundary(self, ctx):
        # The atomic guard can only see the CURRENT bucket, so without the
        # previous-bucket check a fresh hour would hand out a free send on a
        # published schedule.
        assert rl.check_create(DEST, SIGNUP, now=_t(12, 59, 40))
        d = rl.check_create(DEST, SIGNUP, now=_t(13, 0, 10))
        assert not d and d.reason == rl.DENIED_COOLDOWN

    def test_the_boundary_check_does_not_over_block(self, ctx):
        assert rl.check_create(DEST, SIGNUP, now=_t(12, 58, 0))
        assert rl.check_create(DEST, SIGNUP, now=_t(13, 0, 10))

    def test_cooldown_is_per_subject(self, ctx):
        assert rl.check_create(DEST, SIGNUP, now=_t(12, 0, 0))
        assert rl.check_create(DEST2, SIGNUP, now=_t(12, 0, 5))

    def test_verification_has_no_cooldown(self, ctx):
        # A user correcting a typo must not be made to wait a minute.
        assert rl.check_verify(DEST, LOGIN, now=_t(12, 0, 0))
        assert rl.check_verify(DEST, LOGIN, now=_t(12, 0, 1))


# ── 6. Every attempt counts, including a successful one ─────────────────────

class TestEveryAttemptCounts:

    def test_check_verify_takes_no_outcome_parameter(self):
        import inspect
        params = list(inspect.signature(rl.check_verify).parameters)
        assert params == ["destination", "purpose", "now"], (
            "an outcome/success parameter would be a refund path: an attacker "
            "who interleaves a known-good code would keep guessing for free")

    def test_there_is_no_refund_or_decrement_anywhere(self):
        src = _code_only("app/services/rate_limit_service.py")
        assert "count - 1" not in src and "count-1" not in src
        assert "refund" not in src.lower()


# ── 7. Canonical subjects ───────────────────────────────────────────────────

class TestCanonicalSubject:

    def test_phone_spellings_collapse_to_one_bucket(self, ctx):
        # Without canonicalisation these are three identities for one phone
        # and the ceiling silently triples.
        spellings = ["+919847312534", "919847312534", "09847312534"]
        canon = {ph.normalize_destination(s) for s in spellings}
        assert canon == {DEST}
        for s in spellings:
            rl.check_verify(ph.normalize_destination(s), LOGIN, now=_t(12, 0))
        assert len(_rows(rl.SCOPE_VERIFY)) == 1
        assert _rows(rl.SCOPE_VERIFY)[0].count == 3

    def test_ipv6_spellings_collapse_to_one_bucket(self):
        from app.services.audit_service import _valid_ip
        a = _valid_ip("2001:db8::1")
        b = _valid_ip("2001:0db8:0000:0000:0000:0000:0000:0001")
        assert a == b == "2001:db8::1", (
            "an IPv6 client that re-spells its own address would otherwise "
            "mint a fresh budget per spelling")

    def test_valid_ip_still_rejects_junk(self):
        from app.services.audit_service import _valid_ip
        for bad in ("", None, "   ", "not-an-ip", "1.2.3.4.5", "x" * 60,
                    "<script>", "8.8.8.8; DROP TABLE"):
            assert _valid_ip(bad) == ""

    def test_valid_ip_canonicalises_without_widening_what_it_accepts(self):
        from app.services.audit_service import _valid_ip
        assert _valid_ip("8.8.8.8") == "8.8.8.8"
        assert _valid_ip(" 8.8.8.8 ") == "8.8.8.8"
        assert _valid_ip("::ffff:1.2.3.4") == "::ffff:102:304"

    def test_route_delegates_to_the_one_normaliser(self):
        src = _code_only("app/routes/public.py")
        assert "normalize_destination" in src, (
            "the route must not keep a second copy of the rule: a limiter "
            "that canonicalises differently from the writer meters a "
            "different subject than the challenge records")

    def test_promotion_preserved_behaviour_exactly(self):
        from app.routes.public import normalize_user_phone
        for raw, want in [("+919847312534", "919847312534"),
                          ("9847312534", "919847312534"),
                          ("09847312534", "919847312534"),
                          ("+1 555 012 3456", "15550123456"),
                          ("", ""), ("abc", ""), ("0", ""),
                          ("9" * 30, "")]:
            assert normalize_user_phone(raw) == want, raw


# ── 8. Fail closed ──────────────────────────────────────────────────────────

class TestFailClosed:

    def test_a_missing_table_denies(self, ctx):
        db.drop_all()
        d = rl.check_create(DEST, SIGNUP, now=_t(12, 0))
        assert not d
        assert d.reason == rl.DENIED_UNAVAILABLE
        db.create_all()

    def test_a_broken_engine_denies(self, ctx, monkeypatch):
        class _Boom:
            def begin(self):
                raise RuntimeError("connection refused")
        monkeypatch.setattr(type(db), "engine", property(lambda s: _Boom()))
        d = rl.check_verify(DEST, LOGIN, now=_t(12, 0))
        assert not d and d.reason == rl.DENIED_UNAVAILABLE

    def test_failure_never_raises_at_the_caller(self, ctx):
        db.drop_all()
        # The caller is an HTTP request handler. A limiter that raises turns a
        # counter outage into a 500 on a path that should simply say "later".
        assert rl.check_create(DEST, SIGNUP, now=_t(12, 0)).allowed is False
        db.create_all()

    def test_the_default_is_deny_not_allow(self):
        # Every `except` in a DECISION function must RETURN -- a handler that
        # logs and falls through would leave the caller with None, which is
        # falsy by luck rather than by decision.
        #
        # Scoped to the decision path on purpose. _retention_loop's handler
        # deliberately does NOT return: it is an infinite worker loop, and a
        # return there would kill retention on the first transient error. That
        # handler has its own test (test_the_loop_survives_a_failing_pass).
        tree = ast.parse(_read("app/services/rate_limit_service.py"))
        decision_fns = {"_check", "_bump", "purge_expired",
                        "check_create", "check_verify"}
        checked = 0
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name in decision_fns]:
            for node in ast.walk(fn):
                if isinstance(node, ast.ExceptHandler):
                    assert [x for x in node.body if isinstance(x, ast.Return)], (
                        f"{fn.name}: except handler falls through")
                    checked += 1
        assert checked >= 2, "expected to have checked real handlers"

    def test_no_except_handler_logs_a_traceback(self):
        # logger.exception renders the SQLAlchemy statement together with its
        # BOUND PARAMETERS, and `subject` is one of them -- so a traceback
        # publishes the phone number the format string masks. Under a counter
        # outage this path logs on every request, i.e. at maximum volume.
        src = _code_only("app/services/rate_limit_service.py")
        assert "logger.exception" not in src


# ── 9. PostgreSQL is authoritative for the concurrency property ─────────────

_PG_URL = os.environ.get("OTP_PG_TEST_URL")


@pytest.mark.skipif(not _PG_URL,
                    reason="set OTP_PG_TEST_URL to a DISPOSABLE PostgreSQL "
                           "database to run the authoritative concurrency "
                           "validation (SQLite serialises writers and cannot "
                           "reproduce the READ COMMITTED race)")
class TestPostgresConcurrency:
    """The only evidence that the limiter is atomic.

    RC2.5.16 Gate B.1 proved on this database that a read-then-write under
    READ COMMITTED loses updates and produces duplicate rows: a concurrent
    INSERT is invisible until it commits, so each transaction sees "no bucket"
    and inserts its own. The SQLite sections above cannot see that, because
    SQLite serialises writers. This class is therefore not a nice-to-have
    duplicate of them -- it is the whole proof.

    OTP_PG_TEST_URL must be DISPOSABLE. This class creates and drops tables.
    Never point it at production.
    """

    @staticmethod
    def _pg_app():
        from flask import Flask
        from app.extensions import db as _db
        app = Flask(__name__)
        app.config["SQLALCHEMY_DATABASE_URI"] = _PG_URL
        app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_size": 12,
                                                   "max_overflow": 12,
                                                   "pool_timeout": 60}
        _db.init_app(app)
        return app

    @pytest.fixture()
    def pg(self):
        app = self._pg_app()
        with app.app_context():
            RateLimitCounter.__table__.drop(db.engine, checkfirst=True)
            RateLimitCounter.__table__.create(db.engine)
            try:
                yield app
            finally:
                db.session.remove()
                RateLimitCounter.__table__.drop(db.engine, checkfirst=True)
                # MUST dispose. Each test builds its own Flask app and so its
                # own pool; without this the pools accumulate across the class
                # and the cluster refuses with "too many clients already" --
                # which surfaced as a failure in the LAST test rather than the
                # one that leaked, i.e. a harness artefact masquerading as a
                # limiter defect.
                db.engine.dispose()

    def _race(self, app, n, fn):
        barrier = threading.Barrier(n)
        out, errs = [], []

        def w(_):
            try:
                with app.app_context():
                    barrier.wait(timeout=60)
                    out.append(fn())
            except Exception as exc:                            # noqa: BLE001
                errs.append(exc)

        threads = [threading.Thread(target=w, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=90)
        assert not errs, f"limiter raised under concurrency: {errs[:3]}"
        return out

    def test_concurrent_verifies_grant_exactly_the_budget(self, pg):
        n = 30
        out = self._race(pg, n, lambda: rl.check_verify(DEST, LOGIN,
                                                        now=_t(12, 0)))
        allowed = [d for d in out if d.allowed]
        assert len(out) == n
        assert len(allowed) == rl.VERIFY_AGGREGATE_PER_HOUR, (
            f"{len(allowed)} of {n} concurrent attempts were allowed; the "
            f"budget is {rl.VERIFY_AGGREGATE_PER_HOUR}. More means lost "
            f"updates or duplicate buckets.")

    def test_no_duplicate_buckets_are_created(self, pg):
        self._race(pg, 30, lambda: rl.check_verify(DEST, LOGIN, now=_t(12, 0)))
        with pg.app_context():
            rows = RateLimitCounter.query.filter_by(
                scope=rl.SCOPE_VERIFY, subject=DEST).all()
            assert len(rows) == 1, (
                f"{len(rows)} buckets for one (scope, subject, window). This "
                f"is the exact phantom-insert failure Gate B.1 found; the "
                f"unique constraint plus ON CONFLICT is what prevents it.")
            assert rows[0].count == rl.VERIFY_AGGREGATE_PER_HOUR

    def test_the_post_increment_counts_are_a_dense_sequence(self, pg):
        out = self._race(pg, 10, lambda: rl.check_verify(DEST, LOGIN,
                                                         now=_t(12, 0)))
        counts = sorted(d.count for d in out)
        # 1..10 with no repeats: a repeat is a lost update, a gap is a
        # duplicate bucket. Either would mean the counter is advisory.
        assert counts == list(range(1, 11)), counts

    def test_concurrent_creates_respect_the_cooldown(self, pg):
        out = self._race(pg, 20, lambda: rl.check_create(DEST, SIGNUP,
                                                         now=_t(12, 0, 0)))
        allowed = [d for d in out if d.allowed]
        assert len(allowed) == 1, (
            f"{len(allowed)} concurrent creates were allowed within one "
            f"cooldown window; the guard must be evaluated by the database, "
            f"not read into Python and checked")

    def test_a_denied_concurrent_request_consumes_nothing(self, pg):
        # phone_change is 5/h; fire 20 at once and the aggregate must show
        # exactly 5, not 20.
        self._race(pg, 20, lambda: rl.check_verify(DEST, CHANGE, now=_t(12, 0)))
        with pg.app_context():
            agg = RateLimitCounter.query.filter_by(
                scope=rl.SCOPE_VERIFY, subject=DEST).one()
            per = RateLimitCounter.query.filter_by(
                scope=f"{rl.SCOPE_VERIFY}:{CHANGE}", subject=DEST).one()
            assert per.count == rl.PER_PURPOSE_PER_HOUR[CHANGE]
            assert agg.count == rl.PER_PURPOSE_PER_HOUR[CHANGE], (
                "rolled-back aggregate units leaked; a caller stuck on one "
                "purpose would burn the ceiling for every other purpose")

    def test_the_unique_constraint_actually_exists_on_postgres(self, pg):
        from sqlalchemy import inspect as sa_inspect
        with pg.app_context():
            names = {c["name"] for c in sa_inspect(db.engine)
                     .get_unique_constraints("rate_limit_counters")}
            assert "uq_rate_limit_counters_bucket" in names, (
                "ON CONFLICT names this constraint; without it the atomic "
                "statement is not merely slower, it does not run")


# ── 10. Retention ───────────────────────────────────────────────────────────

class TestRetention:

    def test_purge_deletes_rows_older_than_the_window(self, ctx):
        rl.check_verify(DEST, LOGIN, now=_t(12, 0) - timedelta(days=30))
        rl.check_verify(DEST2, LOGIN, now=_t(12, 0))
        assert rl.purge_expired(now=_t(12, 0)) == 2   # aggregate + per-purpose
        left = {r.subject for r in _rows()}
        assert left == {DEST2}

    def test_purge_keeps_rows_inside_the_window(self, ctx):
        rl.check_verify(DEST, LOGIN, now=_t(12, 0) - timedelta(days=3))
        assert rl.purge_expired(now=_t(12, 0)) == 0

    def test_purge_never_raises(self, ctx):
        db.drop_all()
        assert rl.purge_expired(now=_t(12, 0)) == 0
        db.create_all()

    def test_purge_is_not_the_boundary(self, ctx):
        # A bucket outside the current window is never read, so a purge that
        # never runs leaks storage and PII but can never grant budget.
        for i in range(10):
            assert rl.check_verify(DEST, LOGIN, now=_t(12, i))
        assert not rl.check_verify(DEST, LOGIN, now=_t(12, 30))
        assert rl.purge_expired(now=_t(12, 0)) == 0
        assert not rl.check_verify(DEST, LOGIN, now=_t(12, 31))


# ── 11. Privacy ─────────────────────────────────────────────────────────────

class TestPrivacy:

    def test_repr_never_renders_the_subject(self, ctx):
        rl.check_verify(DEST, LOGIN, now=_t(12, 0))
        row = _rows(rl.SCOPE_VERIFY)[0]
        assert DEST not in repr(row)
        assert DEST not in repr(rl.check_verify(DEST, LOGIN, now=_t(12, 1)))

    def test_the_failure_log_masks_the_destination(self, ctx, caplog):
        db.drop_all()
        with caplog.at_level("ERROR"):
            rl.check_create(DEST, SIGNUP, now=_t(12, 0))
        db.create_all()
        text = caplog.text
        assert DEST not in text
        assert ph.mask_destination(DEST) in text
        assert not re.search(r"\d{6,}", text), "a long digit run reached a log"

    def test_mask_keeps_only_three_digits(self):
        assert ph.mask_destination(DEST) == "*" * (len(DEST) - 3) + DEST[-3:]
        assert ph.mask_destination("") == ""
        assert ph.mask_destination(None) == ""

    def test_subject_is_stored_not_hashed(self, ctx):
        # Deliberate: hashing defeats nothing (phone numbers are trivially
        # enumerable) while making an operator unable to answer "is this
        # number being throttled?". Retention, not hashing, is the control.
        rl.check_verify(DEST, LOGIN, now=_t(12, 0))
        assert _rows(rl.SCOPE_VERIFY)[0].subject == DEST


# ── 12. Separation from the OTP primitive ───────────────────────────────────

class TestSeparation:

    def test_otp_service_contains_no_durable_rate_limiting(self):
        src = _code_only("app/services/otp_service.py")
        assert "rate_limit" not in src
        assert "RateLimitCounter" not in src

    def test_the_limiter_does_not_import_the_otp_primitive(self):
        src = _code_only("app/services/rate_limit_service.py")
        assert "otp_service" not in src
        assert "OtpChallenge" not in src

    def test_the_limiter_does_not_reuse_the_challenge_table(self):
        src = _code_only("app/services/rate_limit_service.py")
        assert "otp_challenges" not in src

    def test_the_limiter_uses_its_own_transaction_not_db_session(self):
        src = _code_only("app/services/rate_limit_service.py")
        assert "db.engine.begin()" in src
        assert "db.session" not in src, (
            "a limiter decision on db.session could commit a caller's "
            "half-finished business transaction")

    def test_the_limiter_writes_no_audit_rows(self):
        src = _code_only("app/services/rate_limit_service.py")
        assert "log_audit" not in src

    def test_the_limiter_uses_no_read_then_write(self):
        src = _code_only("app/services/rate_limit_service.py")
        assert "ON CONFLICT" in src
        assert "RETURNING count" in src


# ── 13. Regression: nothing else moved ──────────────────────────────────────

class TestNothingElseMoved:

    def test_register_throttle_is_still_five_per_hour(self):
        from app.routes import public
        assert public._REGISTER_MAX_PER_IP == 5
        assert public._REGISTER_WINDOW_SECONDS == 3600

    def test_the_process_local_limiter_is_untouched(self):
        from app.routes import public
        assert hasattr(public, "check_rate_limit")
        assert isinstance(public._RATE_LIMITS, dict)

    def test_the_six_existing_call_sites_were_not_migrated(self):
        src = _code_only("app/routes/public.py")
        assert src.count("check_rate_limit(") >= 6
        assert "rate_limit_service" not in src, (
            "migrating the process-local call sites is explicitly out of "
            "scope for this phase")

    @staticmethod
    def _importers(module_name):
        """Files under app/ whose CODE (not prose) names `module_name`.

        _code_only is mandatory here. models.py, config.py, phone_service.py
        and rate_limit_service.py each EXPLAIN in prose why they do not
        depend on these modules, so a raw substring scan reports every
        explanation as a violation -- the same trap RC2.5.16 hit four times.
        """
        hits = []
        for root, _dirs, files in os.walk(os.path.join(_ROOT, "app")):
            for f in files:
                if not f.endswith(".py") or f == f"{module_name}.py":
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, _ROOT).replace("\\", "/")
                try:
                    if module_name in _code_only(rel):
                        hits.append(rel)
                except SyntaxError:                       # pragma: no cover
                    pytest.fail(f"{rel} does not parse")
        return hits

    def test_the_limiter_has_exactly_one_caller_and_it_is_retention(self):
        """NARROWED BY the Gate B review, which required retention to be
        actually enforced rather than merely implemented.

        Before the review this asserted ZERO callers. create_app now starts
        the retention worker, which is a caller -- so the assertion moves from
        "nothing imports it" to "exactly one thing imports it, for retention
        only". test_no_decision_function_has_a_caller below keeps the half
        that matters: no route, service or worker makes a limiter DECISION.
        """
        hits = self._importers("rate_limit_service")
        assert hits == ["app/__init__.py"], hits

    def test_no_decision_function_has_a_caller(self):
        """The limiter still decides nothing in production.

        This is the real dormancy pin. check_create/check_verify are what
        would change behaviour if wired up; init_rate_limit_retention only
        deletes expired rows from an empty table.
        """
        for root, _dirs, files in os.walk(os.path.join(_ROOT, "app")):
            for f in files:
                if not f.endswith(".py") or f == "rate_limit_service.py":
                    continue
                rel = os.path.relpath(os.path.join(root, f),
                                      _ROOT).replace("\\", "/")
                src = _code_only(rel)
                for fn in ("check_create(", "check_verify(", "Decision("):
                    assert fn not in src, f"{rel} calls {fn}"

    def test_the_only_import_is_the_retention_init(self):
        src = _code_only("app/__init__.py")
        assert "init_rate_limit_retention" in src
        for fn in ("check_create", "check_verify", "purge_expired"):
            assert fn not in src, fn

    def test_the_otp_primitive_is_still_dormant(self):
        hits = self._importers("otp_service")
        assert hits == [], f"OTP primitive has callers: {hits}"

    def test_no_otp_route_exists(self):
        rules = [str(r) for r in _APP.url_map.iter_rules()]
        assert not [r for r in rules if re.search(r"\botp\b", r, re.I)]

    def test_otp_hmac_key_is_not_required_by_this_phase(self):
        # The limiter must work with the key absent, because production does
        # not have it and this phase does not provision it.
        assert "OTP_HMAC_KEY" not in _code_only(
            "app/services/rate_limit_service.py")


# ── 14. Migration ───────────────────────────────────────────────────────────

class TestMigration:

    MIG = ("migrations/versions/"
           "a4f2c70b19de_rc2_5_17b_rate_limit_counters.py")

    def test_it_descends_from_the_current_head(self):
        src = _read(self.MIG)
        assert re.search(r"^revision = 'a4f2c70b19de'", src, re.M)
        assert re.search(r"^down_revision = 'd1b6c48e7f92'", src, re.M)

    def test_there_is_exactly_one_head(self):
        revs = {}
        d = os.path.join(_ROOT, "migrations", "versions")
        for f in os.listdir(d):
            if not f.endswith(".py"):
                continue
            src = _read(f"migrations/versions/{f}")
            r = re.search(r"^revision = '([^']+)'", src, re.M)
            if not r:
                continue
            dn = re.search(r"^down_revision = '([^']+)'", src, re.M)
            revs[r.group(1)] = dn.group(1) if dn else None
        children = {v for v in revs.values() if v}
        heads = [k for k in revs if k not in children]
        assert heads == ["a4f2c70b19de"], heads

    def test_it_is_additive_only(self):
        src = _code_only(self.MIG)
        up = src.split("def upgrade")[1].split("def downgrade")[0]
        for forbidden in ("drop_table", "drop_column", "alter_column",
                          "execute(", "UPDATE ", "DELETE "):
            assert forbidden not in up, forbidden

    def test_the_downgrade_is_reversible(self):
        src = _code_only(self.MIG)
        down = src.split("def downgrade")[1]
        assert "drop_table('rate_limit_counters')" in down
        assert "drop_index" in down

    def test_the_unique_constraint_is_in_the_migration(self):
        src = _read(self.MIG)
        assert "uq_rate_limit_counters_bucket" in src
        assert "'scope', 'subject', 'window_start'" in src

    def test_the_model_and_the_migration_agree(self, ctx):
        from sqlalchemy import inspect as sa_inspect
        cols = {c["name"] for c in
                sa_inspect(db.engine).get_columns("rate_limit_counters")}
        assert cols == {"id", "scope", "subject", "window_start", "count",
                        "created_at", "updated_at"}


# ── 15. Subject width (Gate B review correction 1) ──────────────────────────

class TestSubjectWidth:
    """The column must fit every subject any supported purpose can produce.

    45 would not truncate anything THIS implementation builds -- purpose is
    carried by `scope`, so `subject` is a bare destination or a bare IP. The
    width is 64 because 45 is not future-safe: the composite shape a later
    phase would most naturally reach for overflows it, and the two ways that
    can fail are a hard error under load or, on a truncating backend, two
    subjects silently collapsing into one bucket -- a bypass with no trace.
    """

    #: The widest composite a supported purpose can produce:
    #: destination (20) + separator (1) + purpose (32).
    MAX_COMPOSITE = 20 + 1 + 32

    def test_the_column_is_sixty_four(self):
        assert RateLimitCounter.__table__.c.subject.type.length == 64

    def test_the_migration_declares_the_same_width(self):
        src = _read("migrations/versions/"
                    "a4f2c70b19de_rc2_5_17b_rate_limit_counters.py")
        assert "sa.String(length=64)" in src
        assert "sa.String(length=45)" not in src

    def test_the_longest_composite_subject_fits(self):
        from app.models import OtpChallenge
        from app.services.phone_service import _PHONE_MAX_LEN
        assert _PHONE_MAX_LEN == 20
        assert OtpChallenge.__table__.c.purpose.type.length == 32
        assert self.MAX_COMPOSITE == 53
        assert self.MAX_COMPOSITE <= 64, (
            "a composite subject would overflow the column")

    def test_forty_five_would_have_been_too_narrow(self):
        # States the defect the review caught, so nobody narrows it back.
        assert self.MAX_COMPOSITE > 45

    def test_the_longest_composite_round_trips_without_truncation(self, ctx):
        longest = ("9" * 20) + "|" + ("p" * 32)
        assert len(longest) == self.MAX_COMPOSITE
        row = RateLimitCounter(scope=rl.SCOPE_VERIFY, subject=longest,
                               window_start=_t(12, 0), count=1,
                               created_at=_t(12, 0), updated_at=_t(12, 0))
        db.session.add(row)
        db.session.commit()
        db.session.expire_all()
        back = RateLimitCounter.query.one()
        assert back.subject == longest, "subject was truncated"
        assert len(back.subject) == self.MAX_COMPOSITE

    def test_a_full_width_subject_round_trips(self, ctx):
        full = "z" * 64
        db.session.add(RateLimitCounter(
            scope=rl.SCOPE_VERIFY, subject=full, window_start=_t(12, 0),
            count=1, created_at=_t(12, 0), updated_at=_t(12, 0)))
        db.session.commit()
        db.session.expire_all()
        assert RateLimitCounter.query.one().subject == full

    def test_every_real_subject_kind_fits(self):
        from app.services.audit_service import _valid_ip
        widest_ip = _valid_ip("2001:0db8:85a3:0000:0000:8a2e:0370:7334")
        assert 0 < len(widest_ip) <= 64
        assert len(ph.normalize_destination("+" + "9" * 20)) <= 64

    def test_the_unique_constraint_still_covers_the_bucket(self):
        uqs = [c for c in RateLimitCounter.__table__.constraints
               if getattr(c, "name", None) == "uq_rate_limit_counters_bucket"]
        assert len(uqs) == 1
        assert [c.name for c in uqs[0].columns] == ["scope", "subject",
                                                    "window_start"]


# ── 16. Retention is actually enforced (Gate B review correction 2) ─────────

class TestRetentionIsEnforced:
    """The review's finding: a purge function nobody calls is not a policy.

    These tests pin the MECHANISM, not just the function -- that it is wired
    into create_app, that it cannot touch a live bucket, that it is idempotent
    and multi-worker safe, and that its failure cannot weaken the limiter.
    """

    def test_create_app_starts_the_retention_worker(self):
        src = _code_only("app/__init__.py")
        assert "init_rate_limit_retention(app)" in src

    def test_it_is_not_behind_a_feature_flag(self):
        # subject is PII; a retention commitment behind a flag is not one.
        src = _code_only("app/__init__.py")
        before = src.split("init_rate_limit_retention")[0]
        tail = before.rsplit("init_campaign_worker(app)", 1)[-1]
        assert "if " not in tail, "retention must be unconditional"

    def test_it_reuses_the_existing_worker_pattern(self):
        src = _code_only("app/services/rate_limit_service.py")
        assert "threading.Thread(" in src and "daemon=True" in src
        assert "time.sleep(" in src
        # No new infrastructure. Each of these would be a new dependency.
        for banned in ("redis", "Redis", "celery", "Celery", "APScheduler",
                       "BackgroundScheduler", "crontab"):
            assert banned not in src, banned

    def test_the_loop_sleeps_before_its_first_pass(self):
        src = _read("app/services/rate_limit_service.py")
        body = src.split("def _retention_loop")[1].split("\ndef ")[0]
        after_while = body.split("while True:")[1]
        assert after_while.index("time.sleep(") < after_while.index(
            "purge_expired()"), (
            "purge-then-sleep would fire a DELETE during every test suite's "
            "create_app(), before any fixture has run")

    def test_the_interval_is_far_shorter_than_the_window(self):
        assert rl.RETENTION_INTERVAL_SECONDS <= 3600
        assert rl.RETENTION_DAYS * 86400 // rl.RETENTION_INTERVAL_SECONDS >= 24

    # ── the boundary ──

    def test_rows_older_than_seven_days_are_deleted(self, ctx):
        rl.check_verify(DEST, LOGIN, now=_t(12, 0) - timedelta(days=8))
        assert len(_rows()) == 2
        assert rl.purge_expired(now=_t(12, 0)) == 2
        assert _rows() == []

    def test_rows_just_inside_the_boundary_survive(self, ctx):
        # 7 days minus one hour: still inside retention.
        rl.check_verify(DEST, LOGIN,
                        now=_t(12, 0) - timedelta(days=7) + timedelta(hours=1))
        assert rl.purge_expired(now=_t(12, 0)) == 0
        assert len(_rows()) == 2

    def test_rows_just_outside_the_boundary_are_deleted(self, ctx):
        rl.check_verify(DEST, LOGIN,
                        now=_t(12, 0) - timedelta(days=7) - timedelta(hours=1))
        assert rl.purge_expired(now=_t(12, 0)) == 2

    def test_the_current_window_is_never_touched(self, ctx):
        rl.check_verify(DEST, LOGIN, now=_t(12, 0))
        # Even with a ZERO-day retention the cutoff is the CURRENT bucket's
        # start and the comparison is strictly `<`, so the live bucket is out
        # of range by construction, not by arithmetic luck.
        assert rl.purge_expired(now=_t(12, 30), retention_days=0) == 0
        assert len(_rows()) == 2

    def test_recent_windows_survive(self, ctx):
        for d in range(0, 7):
            rl.check_verify(DEST, LOGIN, now=_t(12, 0) - timedelta(days=d))
        before = len(_rows())
        assert before > 0
        assert rl.purge_expired(now=_t(12, 0)) == 0
        assert len(_rows()) == before

    def test_purge_is_idempotent(self, ctx):
        rl.check_verify(DEST, LOGIN, now=_t(12, 0) - timedelta(days=9))
        assert rl.purge_expired(now=_t(12, 0)) == 2
        assert rl.purge_expired(now=_t(12, 0)) == 0
        assert rl.purge_expired(now=_t(12, 0)) == 0

    def test_purge_deletes_by_the_indexed_column(self):
        src = _code_only("app/services/rate_limit_service.py")
        assert "WHERE window_start < :cutoff" in src
        idx = {i.name for i in RateLimitCounter.__table__.indexes}
        assert "ix_rate_limit_counters_window_start" in idx

    # ── failure containment ──

    def test_a_failing_purge_does_not_weaken_the_limiter(self, ctx,
                                                         monkeypatch):
        for i in range(10):
            assert rl.check_verify(DEST, LOGIN, now=_t(12, i))

        def _boom(*a, **k):
            raise RuntimeError("retention down")

        monkeypatch.setattr(rl, "purge_expired", _boom)
        # Retention is broken; the ceiling must still hold exactly.
        d = rl.check_verify(DEST, LOGIN, now=_t(12, 30))
        assert not d and d.reason == rl.DENIED_AGGREGATE
        assert rl.check_verify(DEST2, LOGIN, now=_t(12, 30))

    def test_the_loop_survives_a_failing_pass(self, ctx, monkeypatch, caplog):
        calls = {"n": 0}

        def _boom(*a, **k):
            calls["n"] += 1
            raise RuntimeError("transient")

        monkeypatch.setattr(rl, "purge_expired", _boom)
        monkeypatch.setattr(rl, "_app", _APP)
        # Drive ONE iteration's body, not the infinite loop.
        with caplog.at_level("ERROR"):
            try:
                with rl._app.app_context():
                    rl.purge_expired()
            except Exception as exc:                            # noqa: BLE001
                rl.logger.error("[ratelimit] retention loop error (%s)",
                                type(exc).__name__)
        assert calls["n"] == 1
        assert "retention loop error" in caplog.text
        # And the source really does contain that containment.
        body = _read("app/services/rate_limit_service.py")
        body = body.split("def _retention_loop")[1].split("\ndef ")[0]
        assert "except Exception" in body

    def test_init_never_raises(self, ctx):
        # A failure to start retention must not prevent the app from booting.
        assert rl.init_rate_limit_retention(_APP) is None

    def test_retention_logs_no_pii(self, ctx, caplog):
        rl.check_verify(DEST, LOGIN, now=_t(12, 0) - timedelta(days=9))
        with caplog.at_level("INFO"):
            deleted = rl.purge_expired(now=_t(12, 0))
            if deleted:
                rl.logger.info("[ratelimit] retention purged %d expired "
                               "counter rows", deleted)
        assert DEST not in caplog.text
        assert not re.search(r"\d{6,}", caplog.text)

    def test_no_web_concurrency_gate_was_copied(self):
        # The campaign worker's gate exists because it CLAIMS and SENDS rows.
        # A set-based DELETE over an already-expired range has no such hazard,
        # and a gate here would silently disable retention on a multi-worker
        # deployment -- which is the failure this review is closing.
        src = _code_only("app/services/rate_limit_service.py")
        assert "WEB_CONCURRENCY" not in src
