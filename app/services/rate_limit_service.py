"""rate_limit_service.py — Phase RC2.5.17 Gate B

A DURABLE, cross-worker rate limiter for the OTP primitive.

WHY THIS EXISTS
---------------
app.routes.public._RATE_LIMITS is a process-local dict. It is adequate for the
six things it guards and structurally unable to guard an OTP budget:

  * it evaporates on every deploy, restart and crash -- and this application
    deploys on every push to main, so an attacker resets the budget by waiting
    for a release, or by causing one;
  * it is per-worker, so at WEB_CONCURRENCY=1 it is a limit and at
    WEB_CONCURRENCY=2 it is silently two limits. The production value is 1
    today. A rate limit whose correctness depends on a scaling variable nobody
    is watching is not a rate limit.

Each OTP send costs real money and reaches a real phone, so the ceiling has to
survive the process. Those six existing call sites are deliberately NOT
migrated here: they are a different risk with a different blast radius, and
changing them is not in this phase's scope.

WHAT THIS MODULE IS NOT
-----------------------
It is not part of the OTP primitive. otp_service.py contains no durable
rate-limiting logic and does not import this module -- the primitive answers
"is this code correct?", the limiter answers "may this caller ask again?", and
a caller composes them. Keeping them apart is what lets the limiter meter
events that produce no challenge row at all: a DENIED create consumes budget
precisely because it must not be retryable for free.

It is also not an audit log. A denial is a return value, not a row in
audit_log; log_audit() has a closed VALID_ACTIONS set and commits the session,
and neither property is wanted on this path.

THE TRANSACTION BOUNDARY
------------------------
Every limiter decision runs in its OWN connection and its OWN transaction,
taken from the engine rather than from db.session. Two consequences, both
intended:

  1. A limiter decision cannot commit a caller's half-finished business
     transaction, and a caller's rollback cannot un-spend budget that was
     legitimately consumed.
  2. The aggregate and per-purpose counters are consumed together in ONE
     transaction, so a request that exceeds EITHER budget rolls back BOTH. A
     denied request consumes nothing -- otherwise a caller who is already over
     its per-purpose budget would keep burning the aggregate ceiling for the
     other purposes, turning a narrow limit into a denial of service against
     the same user's other flows.

FAIL CLOSED
-----------
If the database is unreachable, or the atomic statement fails, or anything
else raises, the answer is DENY. The alternative -- degrading to "allow" when
the limiter is broken -- means an outage of the counter store is also an
outage of the control, which is exactly when it would be most needed. This is
the opposite of log_audit()'s never-raise contract, deliberately: an audit
failure must not break the business action it merely records, while a limiter
failure IS the business decision.

PRIVACY
-------
`subject` is a phone number or an IP. It is never logged unmasked; see
phone_service.mask_destination. Rows are swept after RETENTION_DAYS.
"""
import logging
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import text

logger = logging.getLogger(__name__)

#: App reference for the retention thread, set by init_rate_limit_retention().
_app = None

#: How often the retention thread wakes. Hourly against a SEVEN-DAY window --
#: deliberately far more often than necessary, because each pass is a single
#: indexed DELETE and the cost of being late is PII sitting longer than the
#: approved policy allows.
RETENTION_INTERVAL_SECONDS = 3600


# ── Approved policy (RC2.5.17 Gate B). Values here are the whole policy; ──
# ── there is no per-tenant override and no environment variable, because ──
# ── a limit that can be raised by configuration is a limit that will be.  ──

#: Challenge CREATION, all purposes together, per destination per hour.
#: Each unit is a real message to a real handset with a real cost.
CREATE_AGGREGATE_PER_HOUR = 3

#: Verification ATTEMPTS, all purposes together, per destination per hour.
#: Higher than creation because a legitimate user mistypes; still a hard
#: ceiling, and OtpChallenge.max_attempts (5) binds first within one challenge.
VERIFY_AGGREGATE_PER_HOUR = 10

#: Per-purpose budgets. Every value MUST be <= the aggregate for the operation
#: -- the aggregate is the hard ceiling and a per-purpose number above it
#: would be unreachable, i.e. a limit that reads as active but never fires.
#: Asserted at import rather than trusted to review.
PER_PURPOSE_PER_HOUR = {
    'tenant_signup': 5,
    'phone_login': 10,
    'phone_change': 5,
    'sensitive_action': 5,
}

