"""Phase RC2.5.16 — the OTP challenge primitive.

Answers exactly one question:

    "Was this code, for this purpose and destination, presented correctly,
     once, before it expired?"

It creates and verifies challenges. It does NOT send anything, does not know
what a delivery channel is, does not create sessions, does not touch User, and
does not decide what a success means. Those belong to later, separately
authorised phases. There are currently NO callers.

WHY THE VERIFY IS TWO STATEMENTS
--------------------------------
Two requirements pull in opposite directions:

  * the success predicate must be atomic, so exactly one concurrent verifier
    can consume a challenge and no attempt increment is ever lost;
  * the secret comparison must be constant-time (hmac.compare_digest), which
    a SQL `code_hash = :hash` predicate is not.

Doing it in one UPDATE would satisfy the first and quietly drop the second.
Doing it as SELECT -> inspect in Python -> save is what the authorisation
forbids, and rightly: under PostgreSQL's default READ COMMITTED isolation two
concurrent verifiers both read attempt_count = 2 and both write 3, losing an
attempt, and both can pass a "not consumed" check.

So verification is two statements, each atomic, in this order:

  1. CLAIM  -- one conditional UPDATE that increments attempt_count and
               returns code_hash, but only if the challenge is ACTIVE.
               The increment and the state check are the same statement, so
               concurrent failures cannot lose increments and an expired or
               consumed challenge can never be charged an attempt.
  2. CONSUME -- compare_digest in Python, then a second conditional UPDATE
               that sets consumed_at, re-asserting the FULL predicate. Only
               one concurrent verifier can win it.

Re-asserting the whole predicate in step 2 matters: between the two statements
another request may have superseded the challenge (latest-wins), so consuming
on `consumed_at IS NULL` alone would be a narrower guard than step 1 applied.

A successful verification also consumes one attempt, because step 1 runs
first. That is harmless -- the challenge is consumed in the same call -- and
it is the honest reading of "attempts made".

TRANSACTION BOUNDARY
--------------------
verify_challenge() COMMITS its own work before returning. A caller whose own
transaction later rolls back must not be able to un-burn a code that was
already accepted; a consumed challenge stays consumed. Callers must therefore
treat a True result as final.

For the same reason this module never calls log_audit(): that helper commits
the session (see audit_service's docstring), and calling it mid-verification
would commit a consume at a moment of its choosing rather than ours. Audit
integration belongs to the phase that adds a caller.

NOTHING HERE EVER LOGS THE CODE
-------------------------------
Not the code, not code_hash, not OTP_HMAC_KEY, and never a full destination.
RC2.5.15 disclosed that a customer phone number can reach production logs
through an incidental path; this module is written so there is no such path.
"""
import hashlib
import hmac
import logging
import secrets
import time
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

# ── Policy constants (RC2.5.16 Gate B approved decisions) ───────────────────
CODE_LENGTH = 6
CODE_TTL_SECONDS = 300          # 5 minutes
MAX_ATTEMPTS = 5

#: Gate B.2: how many times a creator that LOST the unique-index race will
#: re-run invalidate+insert. Each round has exactly one winner, so N-way
#: contention needs up to N rounds; 12 covers the concurrency Gate B.2 tests
#: and far more than a double-clicked button produces. Exhaustion raises
#: OtpContentionError rather than leaking IntegrityError.
_CREATE_MAX_RETRIES = 12

#: Shortest key accepted. Not a cryptographic threshold so much as a guard
#: against a placeholder ("changeme", "") reaching a keyed digest, which would
#: make every stored hash forgeable by anyone who read the source.
_MIN_KEY_LENGTH = 16

# ── Internal verification results ──────────────────────────────────────────
# These are INTERNAL. A future HTTP caller must collapse every failure into
# one indistinguishable response: telling a client "expired" versus "wrong
# code" versus "no such challenge" lets it map challenge state, which is the
# oracle class RC2.5.15 just removed from /register.
OK = "OK"
INVALID_CODE = "INVALID_CODE"
EXPIRED = "EXPIRED"
ALREADY_CONSUMED = "ALREADY_CONSUMED"
ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
NO_SUCH_CHALLENGE = "NO_SUCH_CHALLENGE"
MALFORMED_INPUT = "MALFORMED_INPUT"
INVALIDATED = "INVALIDATED"


