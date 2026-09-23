"""Phase RC2.5.16: the offline OTP challenge primitive.

WHY
---
An OTP cannot be a stateless signed token. email_service already issues
itsdangerous tokens, and its own docstring calls them "signed, stateless" --
exactly what a one-time code must not be: with no server-side row there is
nothing to consume, so a code stays replayable until it expires, attempts
cannot be counted, and a resend cannot revoke its predecessor. Those three
properties ARE the primitive, and this suite is where they are proven.

WHAT THIS PHASE IS NOT
----------------------
No routes, no WhatsApp, no Meta, no phone login, no passwordless auth, no
registration or login change, no durable rate limiting, no audit integration,
no cleanup worker. There are NO callers of this service. Several tests below
pin those absences, so a later change cannot arrive unannounced under cover of
"foundation".

THE TWO PROPERTIES WORTH READING THE CODE FOR
---------------------------------------------
  1. Verification is TWO atomic statements, not a SELECT-inspect-save. Under
     PostgreSQL's default READ COMMITTED isolation, two concurrent verifiers
     doing read-modify-write both read attempt_count = 2 and both write 3,
     losing an attempt. The claim step makes the state check and the increment
     one statement; the consume step re-asserts the whole predicate so exactly
     one verifier can win.
  2. The secret comparison is hmac.compare_digest, never SQL equality -- which
     is why the hash is returned by the claim and compared in Python rather
     than pushed into the UPDATE's WHERE clause.
"""
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

_DB = os.path.join(tempfile.gettempdir(), "rc2516_otp.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc2516-admin-key")
os.environ.setdefault("SECRET_KEY", "rc2516-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc2516-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ["PRIMARY_TENANT_ID"] = "t-a"
# A TEST key. Not a production secret, and no production secret is ever read
# by this suite.
os.environ["OTP_HMAC_KEY"] = "rc2516-test-hmac-key-not-a-real-secret"
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import app.services.followup_service as _fs                                   # noqa: E402
import app.marketing.campaign_worker as _cw                                   # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from app import create_app                                                    # noqa: E402
from app.extensions import db                                                 # noqa: E402
from app.models import OtpChallenge, Tenant, User                             # noqa: E402
from app.services import otp_service as otp                                   # noqa: E402

A = "t-a"
B = "t-b"
DEST = "919847312534"
DEST2 = "919999000111"
SIGNUP = "tenant_signup"
LOGIN = "phone_login"

_APP = create_app()
_APP.config["TESTING"] = True

_OWN_MODULES = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}


@pytest.fixture(autouse=True)
def _pin_own_modules():
    sys.modules.update(_OWN_MODULES)
    yield


def _read(rel):
    with open(os.path.join(_ROOT, *rel.split("/")), encoding="utf-8") as fh:
        return fh.read()


def _code_only(rel):
    """Source with docstrings and comments stripped.

    A plain substring scan for "random.choice" or "log_audit" matches the
    PROSE that explains why those are not used -- the module documents its own
    reasoning, so the naive assertion fails on its own explanation. Three
    tests in this suite hit that on first run. Strip the narration and assert
    against executable code only.
    """
    import ast
    src = _read(rel)
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    for doc in docstrings:
        src = src.replace(doc, "")
    return "\n".join(l.split("#", 1)[0] for l in src.splitlines())


class _FakeSecrets:
    """Stands in for the `secrets` MODULE, so a test never patches the real one.

    RC2.5.10 recorded this exact trap: monkeypatching an attribute on a shared
    stdlib module rewires it for every other consumer, and "restoring" it from
    a fresh `import secrets` restores the already-patched object because it is
    the same module. Here that silently pinned generate_code() to one value
    for the rest of the session, which a later randomness test caught only by
    luck. Replacing the whole reference leaves the stdlib untouched.
    """

    def __init__(self, value):
        self._value = value

    def randbelow(self, n):
        return self._value


@pytest.fixture()
def ctx():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid in (A, B):
            db.session.add(Tenant(id=tid, name=tid, slug=f"rc2516-{tid}",
                                  status="ACTIVE", billing_exempt=True))
        db.session.commit()
        yield
        db.session.remove()
        db.drop_all()


def _row(cid):
    return db.session.get(OtpChallenge, cid)


def _wrong(code):
    """A six-digit code guaranteed different from `code`."""
    return "000000" if code != "000000" else "111111"


# ── 1. core verification ────────────────────────────────────────────────────