#: Minimum gap between two challenge creations for one destination. Distinct
#: from the hourly budget: 3/hour permits three sends spaced a second apart,
#: which is what an impatient user's repeated "Resend" click produces and what
#: a naive loop produces. The cooldown shapes the burst; the budget caps the
#: total.
RESEND_COOLDOWN_SECONDS = 60

#: How long a counter row lives. Short because `subject` is PII, and long
#: enough that an operator can still answer "was this number throttled?" the
#: next working day.
RETENTION_DAYS = 7

WINDOW_SECONDS = 3600

# Scope names. The aggregate scopes are bare; per-purpose scopes are suffixed,
# so one subject's buckets are self-describing in a SELECT.
SCOPE_CREATE = 'otp_create'
SCOPE_VERIFY = 'otp_verify'

# Decision reasons. Returned, never raised: a denial is an ordinary outcome.
ALLOWED = 'allowed'
DENIED_AGGREGATE = 'denied_aggregate'
DENIED_PER_PURPOSE = 'denied_per_purpose'
DENIED_COOLDOWN = 'denied_cooldown'
DENIED_UNAVAILABLE = 'denied_limiter_unavailable'
DENIED_MALFORMED = 'denied_malformed_input'

for _p, _v in PER_PURPOSE_PER_HOUR.items():
    assert _v <= VERIFY_AGGREGATE_PER_HOUR, (
        f"per-purpose budget {_p}={_v} exceeds the aggregate ceiling "
        f"{VERIFY_AGGREGATE_PER_HOUR}; it could never fire"
    )
del _p, _v


class Decision(object):
    """The outcome of one limiter call. Truthy iff allowed."""

    __slots__ = ('allowed', 'reason', 'scope', 'count', 'limit', 'retry_after')

    def __init__(self, allowed, reason, scope=None, count=0, limit=0,
                 retry_after=0):
        self.allowed = allowed
        self.reason = reason
        self.scope = scope
        self.count = count
        self.limit = limit
        self.retry_after = retry_after

    def __bool__(self):
        return bool(self.allowed)

    def __repr__(self):
        # No `subject`: it is PII and a repr reaches tracebacks and Sentry.
        return (f"<Decision {self.reason} scope={self.scope} "
                f"{self.count}/{self.limit}>")


def _utcnow():
    """Naive UTC, matching every DateTime column in models.py.

    Not datetime.now(timezone.utc): mixing an aware value into a comparison
    with a naive column raises on PostgreSQL and silently compares wrong
    elsewhere. The convention is application-side naive UTC (50 uses in
    models.py) and this phase does not change it.
    """
    return datetime.utcnow()


def window_start_for(now=None):
    """The fixed hour bucket containing `now`, truncated in Python.

    Deliberately NOT date_trunc() in SQL: the value is also compared against
    the PREVIOUS bucket below, and computing it in one place means the two
    cannot disagree. Truncation is exact and has no dialect dependency.
    """
    now = now or _utcnow()
    return now.replace(minute=0, second=0, microsecond=0)


# THE atomic statement. One round trip; no read-then-write anywhere.
#
# ON CONFLICT DO UPDATE is what makes concurrent increments correct. RC2.5.16
# Gate B.1 proved on this database that the read-then-write shape fails under
# READ COMMITTED: a concurrent INSERT is invisible to the other transaction
# until it commits, so both see "no bucket", both insert, and the unique
# constraint is what turns that into one row rather than two half-counts.
#
# RETURNING count gives the POST-increment value, so the caller never has to
# guess whether its own unit is included. `count > limit` is therefore the
# correct test, not `>=`.
#
# The same text runs on PostgreSQL and on SQLite (>= 3.35 for RETURNING;
# the bundled library is 3.45), so the SQLite suite exercises the real
# statement rather than a stand-in that could drift from it.
_BUMP_SQL = text("""
    INSERT INTO rate_limit_counters
        (scope, subject, window_start, count, created_at, updated_at)
    VALUES (:scope, :subject, :ws, 1, :now, :now)
    ON CONFLICT (scope, subject, window_start) DO UPDATE
       SET count = rate_limit_counters.count + 1,
           updated_at = :now
    RETURNING count
""")