class OtpConfigurationError(RuntimeError):
    """OTP_HMAC_KEY is missing or unusable.

    Raised at the point of USE, not at boot. Production has no such variable
    and this phase ships no caller, so a boot-time requirement would turn a
    deployment of dormant code into an outage. The phase that introduces a
    caller is the phase that must provision the key.

    The message names the variable and NEVER its value.
    """


class OtpContentionError(RuntimeError):
    """Lost the latest-wins race repeatedly and gave up.

    Gate B.2. Signals contention, not a fault: another caller's challenge is
    active. Raised instead of a raw IntegrityError so a caller can tell the two
    apart. Carries no code, no destination and no key.
    """


def _utcnow():
    """Naive UTC, matching this repository's convention.

    models.py uses datetime.utcnow() 49 times and production stores
    `timestamp without time zone`; the database server is Etc/UTC. Note that
    datetime.now() (LOCAL time) also appears elsewhere in the codebase and
    coincides with UTC only because the server happens to be UTC -- this
    module never relies on that coincidence.
    """
    return datetime.utcnow()


def _key() -> bytes:
    """The HMAC key, or a loud failure. Never logs or returns the value.

    Read from app.config the way ADMIN_KEY and BROADCAST_API_KEY are read --
    create_app() copies only SECRET_KEY and AUTH_MODE into app.config, so a
    current_app.config lookup alone would find nothing. An app.config override
    is still honoured first, which keeps the key injectable in a test without
    reaching into the environment.
    """
    raw = ""
    try:
        from flask import current_app
        raw = current_app.config.get("OTP_HMAC_KEY") or ""
    except Exception:                                       # noqa: BLE001
        raw = ""
    if not raw:
        from app import config as _cfg
        raw = getattr(_cfg, "OTP_HMAC_KEY", "") or ""
    if not raw:
        raise OtpConfigurationError(
            "OTP_HMAC_KEY is not configured. The OTP primitive cannot hash a "
            "challenge without it. Set it in the environment.")
    if len(raw) < _MIN_KEY_LENGTH:
        raise OtpConfigurationError(
            f"OTP_HMAC_KEY is shorter than {_MIN_KEY_LENGTH} characters and "
            "is refused as a placeholder.")
    return raw.encode("utf-8")


def _canonical(purpose: str, destination: str, code: str) -> bytes:
    """Unambiguous serialisation of the three bound fields.

    Length-prefixed rather than delimiter-joined. A plain separator invites
    the classic ambiguity where ("ab", "c") and ("a", "bc") hash identically
    if the separator can appear in a field; length prefixes cannot collide
    whatever the field contents.
    """
    parts = []
    for label, value in (("p", purpose), ("d", destination), ("c", code)):
        v = str(value)
        parts.append(f"{label}{len(v)}:{v}")
    return "|".join(parts).encode("utf-8")


def compute_hash(purpose: str, destination: str, code: str) -> str:
    """HMAC-SHA256 hex digest binding purpose, destination and code.

    Binding all three means a code is structurally unusable outside the exact
    challenge it was issued for: a tenant_signup code cannot verify as a
    phone_login code, and a code for one number cannot verify for another --
    the digests simply do not match, with no separate check to forget.
    """
    return hmac.new(_key(), _canonical(purpose, destination, code),
                    hashlib.sha256).hexdigest()


def generate_code() -> str:
    """A cryptographically secure six-digit code, zero-padded.

    secrets, never random: app/bot/constants.py uses random.choice() to pick
    reply copy, which is fine there and would be a credential defect here.

    The zero-padding is not cosmetic. secrets.randbelow(10**6) legitimately
    returns 4211, and rendering that as "4211" would both shrink the effective
    space and produce a code the user cannot type back correctly. The RENDERED
    string is canonical and is what gets hashed.
    """
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