class TestCore:

    def test_valid_code_succeeds(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        assert otp.verify_challenge(cid, code) == (True, otp.OK)

    def test_valid_code_is_consumed(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        otp.verify_challenge(cid, code)
        assert _row(cid).consumed_at is not None

    def test_replay_fails(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        assert otp.verify_challenge(cid, code)[0] is True
        ok, reason = otp.verify_challenge(cid, code)
        assert ok is False and reason == otp.ALREADY_CONSUMED

    def test_wrong_code_fails(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        assert otp.verify_challenge(cid, _wrong(code)) == (False, otp.INVALID_CODE)

    def test_wrong_purpose_fails(self, ctx):
        """A signup code must be structurally unusable as a login code."""
        cid, code = otp.create_challenge(SIGNUP, DEST)
        assert otp.verify_challenge(cid, code, purpose=LOGIN)[0] is False

    def test_wrong_destination_fails(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        assert otp.verify_challenge(cid, code, destination=DEST2)[0] is False

    def test_matching_context_still_succeeds(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        assert otp.verify_challenge(cid, code, purpose=SIGNUP,
                                    destination=DEST) == (True, otp.OK)

    def test_no_such_challenge(self, ctx):
        assert otp.verify_challenge(999999, "123456") == (False, otp.NO_SUCH_CHALLENGE)

    @pytest.mark.parametrize("bad", [None, "", "   "])
    def test_malformed_input(self, ctx, bad):
        cid, _ = otp.create_challenge(SIGNUP, DEST)
        assert otp.verify_challenge(cid, bad) == (False, otp.MALFORMED_INPUT)

    def test_malformed_input_costs_no_attempt(self, ctx):
        cid, _ = otp.create_challenge(SIGNUP, DEST)
        otp.verify_challenge(cid, "")
        assert _row(cid).attempt_count == 0


# ── 2. expiry ───────────────────────────────────────────────────────────────

class TestExpiry:

    def test_default_lifetime_is_five_minutes(self, ctx):
        cid, _ = otp.create_challenge(SIGNUP, DEST)
        r = _row(cid)
        assert (r.expires_at - r.created_at) == timedelta(seconds=300)

    def test_expired_code_fails(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        r = _row(cid)
        r.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        assert otp.verify_challenge(cid, code) == (False, otp.EXPIRED)

    def test_exact_expiry_boundary_fails(self, ctx):
        """utcnow() >= expires_at is EXPIRED. Pinned exactly, because an
        off-by-one here is a silently longer-lived credential."""
        cid, code = otp.create_challenge(SIGNUP, DEST)
        frozen = datetime.utcnow()
        r = _row(cid)
        r.expires_at = frozen
        db.session.commit()
        otp._utcnow = lambda: frozen                    # exactly at the boundary
        try:
            assert otp.verify_challenge(cid, code) == (False, otp.EXPIRED)
        finally:
            otp._utcnow = datetime.utcnow

    def test_one_second_before_expiry_succeeds(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        frozen = datetime.utcnow()
        r = _row(cid)
        r.expires_at = frozen + timedelta(seconds=1)
        db.session.commit()
        otp._utcnow = lambda: frozen
        try:
            assert otp.verify_challenge(cid, code) == (True, otp.OK)
        finally:
            otp._utcnow = datetime.utcnow

    def test_expired_challenge_does_not_increment_attempts(self, ctx):
        """Otherwise an attacker keeps a dead row busy, and the attempt
        counter stops meaning anything."""
        cid, code = otp.create_challenge(SIGNUP, DEST)
        r = _row(cid)
        r.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        otp.verify_challenge(cid, _wrong(code))
        assert _row(cid).attempt_count == 0

    def test_expiry_needs_no_cleanup_job(self, ctx):
        """Expiry is enforced in the verification predicate. Nothing sweeps."""
        assert "def cleanup" not in _read("app/services/otp_service.py")


# ── 3. attempt counting ─────────────────────────────────────────────────────

class TestAttempts:

    def test_default_max_attempts_is_five(self, ctx):
        cid, _ = otp.create_challenge(SIGNUP, DEST)
        assert _row(cid).max_attempts == 5

    def test_attempt_count_increments_exactly_once(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        otp.verify_challenge(cid, _wrong(code))
        assert _row(cid).attempt_count == 1

    def test_each_failure_increments_exactly_once(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        for expected in (1, 2, 3):
            otp.verify_challenge(cid, _wrong(code))
            assert _row(cid).attempt_count == expected

    def test_max_attempts_blocks_further_verification(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        for _ in range(5):
            otp.verify_challenge(cid, _wrong(code))
        assert _row(cid).attempt_count == 5
        # Even the CORRECT code is now refused.
        assert otp.verify_challenge(cid, code) == (False, otp.ATTEMPTS_EXHAUSTED)

    def test_exhausted_challenge_does_not_increment_further(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        for _ in range(6):
            otp.verify_challenge(cid, _wrong(code))
        assert _row(cid).attempt_count == 5

    def test_consumed_challenge_does_not_increment_attempts(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        otp.verify_challenge(cid, code)
        before = _row(cid).attempt_count
        otp.verify_challenge(cid, _wrong(code))
        assert _row(cid).attempt_count == before

    def test_max_attempts_is_read_from_the_row_not_config(self, ctx):
        """Raising the policy later must not retroactively re-open a
        challenge that had already exhausted its attempts."""
        cid, code = otp.create_challenge(SIGNUP, DEST, max_attempts=1)
        otp.verify_challenge(cid, _wrong(code))
        assert otp.verify_challenge(cid, code)[1] == otp.ATTEMPTS_EXHAUSTED


# ── 4. generation ───────────────────────────────────────────────────────────

class TestGeneration:

    def test_code_is_exactly_six_digits(self, ctx):
        for _ in range(200):
            c = otp.generate_code()
            assert len(c) == 6 and c.isdigit()

    def test_leading_zeros_are_preserved(self, ctx, monkeypatch):
        """secrets.randbelow legitimately returns 4211; rendering that as
        "4211" shrinks the space and produces an untypeable code."""
        monkeypatch.setattr(otp, "secrets", _FakeSecrets(4211))
        assert otp.generate_code() == "004211"

    def test_zero_renders_as_six_zeros(self, ctx, monkeypatch):
        monkeypatch.setattr(otp, "secrets", _FakeSecrets(0))
        assert otp.generate_code() == "000000"

    def test_a_zero_padded_code_verifies(self, ctx, monkeypatch):
        """The RENDERED string is canonical and is what gets hashed."""
        monkeypatch.setattr(otp, "secrets", _FakeSecrets(42))
        cid, code = otp.create_challenge(SIGNUP, DEST)
        monkeypatch.undo()
        assert code == "000042"
        assert otp.verify_challenge(cid, "000042") == (True, otp.OK)

    def test_csprng_is_used_not_random(self):
        src = _code_only("app/services/otp_service.py")
        assert "import secrets" in src
        assert "secrets.randbelow" in src
        assert "import random" not in src
        for banned in ("random.randint", "random.randrange", "random.choice"):
            assert banned not in src, banned

    def test_codes_are_not_trivially_repeating(self, ctx):
        assert len({otp.generate_code() for _ in range(200)}) > 100


# ── 5. HMAC ─────────────────────────────────────────────────────────────────

class TestHmac:

    def test_plaintext_is_never_persisted(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        r = _row(cid)
        assert code not in r.code_hash
        # No column anywhere on the row holds it.
        for col in r.__table__.columns.keys():
            assert str(getattr(r, col)) != code, col

    def test_hash_is_a_sha256_hex_digest(self, ctx):
        cid, _ = otp.create_challenge(SIGNUP, DEST)
        assert re.fullmatch(r"[0-9a-f]{64}", _row(cid).code_hash)

    def test_changing_purpose_changes_the_hash(self, ctx):
        a = otp.compute_hash(SIGNUP, DEST, "123456")
        b = otp.compute_hash(LOGIN, DEST, "123456")
        assert a != b

    def test_changing_destination_changes_the_hash(self, ctx):
        a = otp.compute_hash(SIGNUP, DEST, "123456")
        b = otp.compute_hash(SIGNUP, DEST2, "123456")
        assert a != b

    def test_changing_the_code_changes_the_hash(self, ctx):
        assert otp.compute_hash(SIGNUP, DEST, "123456") != \
               otp.compute_hash(SIGNUP, DEST, "123457")

    def test_canonical_encoding_is_unambiguous(self, ctx):
        """Length-prefixed, so field-boundary shuffling cannot collide -- the
        classic ("ab","c") vs ("a","bc") delimiter ambiguity."""
        assert otp.compute_hash("ab", "c", "123456") != \
               otp.compute_hash("a", "bc", "123456")

    def test_compare_digest_is_used_not_equality(self):
        src = _read("app/services/otp_service.py")
        assert "hmac.compare_digest" in src
        assert "== stored_hash" not in src
        assert "code_hash ==" not in src

    def test_hash_is_keyed_not_a_bare_digest(self):
        """A bare sha256 over 10**6 values is a rainbow table."""
        src = _read("app/services/otp_service.py")
        assert "hmac.new(" in src
        assert "hashlib.sha256(" not in src.replace("hashlib.sha256)", "")

    def test_a_different_key_produces_a_different_hash(self, ctx):
        first = otp.compute_hash(SIGNUP, DEST, "123456")
        _APP.config["OTP_HMAC_KEY"] = "a-completely-different-test-key-value"
        try:
            assert otp.compute_hash(SIGNUP, DEST, "123456") != first
        finally:
            _APP.config.pop("OTP_HMAC_KEY", None)

    def test_a_missing_key_raises_rather_than_hashing(self, ctx):
        import app.config as cfg
        saved = cfg.OTP_HMAC_KEY
        cfg.OTP_HMAC_KEY = ""
        try:
            with pytest.raises(otp.OtpConfigurationError):
                otp.compute_hash(SIGNUP, DEST, "123456")
        finally:
            cfg.OTP_HMAC_KEY = saved

    def test_a_placeholder_key_is_refused(self, ctx):
        import app.config as cfg
        saved = cfg.OTP_HMAC_KEY
        cfg.OTP_HMAC_KEY = "short"
        try:
            with pytest.raises(otp.OtpConfigurationError):
                otp.compute_hash(SIGNUP, DEST, "123456")
        finally:
            cfg.OTP_HMAC_KEY = saved


# ── 6. latest-wins ──────────────────────────────────────────────────────────

class TestLatestWins:

    def test_new_challenge_invalidates_the_previous_one(self, ctx):
        first_id, first_code = otp.create_challenge(SIGNUP, DEST)
        otp.create_challenge(SIGNUP, DEST)
        assert _row(first_id).invalidated_at is not None
        assert otp.verify_challenge(first_id, first_code) == (False, otp.INVALIDATED)

    def test_the_new_challenge_remains_valid(self, ctx):
        otp.create_challenge(SIGNUP, DEST)
        second_id, second_code = otp.create_challenge(SIGNUP, DEST)
        assert otp.verify_challenge(second_id, second_code) == (True, otp.OK)

    def test_a_different_purpose_is_not_invalidated(self, ctx):
        signup_id, signup_code = otp.create_challenge(SIGNUP, DEST)
        otp.create_challenge(LOGIN, DEST)
        assert _row(signup_id).invalidated_at is None
        assert otp.verify_challenge(signup_id, signup_code) == (True, otp.OK)

    def test_a_different_destination_is_not_invalidated(self, ctx):
        first_id, first_code = otp.create_challenge(SIGNUP, DEST)
        otp.create_challenge(SIGNUP, DEST2)
        assert _row(first_id).invalidated_at is None
        assert otp.verify_challenge(first_id, first_code) == (True, otp.OK)

    def test_a_consumed_challenge_is_not_re_invalidated(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        otp.verify_challenge(cid, code)
        otp.create_challenge(SIGNUP, DEST)
        assert _row(cid).invalidated_at is None
        assert _row(cid).consumed_at is not None

    def test_invalidated_challenge_does_not_increment_attempts(self, ctx):
        first_id, first_code = otp.create_challenge(SIGNUP, DEST)
        otp.create_challenge(SIGNUP, DEST)
        otp.verify_challenge(first_id, _wrong(first_code))
        assert _row(first_id).attempt_count == 0


# ── 7. tenant boundary ──────────────────────────────────────────────────────

class TestTenantBoundary:

    def test_pre_tenant_signup_stores_null_tenant(self, ctx):
        """The tenant does not exist yet and must not be invented."""
        cid, code = otp.create_challenge(SIGNUP, DEST)
        assert _row(cid).tenant_id is None
        assert otp.verify_challenge(cid, code, tenant_id=None) == (True, otp.OK)

    def test_a_tenant_bound_challenge_verifies_for_that_tenant(self, ctx):
        cid, code = otp.create_challenge(LOGIN, DEST, tenant_id=A)
        assert otp.verify_challenge(cid, code, tenant_id=A) == (True, otp.OK)

    def test_a_tenant_bound_challenge_fails_under_another_tenant(self, ctx):
        cid, code = otp.create_challenge(LOGIN, DEST, tenant_id=A)
        assert otp.verify_challenge(cid, code, tenant_id=B)[0] is False
        assert _row(cid).consumed_at is None

    def test_a_tenant_bound_challenge_fails_with_no_tenant(self, ctx):
        cid, code = otp.create_challenge(LOGIN, DEST, tenant_id=A)
        assert otp.verify_challenge(cid, code, tenant_id=None)[0] is False

    def test_a_null_tenant_challenge_fails_under_a_tenant(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        assert otp.verify_challenge(cid, code, tenant_id=A)[0] is False

    def test_user_id_is_optional_and_null_at_signup(self, ctx):
        cid, _ = otp.create_challenge(SIGNUP, DEST)
        assert _row(cid).user_id is None

    def test_a_user_bound_challenge_records_the_user(self, ctx):
        u = User(username="owner", email="owner@rc2516.test",
                 password_hash="x", role="ADMIN", tenant_id=A, is_active=True)
        db.session.add(u)
        db.session.commit()
        cid, _ = otp.create_challenge("phone_change", DEST,
                                      tenant_id=A, user_id=u.id)
        assert _row(cid).user_id == u.id

    def test_the_service_does_not_route_through_tenant_query(self):
        """tenant_query() fails closed, which would make every NULL-tenant
        signup challenge permanently unverifiable. The service owns its own
        access instead -- and must keep doing so."""
        assert "tenant_query" not in _read("app/services/otp_service.py")


# ── 8. concurrency ──────────────────────────────────────────────────────────

class TestConcurrency:

    def test_verification_is_not_select_inspect_save(self):
        """The shape is the guarantee. Under READ COMMITTED a read-modify-
        write loses increments and lets two verifiers both pass a 'not
        consumed' check."""
        src = _read("app/services/otp_service.py")
        assert "attempt_count += 1" not in src
        assert ".returning(" in src
        assert src.count("update(OtpChallenge)") >= 3   # invalidate, claim, consume

    def test_the_increment_is_computed_by_the_database(self):
        src = _read("app/services/otp_service.py")
        assert "OtpChallenge.attempt_count + 1" in src

    def test_the_consume_reasserts_the_full_predicate(self):
        """Between claim and consume another request may supersede the
        challenge, so consuming on 'not consumed' alone would be a weaker
        guard than the claim applied."""
        src = _read("app/services/otp_service.py")
        consume = src.split("# ── Step 2")[1]
        for clause in ("consumed_at.is_(None)", "invalidated_at.is_(None)",
                       "expires_at >"):
            assert clause in consume, clause

    def test_two_sequential_verifies_yield_exactly_one_success(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        results = [otp.verify_challenge(cid, code)[0] for _ in range(2)]
        assert results.count(True) == 1

    def test_two_threaded_verifies_yield_exactly_one_success(self, ctx):
        """Real threads, real sessions. Exactly one may consume."""
        cid, code = otp.create_challenge(SIGNUP, DEST)
        barrier = threading.Barrier(2)
        out = []

        def worker():
            with _APP.app_context():
                barrier.wait()
                try:
                    out.append(otp.verify_challenge(cid, code)[0])
                except Exception:                       # noqa: BLE001
                    out.append(None)                    # a DB lock is not a success
                finally:
                    db.session.remove()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert out.count(True) == 1, out
        assert _row(cid).consumed_at is not None

    def test_repeated_failures_do_not_lose_increments(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST, max_attempts=50)
        for _ in range(10):
            otp.verify_challenge(cid, _wrong(code))
        assert _row(cid).attempt_count == 10

    def test_consumption_survives_a_later_caller_rollback(self, ctx):
        """verify_challenge commits its own work. A caller whose transaction
        later rolls back must not be able to un-burn an accepted code."""
        cid, code = otp.create_challenge(SIGNUP, DEST)
        assert otp.verify_challenge(cid, code) == (True, otp.OK)
        db.session.rollback()
        assert _row(cid).consumed_at is not None
        assert otp.verify_challenge(cid, code)[1] == otp.ALREADY_CONSUMED


# ── 9. leakage ──────────────────────────────────────────────────────────────

class TestLeakage:

    def test_the_code_is_absent_from_the_persisted_row(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        from sqlalchemy import text
        row = db.session.execute(
            text("SELECT * FROM otp_challenges WHERE id = :i"), {"i": cid}).first()
        assert code not in " ".join(str(v) for v in row)

    def test_the_code_never_reaches_a_logger(self):
        """Verified by reading every logger call in the module: none may
        interpolate the code or the hash."""
        src = _read("app/services/otp_service.py")
        for call in re.findall(r"logger\.\w+\([^)]*\)", src, re.S):
            for banned in ("code", "code_hash", "presented", "_key()"):
                assert banned not in call, call

    def test_the_destination_is_masked_in_logs(self):
        src = _read("app/services/otp_service.py")
        for call in re.findall(r"logger\.\w+\([^)]*\)", src, re.S):
            if "destination" in call:
                assert "mask_destination" in call, call

    def test_mask_destination_hides_all_but_the_last_three(self, ctx):
        assert otp.mask_destination("919847312534") == "*********534"
        assert "9847" not in otp.mask_destination("919847312534")

    def test_the_repr_leaks_neither_destination_nor_hash(self, ctx):
        cid, _ = otp.create_challenge(SIGNUP, DEST)
        r = repr(_row(cid))
        assert DEST not in r and _row(cid).code_hash not in r

    def test_exceptions_never_carry_the_key_or_a_code(self, ctx):
        import app.config as cfg
        saved = cfg.OTP_HMAC_KEY
        cfg.OTP_HMAC_KEY = ""
        try:
            with pytest.raises(otp.OtpConfigurationError) as exc:
                otp.compute_hash(SIGNUP, DEST, "123456")
            assert "123456" not in str(exc.value)
            assert saved not in str(exc.value)
        finally:
            cfg.OTP_HMAC_KEY = saved

    def test_no_audit_integration_in_this_phase(self):
        """log_audit() COMMITS the session, so calling it mid-verification
        would commit a consume at a moment of its choosing."""
        src = _code_only("app/services/otp_service.py")
        assert "log_audit" not in src
        assert "audit_service" not in src

    def test_no_debug_print_statements(self):
        src = _read("app/services/otp_service.py")
        assert not re.search(r"^\s*print\(", src, re.M)


# ── 10. scope: out of scope stays out of scope ──────────────────────────────

class TestScope:

    def test_the_service_has_no_callers(self):
        """RC2.5.16 Gate B ships the primitive and nothing that uses it."""
        import glob
        for path in glob.glob(os.path.join(_ROOT, "app", "**", "*.py"),
                              recursive=True):
            if path.endswith(os.path.join("services", "otp_service.py")):
                continue
            rel = os.path.relpath(path, _ROOT).replace(os.sep, "/")
            assert "otp_service" not in _code_only(rel), path

    def test_no_otp_route_was_created(self):
        rules = {str(r) for r in _APP.url_map.iter_rules()}
        for banned in ("/otp", "/send-otp", "/verify-otp", "/phone-login",
                       "/login-phone"):
            assert banned not in rules, banned

    def test_no_whatsapp_or_meta_integration(self):
        src = _read("app/services/otp_service.py")
        for banned in ("whatsapp", "send_template", "graph.facebook", "requests"):
            assert banned not in src.lower(), banned

    def test_registration_and_login_are_untouched(self):
        pub = _read("app/routes/public.py")
        adm = _read("app/routes/admin.py")
        assert "otp" not in pub.lower()
        assert "User.query.filter_by(email=email).first()" in adm

    def test_password_hash_is_still_required(self):
        assert "password_hash = db.Column(db.String(256), nullable=False)" in \
               _read("app/models.py")

    def test_admin_key_and_auth_mode_are_untouched(self):
        cfg = _read("app/config.py")
        assert 'ADMIN_KEY            = os.environ.get("ADMIN_KEY", "oxford_admin_2026")' in cfg
        assert 'if AUTH_MODE in ("ADMIN_KEY_ONLY", "DUAL"):' in cfg

    def test_the_new_secret_does_not_break_boot(self):
        """Production has no OTP_HMAC_KEY. A boot-time requirement would turn
        a deploy of dormant code into an outage -- the RC2.5.15 lesson."""
        cfg = _read("app/config.py")
        assert 'OTP_HMAC_KEY         = os.environ.get("OTP_HMAC_KEY", "")' in cfg
        guard = cfg.split("DEBUG = ")[1]
        assert "OTP_HMAC_KEY" not in guard
        assert "OTP_HMAC_KEY" not in _read("app/__init__.py")

    def test_the_migration_only_creates_the_otp_table(self):
        src = _read("migrations/versions/c9e5a1f38b64_rc2_5_16_otp_challenges.py")
        up = src.split("def downgrade")[0]
        assert "down_revision = 'b7d2e4f91a35'" in src
        assert up.count("op.create_table(") == 1
        assert "'otp_challenges'" in up
        for banned in ("alter_column", "drop_table", "op.execute", "add_column"):
            assert banned not in up, banned

    def test_no_uniqueness_constraint_was_added(self):
        up = _read("migrations/versions/c9e5a1f38b64_rc2_5_16_otp_challenges.py"
                   ).split("def downgrade")[0]
        assert "unique=True" not in up

    def test_no_durable_rate_limiting_was_built(self):
        src = _read("app/services/otp_service.py")
        assert "check_rate_limit" not in src
        assert "rate_limit" not in src.replace("rate limiter", "")


# ── 11. Gate B.2: latest-wins is a DATABASE invariant ───────────────────────

class TestOneActiveInvariant:
    """RC2.5.16 Gate B.2.

    Gate B.1 validated the primitive against PostgreSQL 18.4 at READ COMMITTED
    and found that concurrent create_challenge() calls for one
    (destination, purpose) could each leave an ACTIVE challenge -- three
    concurrently created challenges verified INDEPENDENTLY, three live
    credentials where policy allows one. A concurrent transaction's INSERT is
    invisible to another's invalidating UPDATE until it commits, so nobody
    invalidates the rows the others are adding. Application logic cannot fix a
    phantom insert at READ COMMITTED; only the database can.

    READ THIS BEFORE TRUSTING THIS CLASS ON SQLITE
    ----------------------------------------------
    SQLite serialises writers, so it CANNOT reproduce the race and these tests
    cannot prove the fix on it. They pin the MECHANISM (the partial unique
    index exists, in the model and in the migration, with the right predicate)
    and the SEQUENTIAL semantics. The concurrency property itself is proven on
    PostgreSQL by TestPostgresOneActiveInvariant below, which is authoritative
    for it and skips when no PostgreSQL URL is supplied.
    """

    def test_the_partial_unique_index_is_declared_on_the_model(self):
        from app.models import OtpChallenge
        idx = {i.name: i for i in OtpChallenge.__table__.indexes}
        assert "uq_otp_challenges_one_active" in idx
        target = idx["uq_otp_challenges_one_active"]
        assert target.unique is True
        assert [c.name for c in target.columns] == ["destination", "purpose"]

    def test_the_index_predicate_excludes_consumed_and_invalidated(self):
        from app.models import OtpChallenge
        idx = {i.name: i for i in OtpChallenge.__table__.indexes}
        target = idx["uq_otp_challenges_one_active"]
        for dialect in ("postgresql", "sqlite"):
            pred = str(target.dialect_options[dialect]["where"])
            assert "consumed_at IS NULL" in pred, dialect
            assert "invalidated_at IS NULL" in pred, dialect

    def test_the_index_predicate_does_not_reference_expires_at(self):
        """A partial index predicate must be IMMUTABLE, so `expires_at > NOW()`
        is rejected by PostgreSQL outright:
            ERROR: functions in index predicate must be marked IMMUTABLE
        Omitting it is not a compromise -- this predicate is a SUPERSET of
        ACTIVE, and uniqueness over a superset implies uniqueness over its
        subset. The index is therefore STRICTLY STRONGER than 'at most one
        ACTIVE', and no expiry assumption is baked into the schema."""
        from app.models import OtpChallenge
        idx = {i.name: i for i in OtpChallenge.__table__.indexes}
        pred = str(idx["uq_otp_challenges_one_active"]
                   .dialect_options["postgresql"]["where"])
        assert "expires_at" not in pred
        assert "NOW()" not in pred.upper()

    def test_the_migration_creates_the_index_and_chains_correctly(self):
        src = _read("migrations/versions/"
                    "d1b6c48e7f92_rc2_5_16_b2_otp_one_active_index.py")
        assert "down_revision = 'c9e5a1f38b64'" in src
        up = src.split("def downgrade")[0]
        assert "uq_otp_challenges_one_active" in up
        assert "unique=True" in up
        assert "postgresql_where" in up and "sqlite_where" in up
        for banned in ("add_column", "drop_column", "alter_column",
                       "create_table", "op.execute"):
            assert banned not in up, banned
        assert "drop_index" in src.split("def downgrade")[1]

    def test_the_index_predicate_in_the_migration_omits_expiry(self):
        src = _read("migrations/versions/"
                    "d1b6c48e7f92_rc2_5_16_b2_otp_one_active_index.py")
        predicate_line = [l for l in src.splitlines()
                          if l.startswith("_PREDICATE")][0]
        assert "consumed_at IS NULL" in predicate_line
        assert "invalidated_at IS NULL" in predicate_line
        assert "expires_at" not in predicate_line
        assert "NOW" not in predicate_line.upper()

    def test_the_invalidate_predicate_matches_the_index_predicate(self):
        """They must describe the SAME row set. If the UPDATE were narrower, a
        stale row would sit in the index slot forever and block every new
        challenge for that destination; if wider, it would erase history."""
        src = _code_only("app/services/otp_service.py")
        create = src.split("def create_challenge")[1].split("def verify_challenge")[0]
        assert "consumed_at.is_(None)" in create
        assert "invalidated_at.is_(None)" in create

    def test_an_integrity_error_is_retried_not_surfaced(self):
        src = _code_only("app/services/otp_service.py")
        create = src.split("def create_challenge")[1].split("def verify_challenge")[0]
        assert "IntegrityError" in create
        assert "rollback()" in create
        assert "_CREATE_MAX_RETRIES" in create

    def test_contention_raises_a_defined_error_not_integrityerror(self):
        assert issubclass(otp.OtpContentionError, RuntimeError)
        assert "OtpContentionError" in _code_only("app/services/otp_service.py")

    def test_verify_challenge_was_not_modified_by_this_remediation(self):
        """Gate B.2 is scoped to create_challenge(). The verification path was
        already validated on PostgreSQL and must not have been touched."""
        src = _code_only("app/services/otp_service.py")
        verify = src.split("def verify_challenge")[1].split("def _diagnose")[0]
        assert "IntegrityError" not in verify
        assert "_CREATE_MAX_RETRIES" not in verify
        assert "hmac.compare_digest" in verify
        assert ".returning(" in verify

    def test_sequential_latest_wins_still_holds(self, ctx):
        first, code1 = otp.create_challenge(SIGNUP, DEST)
        second, code2 = otp.create_challenge(SIGNUP, DEST)
        assert otp.verify_challenge(first, code1) == (False, otp.INVALIDATED)
        assert otp.verify_challenge(second, code2) == (True, otp.OK)

    def test_only_one_row_ever_occupies_the_slot(self, ctx):
        for _ in range(6):
            otp.create_challenge(SIGNUP, DEST)
        live = db.session.query(OtpChallenge).filter(
            OtpChallenge.destination == DEST, OtpChallenge.purpose == SIGNUP,
            OtpChallenge.consumed_at.is_(None),
            OtpChallenge.invalidated_at.is_(None)).count()
        assert live == 1

    def test_an_expired_row_does_not_block_a_new_challenge(self, ctx):
        """Scenario H. The expired row still occupies the index slot, so if the
        invalidate step did not clear it, creation would fail forever."""
        old, _ = otp.create_challenge(SIGNUP, DEST)
        r = _row(old)
        r.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        new, code = otp.create_challenge(SIGNUP, DEST)
        assert otp.verify_challenge(new, code) == (True, otp.OK)

    def test_a_consumed_row_frees_the_slot(self, ctx):
        cid, code = otp.create_challenge(SIGNUP, DEST)
        otp.verify_challenge(cid, code)
        new, code2 = otp.create_challenge(SIGNUP, DEST)
        assert _row(cid).consumed_at is not None
        assert _row(cid).invalidated_at is None
        assert otp.verify_challenge(new, code2) == (True, otp.OK)

    def test_history_is_retained_without_limit(self, ctx):
        for _ in range(5):
            otp.create_challenge(SIGNUP, DEST)
        assert db.session.query(OtpChallenge).filter(
            OtpChallenge.destination == DEST).count() == 5

    def test_the_slot_is_per_destination_and_purpose(self, ctx):
        otp.create_challenge(SIGNUP, DEST)
        otp.create_challenge(SIGNUP, DEST2)
        otp.create_challenge(LOGIN, DEST)
        live = db.session.query(OtpChallenge).filter(
            OtpChallenge.consumed_at.is_(None),
            OtpChallenge.invalidated_at.is_(None)).count()
        assert live == 3


# ── 12. PostgreSQL is authoritative for the concurrency property ────────────

_PG_URL = os.environ.get("OTP_PG_TEST_URL")


@pytest.mark.skipif(not _PG_URL,
                    reason="set OTP_PG_TEST_URL to a DISPOSABLE PostgreSQL "
                           "database to run the authoritative concurrency "
                           "validation (SQLite serialises writers and cannot "
                           "reproduce the READ COMMITTED race)")
class TestPostgresOneActiveInvariant:
    """The authoritative proof of the Gate B.2 invariant.

    SQLite cannot reproduce the phantom-insert race, so no SQLite test may be
    read as evidence for it. This class runs only against a real PostgreSQL
    database supplied via OTP_PG_TEST_URL, which must be DISPOSABLE -- it
    creates and deletes challenge rows. Never point it at production.
    """

    @staticmethod
    def _pg_app():
        from flask import Flask
        from app.extensions import db as _db
        app = Flask(__name__)
        app.config["SQLALCHEMY_DATABASE_URI"] = _PG_URL
        app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_size": 20,
                                                   "max_overflow": 20}
        app.config["OTP_HMAC_KEY"] = os.environ["OTP_HMAC_KEY"]
        _db.init_app(app)
        return app

    def _concurrent_creates(self, app, n, dest):
        barrier = threading.Barrier(n)
        made, errs = [], []

        def w(_):
            try:
                with app.app_context():
                    barrier.wait(timeout=60)
                    made.append(otp.create_challenge(SIGNUP, dest))
            except Exception as e:                               # noqa: BLE001
                errs.append(type(e).__name__)
            finally:
                with app.app_context():
                    db.session.remove()

        ts = [threading.Thread(target=w, args=(i,)) for i in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=120)
        return made, errs

    @pytest.mark.parametrize("workers", [2, 3, 4, 8, 12])
    def test_concurrent_creation_leaves_at_most_one_active(self, workers):
        app = self._pg_app()
        dest = f"9199{workers:08d}"
        with app.app_context():
            db.session.query(OtpChallenge).filter(
                OtpChallenge.destination == dest).delete()
            db.session.commit()
        made, errs = self._concurrent_creates(app, workers, dest)
        with app.app_context():
            live = db.session.query(OtpChallenge).filter(
                OtpChallenge.destination == dest,
                OtpChallenge.consumed_at.is_(None),
                OtpChallenge.invalidated_at.is_(None)).count()
            assert live <= 1, f"{live} live challenges after {workers} creators"
            assert not [e for e in errs if e != "OtpContentionError"], errs
            usable = sum(1 for cid, code in made
                         if otp.verify_challenge(cid, code)[0])
            assert usable <= 1, f"{usable} usable codes"
            db.session.query(OtpChallenge).filter(
                OtpChallenge.destination == dest).delete()
            db.session.commit()