# Same statement, additionally refusing to increment while the cooldown is
# unexpired. The guard lives in the WHERE of the DO UPDATE so it is evaluated
# by the database against the row it is about to modify -- not read first and
# checked in Python, which two concurrent callers would both pass.
#
# A conflicting row that fails the guard yields NO row from RETURNING, which
# is how a cooldown violation is distinguished from an increment.
_BUMP_COOLDOWN_SQL = text("""
    INSERT INTO rate_limit_counters
        (scope, subject, window_start, count, created_at, updated_at)
    VALUES (:scope, :subject, :ws, 1, :now, :now)
    ON CONFLICT (scope, subject, window_start) DO UPDATE
       SET count = rate_limit_counters.count + 1,
           updated_at = :now
     WHERE rate_limit_counters.updated_at <= :cutoff
    RETURNING count
""")

# The cooldown guard above can only see the CURRENT bucket. Within the first
# RESEND_COOLDOWN_SECONDS of an hour the preceding event may live in the
# PREVIOUS bucket, and an unguarded INSERT into the fresh bucket would hand
# out a free send exactly on the hour -- a bypass with a published schedule.
#
# Reading the previous bucket is race-free precisely because it is the
# previous one: window_start is derived from the clock, so once `now` has
# crossed the boundary no transaction can still be writing there.
_PREV_UPDATED_SQL = text("""
    SELECT updated_at FROM rate_limit_counters
     WHERE scope = :scope AND subject = :subject AND window_start = :ws
""")


def _bump(conn, scope, subject, ws, now, limit, cutoff=None):
    """Consume one unit of `scope`. Returns (ok, post_increment_count).

    ok is False either because the budget is now exceeded or because the
    cooldown guard refused the update. The caller distinguishes them: a
    refused update returns count 0, an exceeded budget returns a real count.
    """
    params = {'scope': scope, 'subject': subject, 'ws': ws, 'now': now}
    if cutoff is None:
        row = conn.execute(_BUMP_SQL, params).fetchone()
    else:
        params['cutoff'] = cutoff
        row = conn.execute(_BUMP_COOLDOWN_SQL, params).fetchone()
    if row is None:
        return False, 0
    count = int(row[0])
    return count <= limit, count