def create_challenge(purpose: str, destination: str, *,
                     tenant_id=None, user_id=None,
                     ttl_seconds: int = CODE_TTL_SECONDS,
                     max_attempts: int = MAX_ATTEMPTS):
    """Create a challenge. Returns (challenge_id, plaintext_code).

    THE PLAINTEXT IS RETURNED EXACTLY ONCE and is never stored. The caller
    hands it to a delivery layer and drops it. It must not be logged, put in a
    response body, a flash message, a template, an exception or audit detail.

    LATEST-WINS: every ACTIVE challenge for the same (destination, purpose) is
    invalidated first, in the same transaction. Scoped to destination+purpose
    and NOT to tenant, deliberately -- a signup challenge has tenant_id NULL,
    so scoping by tenant would let a pre-tenant and a tenant-bound challenge
    for one number coexist, which is precisely the ambiguity latest-wins
    exists to remove.

    tenant_id stays None for a pre-tenant signup. No tenant is invented.
    """
    from app.extensions import db
    from app.models import OtpChallenge
    from sqlalchemy import update

    if not purpose or not str(purpose).strip():
        raise ValueError("create_challenge requires a purpose")
    if not destination or not str(destination).strip():
        raise ValueError("create_challenge requires a destination")

    purpose = str(purpose).strip()
    destination = str(destination).strip()

    # Fail BEFORE writing anything if the key is unusable, so a missing key
    # cannot leave an unverifiable challenge behind.
    code = generate_code()

    # ── Gate B.2: the unique index decides, and a loser retries ────────────
    # uq_otp_challenges_one_active makes two live challenges per
    # (destination, purpose) unrepresentable. Under concurrency the INSERT
    # below therefore blocks on the index until the competing transaction
    # commits, and then raises IntegrityError -- which is the CORRECT outcome
    # and not an error condition: it means another create won the slot.
    #
    # The loser re-runs the whole unit. Its second attempt now SEES the
    # winner's committed row, invalidates it, and inserts. That is precisely
    # latest-wins: the most recent caller ends up active. Retrying rather than
    # failing is what makes Scenario C ("exactly ONE final active challenge")
    # true for every caller instead of only the luckiest.
    #
    # The rollback before each retry is mandatory: a failed INSERT aborts the
    # PostgreSQL transaction, and every subsequent statement on that session
    # would fail until it is rolled back.
    last_error = None
    for attempt in range(_CREATE_MAX_RETRIES):
        now = _utcnow()
        code_hash = compute_hash(purpose, destination, code)
        try:
            db.session.execute(
                update(OtpChallenge)
                .where(OtpChallenge.destination == destination,
                       OtpChallenge.purpose == purpose,
                       OtpChallenge.consumed_at.is_(None),
                       OtpChallenge.invalidated_at.is_(None))
                .values(invalidated_at=now)
            )

            challenge = OtpChallenge(
                purpose=purpose,
                destination=destination,
                code_hash=code_hash,
                created_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
                attempt_count=0,
                max_attempts=max_attempts,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            db.session.add(challenge)
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            last_error = exc
            # Tiny randomised backoff so a thundering herd does not retry in
            # lockstep. secrets, not random, only because this module must not
            # import random at all (a test pins that).
            time.sleep(0.005 + (secrets.randbelow(10) / 1000.0))
            continue

        # Destination is masked; the code appears nowhere.
        logger.info("[otp] challenge created id=%s purpose=%s destination=%s "
                    "attempts=%s", challenge.id, purpose,
                    mask_destination(destination), attempt + 1)
        return challenge.id, code

    # Exhausted. Raised as a DEFINED service error rather than leaking a raw
    # IntegrityError, so a caller can distinguish "contention, try again" from
    # a programming fault. The invariant is intact either way: losing the race
    # repeatedly means somebody else's challenge is active, never that two are.
    raise OtpContentionError(
        f"could not create an OTP challenge for this destination and purpose "
        f"after {_CREATE_MAX_RETRIES} attempts due to concurrent creation"
    ) from last_error


def verify_challenge(challenge_id, code, *, purpose=None, destination=None,
                     tenant_id=None):
    """Verify and atomically consume. Returns (ok: bool, reason: str).

    `reason` is INTERNAL. A future route must not pass it to a client.

    purpose, destination and tenant_id are the caller's EXPECTATION. When
    supplied they must match the stored row, so a challenge cannot be verified
    out of its context even if an id leaks. tenant_id is compared with NULL
    equality semantics: None matches a NULL-tenant (pre-signup) challenge and
    does not match a tenant-bound one.
    """
    from app.extensions import db
    from app.models import OtpChallenge
    from sqlalchemy import update, select

    if code is None or not str(code).strip():
        return False, MALFORMED_INPUT
    code = str(code).strip()

    now = _utcnow()

    # ── Step 1: CLAIM an attempt, atomically, only if ACTIVE ───────────────
    # The state check and the increment are ONE statement, so two concurrent
    # failures cannot both read the same attempt_count, and an expired or
    # consumed challenge can never be charged an attempt.
    claim = (
        update(OtpChallenge)
        .where(OtpChallenge.id == challenge_id,
               OtpChallenge.consumed_at.is_(None),
               OtpChallenge.invalidated_at.is_(None),
               OtpChallenge.expires_at > now,
               OtpChallenge.attempt_count < OtpChallenge.max_attempts)
        .values(attempt_count=OtpChallenge.attempt_count + 1)
        .returning(OtpChallenge.code_hash,
                   OtpChallenge.purpose,
                   OtpChallenge.destination,
                   OtpChallenge.tenant_id)
    )
    row = db.session.execute(claim).first()
    db.session.commit()

    if row is None:
        return False, _diagnose(challenge_id, now)

    stored_hash, stored_purpose, stored_destination, stored_tenant = row

    # Context must match the caller's expectation.
    if purpose is not None and purpose != stored_purpose:
        return False, INVALID_CODE
    if destination is not None and destination != stored_destination:
        return False, INVALID_CODE
    if tenant_id != stored_tenant:
        return False, INVALID_CODE

    # ── Step 2: constant-time comparison, then atomic CONSUME ──────────────
    presented = compute_hash(stored_purpose, stored_destination, code)
    if not hmac.compare_digest(presented, stored_hash):
        return False, INVALID_CODE

    consume = (
        update(OtpChallenge)
        .where(OtpChallenge.id == challenge_id,
               OtpChallenge.consumed_at.is_(None),
               OtpChallenge.invalidated_at.is_(None),
               OtpChallenge.expires_at > now)
        .values(consumed_at=now)
        .returning(OtpChallenge.id)
    )
    consumed = db.session.execute(consume).first()
    db.session.commit()

    if consumed is None:
        # Another verifier won the race, or the challenge was superseded
        # between the two statements. Exactly one caller may succeed.
        return False, _diagnose(challenge_id, now)

    logger.info("[otp] challenge consumed id=%s purpose=%s",
                challenge_id, stored_purpose)
    return True, OK


def _diagnose(challenge_id, now):
    """Why a claim or consume failed. INTERNAL ONLY -- never sent to a client.

    A separate read, deliberately: it runs only on the failure path, where
    there is nothing left to race for, and keeps the atomic statements free of
    diagnostic branching.
    """
    from app.extensions import db
    from app.models import OtpChallenge

    row = db.session.get(OtpChallenge, challenge_id)
    if row is None:
        return NO_SUCH_CHALLENGE
    if row.consumed_at is not None:
        return ALREADY_CONSUMED
    if row.invalidated_at is not None:
        return INVALIDATED
    if row.expires_at <= now:
        return EXPIRED
    if row.attempt_count >= row.max_attempts:
        return ATTEMPTS_EXHAUSTED
    return INVALID_CODE


def mask_destination(destination: str) -> str:
    """Last 3 digits only, for logs. Never the full number."""
    s = str(destination or "")
    return ("*" * max(0, len(s) - 3)) + s[-3:] if s else ""