def _check(operation, destination, purpose, now=None, enforce_cooldown=False):
    """Consume aggregate + per-purpose budget atomically. Never raises.

    Both counters are consumed inside one transaction and the transaction is
    rolled back unless BOTH fit, so a denied request consumes neither.
    """
    from app.extensions import db

    subject = str(destination or '').strip()
    if not subject or not purpose or purpose not in PER_PURPOSE_PER_HOUR:
        # Unknown purpose is a DENIAL, not an unmetered pass. A limiter that
        # waves through anything it does not recognise is bypassed by a typo.
        return Decision(False, DENIED_MALFORMED, scope=operation)

    if operation == SCOPE_CREATE:
        aggregate_limit = CREATE_AGGREGATE_PER_HOUR
    else:
        aggregate_limit = VERIFY_AGGREGATE_PER_HOUR
    purpose_limit = min(PER_PURPOSE_PER_HOUR[purpose], aggregate_limit)
    purpose_scope = f"{operation}:{purpose}"

    now = now or _utcnow()
    ws = window_start_for(now)
    retry_after = int((ws + timedelta(seconds=WINDOW_SECONDS) - now)
                      .total_seconds())

    try:
        with db.engine.begin() as conn:
            if enforce_cooldown:
                cutoff = now - timedelta(seconds=RESEND_COOLDOWN_SECONDS)
                prev = conn.execute(_PREV_UPDATED_SQL, {
                    'scope': operation, 'subject': subject,
                    'ws': ws - timedelta(seconds=WINDOW_SECONDS),
                }).fetchone()
                if prev is not None and prev[0] is not None:
                    prev_at = prev[0]
                    if isinstance(prev_at, str):          # SQLite text storage
                        prev_at = datetime.fromisoformat(prev_at)
                    if prev_at > cutoff:
                        raise _Denied(DENIED_COOLDOWN, operation, 0,
                                      aggregate_limit,
                                      int((prev_at - cutoff).total_seconds()))
            else:
                cutoff = None

            ok, count = _bump(conn, operation, subject, ws, now,
                              aggregate_limit, cutoff)
            if not ok and count == 0:
                raise _Denied(DENIED_COOLDOWN, operation, 0, aggregate_limit,
                              RESEND_COOLDOWN_SECONDS)
            if not ok:
                raise _Denied(DENIED_AGGREGATE, operation, count,
                              aggregate_limit, retry_after)

            p_ok, p_count = _bump(conn, purpose_scope, subject, ws, now,
                                  purpose_limit)
            if not p_ok:
                raise _Denied(DENIED_PER_PURPOSE, purpose_scope, p_count,
                              purpose_limit, retry_after)

            return Decision(True, ALLOWED, scope=operation, count=count,
                            limit=aggregate_limit, retry_after=0)
    except _Denied as d:
        # Raised to force `with db.engine.begin()` to ROLL BACK, which is what
        # makes "a denied request consumes neither counter" true rather than
        # merely intended. The exception never escapes this function.
        return Decision(False, d.reason, scope=d.scope, count=d.count,
                        limit=d.limit, retry_after=d.retry_after)
    except Exception as exc:                                    # noqa: BLE001
        from app.services.phone_service import mask_destination
        # logger.error, NOT logger.exception, and the exception CLASS only.
        #
        # A traceback here would defeat the masking on the same line: a
        # SQLAlchemy DBAPIError renders the failing statement together with
        # its BOUND PARAMETERS, and `subject` is one of them -- so
        # logger.exception publishes the very phone number the format string
        # carefully masks. RC2.5.15 disclosed a customer number reaching
        # production logs through exactly this kind of incidental path, and a
        # limiter that fails closed will log on every request during an
        # outage, i.e. at maximum volume.
        #
        # The class name is enough to tell an operator whether this is a
        # connection failure, a missing table or a constraint problem, which
        # is the whole operational question at this point.
        logger.error("[ratelimit] FAIL CLOSED %s purpose=%s subject=%s (%s)",
                     operation, purpose, mask_destination(subject),
                     type(exc).__name__)
        return Decision(False, DENIED_UNAVAILABLE, scope=operation,
                        retry_after=WINDOW_SECONDS)


class _Denied(Exception):
    """Internal control flow only. Never propagates out of this module."""

    def __init__(self, reason, scope, count, limit, retry_after):
        super().__init__(reason)
        self.reason = reason
        self.scope = scope
        self.count = count
        self.limit = limit
        self.retry_after = retry_after


def check_create(destination, purpose, now=None):
    """May a challenge be CREATED for this destination and purpose?

    Enforces, together and atomically: the resend cooldown, the aggregate
    creation budget, and the per-purpose creation budget.
    """
    return _check(SCOPE_CREATE, destination, purpose, now=now,
                  enforce_cooldown=True)


def check_verify(destination, purpose, now=None):
    """May a verification ATTEMPT be made for this destination and purpose?

    EVERY attempt is metered, including one that turns out to be correct. A
    limiter that refunds successes lets an attacker interleave a known-good
    code to keep guessing, and makes the ceiling depend on the attacker's own
    success rate. No cooldown: a user correcting a typo should not be made to
    wait a minute.
    """
    return _check(SCOPE_VERIFY, destination, purpose, now=now,
                  enforce_cooldown=False)


def purge_expired(now=None, retention_days=None):
    """Delete counter rows older than the retention window. Returns the count.

    Retention exists because `subject` is a phone number, not because the
    table would otherwise grow inconveniently. Nothing in the limiter's
    correctness depends on this running: a bucket outside the current window
    is never read, so a purge that never runs leaks storage and PII but never
    grants budget. Cleanup must not be the boundary -- the same rule
    otp_service follows for expiry.

    THE CUTOFF CANNOT REACH THE ACTIVE WINDOW
    -----------------------------------------
    cutoff is the CURRENT bucket's start minus RETENTION_DAYS, and the
    comparison is strictly `<`. The current bucket's window_start equals
    window_start_for(now), which is seven days greater than the cutoff, so no
    bucket that any live request could still increment is in range -- not even
    at an hour boundary, because both sides are derived from the same
    truncation. A retention_days of 0 would still spare the current bucket.

    SAFE UNDER MULTIPLE WORKERS
    ---------------------------
    Unlike campaign_worker.claim_next_batch(), this needs no row locking and
    no WEB_CONCURRENCY gate. It claims nothing and hands nothing out: it is a
    single set-based DELETE over a closed range, so two workers running it
    concurrently either delete disjoint halves or one finds the rows already
    gone. The result is identical either way, which also makes it idempotent
    -- a second pass over the same range deletes 0 rows.
    """
    from app.extensions import db

    now = now or _utcnow()
    days = RETENTION_DAYS if retention_days is None else retention_days
    cutoff = window_start_for(now) - timedelta(days=days)
    try:
        with db.engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM rate_limit_counters "
                     "WHERE window_start < :cutoff"),
                {'cutoff': cutoff})
            return result.rowcount or 0
    except Exception as exc:                                    # noqa: BLE001
        # Class only, for the same reason as above: the DELETE's bound
        # parameter is a timestamp, but a future predicate might not be, and
        # the rule is easier to keep than to remember.
        logger.error("[ratelimit] retention purge failed (%s)",
                     type(exc).__name__)
        return 0


# ── Retention worker ────────────────────────────────────────────────────────
#
# Mirrors init_followup_service() and init_campaign_worker() exactly: a module
# -level init taking the app, a daemon thread, and a poll loop that opens its
# own app context. No new scheduler, no new dependency, no Redis, no cron.
#
# WHY THIS NEEDS NO WEB_CONCURRENCY GATE
# --------------------------------------
# campaign_worker refuses to start when WEB_CONCURRENCY > 1, because
# claim_next_batch() takes no row locks and two workers would claim and SEND
# the same recipients. Nothing analogous exists here. This worker claims
# nothing, sends nothing and hands out no budget; it issues one set-based
# DELETE over a closed, already-expired range. Concurrent copies converge on
# the same end state, so running it in every worker process is correct, merely
# redundant. Adding a concurrency gate would be cargo-culting the shape of a
# safeguard whose reason does not apply -- and would silently disable
# retention on any future multi-worker deployment, which is the failure this
# review is closing.


def _retention_loop():
    """Poll loop. Sleeps FIRST, then purges.

    Sleeping first is deliberate. Every test suite in this repository calls
    create_app(), so a purge-then-sleep loop would fire a DELETE against
    whatever database each suite has just built, during collection, before any
    fixture has run. Sleeping first means the first pass happens an hour in,
    which no test reaches -- so this needs no TESTING special-case, and the
    production behaviour is not a different code path from the tested one.

    An hour's delay after boot costs nothing against a seven-day window.
    """
    while True:
        time.sleep(RETENTION_INTERVAL_SECONDS)
        try:
            with _app.app_context():
                deleted = purge_expired()
            if deleted:
                # Count only. Never a subject, never a row -- `subject` is a
                # phone number and this line runs unattended forever.
                logger.info("[ratelimit] retention purged %d expired "
                            "counter rows", deleted)
        except Exception as exc:                                # noqa: BLE001
            # The loop must outlive any single failure. purge_expired()
            # already swallows its own errors and returns 0; this catches the
            # app-context layer too, so a transient failure costs one pass and
            # never the thread. Retention failing must NOT weaken rate
            # limiting, and it cannot: the limiter reads no state this worker
            # writes, and a bucket outside the current window is never read.
            logger.error("[ratelimit] retention loop error (%s)",
                         type(exc).__name__)


def init_rate_limit_retention(app):
    """Start the retention thread. Called once from create_app().

    Never raises: a failure to start retention must not prevent the
    application from booting, and must not disable the limiter, which does not
    depend on this thread in any way.
    """
    global _app
    _app = app
    try:
        threading.Thread(target=_retention_loop, daemon=True,
                         name="rate-limit-retention").start()
        logger.info("✅ Rate-limit retention worker started (%d-day window)",
                    RETENTION_DAYS)
    except Exception as exc:                                    # noqa: BLE001
        logger.error("[ratelimit] retention worker failed to start (%s); "
                     "rate limiting is UNAFFECTED", type(exc).__name__)
