import os

VERIFY_TOKEN         = os.environ.get("VERIFY_TOKEN", "oxford2026")
ACCESS_TOKEN         = os.environ.get("ACCESS_TOKEN", "")
# Phase 14C: Meta App Secret, used to verify the X-Hub-Signature-256 HMAC on
# inbound webhook POSTs. VERIFY_TOKEN guards only the GET subscription
# handshake — it does NOT authenticate delivered messages, so without this the
# webhook accepts any payload from anyone who knows the URL.
#
# Empty = verification DISABLED and the endpoint behaves exactly as before.
# That default is deliberate: it lets this ship with zero production risk and
# be activated by setting the variable. Set it in Railway to close the hole.
META_APP_SECRET      = os.environ.get("META_APP_SECRET", "")
# Phase 14C: billing provider webhook secrets. Empty = verification disabled.
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
STRIPE_WEBHOOK_SECRET   = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
PHONE_NUMBER_ID      = os.environ.get("PHONE_NUMBER_ID", "")
# WhatsApp Business Account id — required to list approved message templates
# (the message_templates edge lives on the WABA node, not the phone number).
WABA_ID              = os.environ.get("WABA_ID", "")
SHEETS_ID            = os.environ.get("SHEETS_ID", "")
# Phase 0 Sprint 2: explicit primary-tenant context. Replaces the
# Tenant.query.first() crutch (_get_default_tenant_id) which resolves to an
# arbitrary tenant in multi-tenant production (proven: 'amboori', not oxford).
PRIMARY_TENANT_ID    = os.environ.get("PRIMARY_TENANT_ID", "")
# Phase 0 Sprint 3: production exception monitoring. Empty = Sentry disabled
# (local dev, CI). Set the DSN in Railway service variables to activate.
SENTRY_DSN           = os.environ.get("SENTRY_DSN", "")
GEMINI_API_KEY       = os.environ.get("GEMINI_API_KEY", "")
# Phase 1.3A-2: Conversation Memory observe mode. When true, MemoryProvider is
# invoked on AI-eligible requests for metrics only — memory is NEVER injected
# into Gemini. Default OFF = zero execution, zero overhead.
MEMORY_OBSERVE_MODE  = os.environ.get("MEMORY_OBSERVE_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}
# Phase 1.3A-3: Activate memory injection into Gemini context. Separate from
# MEMORY_OBSERVE_MODE (metrics-only). When true, conversation history is prepended
# to the Gemini prompt for AI-eligible turns. Default OFF.
MEMORY_ACTIVATE      = os.environ.get("MEMORY_ACTIVATE", "false").strip().lower() in {"1", "true", "yes", "on"}
GEMINI_MODEL         = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
BROADCAST_API_KEY    = os.environ.get("BROADCAST_API_KEY", "oxford_broadcast_2026")
ADMIN_KEY            = os.environ.get("ADMIN_KEY", "oxford_admin_2026")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS", "{}")
SECRET_KEY           = os.environ.get("SECRET_KEY", "oxford-crm-local-dev-key")

# ── Phase RC2.5.16: OTP challenge HMAC key ────────────────────────────────
# Keys the HMAC-SHA256 digest of every OTP challenge. A six-digit code has
# only 10**6 possible values, so an unkeyed digest is exhaustible in
# milliseconds if the database is ever read. The key lives in the environment
# and NEVER in the database, so a leaked table alone yields nothing.
#
# DELIBERATELY ITS OWN SECRET. SECRET_KEY already serves two purposes (Flask
# session signing and both itsdangerous token families via email_service), and
# WABA_ENCRYPTION_KEY is a Fernet encryption key -- a different primitive
# entirely. Reusing either would couple unrelated rotation schedules.
#
# DELIBERATELY NOT VALIDATED AT BOOT, unlike WABA_ENCRYPTION_KEY.
# --------------------------------------------------------------
# Production does not have this variable, and RC2.5.16 Gate B ships NO caller:
# the OTP service exists and nothing invokes it. A boot-time requirement would
# therefore turn a deployment of dormant code into an outage, which is exactly
# the failure mode RC2.5.15 had to sequence around. Validation lives at the
# point of use instead -- otp_service raises if the key is missing or too weak
# -- so the primitive fails loudly the first time it is actually called, and
# the phase that introduces a caller is the phase that must provision the key.
#
# Empty default follows the BREVO_API_KEY precedent: absent means the feature
# is unavailable, not that the process should refuse to start.
#
# Rotating this key invalidates every in-flight challenge. With a five-minute
# lifetime that is a five-minute window of failed verifications, not data loss.
OTP_HMAC_KEY         = os.environ.get("OTP_HMAC_KEY", "")

# Phase 15C.5-B: Email Configuration
EMAIL_PROVIDER       = os.environ.get("EMAIL_PROVIDER", "brevo")
BREVO_API_KEY        = os.environ.get("BREVO_API_KEY", "")
BREVO_SENDER_EMAIL   = os.environ.get("BREVO_SENDER_EMAIL", "noreply@oxfordedu.com")
BREVO_SENDER_NAME    = os.environ.get("BREVO_SENDER_NAME", "Oxford CRM")
APP_URL              = os.environ.get("APP_URL", "http://localhost:5000")
VERIFY_EMAIL_EXPIRY_SECONDS = int(os.environ.get("VERIFY_EMAIL_EXPIRY_SECONDS", "86400"))
EMAIL_TIMEOUT_SECONDS = int(os.environ.get("EMAIL_TIMEOUT_SECONDS", "5"))

# Phase RC2.5.18-A-FIX1: WhatsApp Graph API timeouts, as a (connect, read) pair.
#
# Until RC2.5.18-A none of the seven calls in whatsapp_service passed a timeout,
# so a stalled Meta connection held its thread forever -- and a reply is sent
# INLINE in POST /webhook, on the only sync gunicorn worker.
#
# WHAT THESE VALUES DO AND DO NOT GUARANTEE
# -----------------------------------------
# They are NOT a total wall-clock limit, and nothing here should be read as
# one. requests applies CONNECT to establishing the connection and READ to
# EACH gap between bytes of the response, so a response trickled one byte at
# a time can run far past READ. DNS resolution is not covered at all. An
# earlier version of this setting summed three per-call timeouts and presented
# the sum as a wall-clock bound under gunicorn's worker timeout. That was
# false: it treated a per-phase timeout as a total.
#
# What they DO bound is the common failure: an unreachable Meta fails after
# about CONNECT; a Meta that accepts the request and then goes silent fails
# after about CONNECT + READ.
#
# The POST /webhook request has no enforceable total budget in any case: the
# Gemini call that precedes the reply (ai_service) has no timeout of its own.
#
# WHY THESE NUMBERS
#   CONNECT 3.05 -- the requests documentation's own recommendation: slightly
#                   above a multiple of 3s, the TCP SYN retransmission window,
#                   so one lost SYN does not fail an otherwise healthy connect.
#   READ    8    -- a Graph API send normally answers well inside 2s; 8 leaves
#                   headroom for a slow Meta without holding the worker for
#                   an unreasonable time.
#
# Env-overridable with the default in code, like EMAIL_TIMEOUT_SECONDS, so
# production needs no new Railway variable.
WHATSAPP_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("WHATSAPP_CONNECT_TIMEOUT_SECONDS", "3.05"))
WHATSAPP_READ_TIMEOUT_SECONDS = float(os.environ.get("WHATSAPP_READ_TIMEOUT_SECONDS", "8"))

# Phase RC2.5.5a: WHATSAPP_API_URL removed. It pinned Graph v19.0 while every
# live call in whatsapp_service.py targets v21.0, and nothing ever read it —
# it was imported once and never dereferenced. Dead configuration built on a
# stale API version is a trap for the next contributor, not a fallback.
# Live Graph URLs are constructed per call; there is no shared constant.

# ── PostgreSQL (Railway auto-sets DATABASE_URL) ────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is mandatory for persistent state.")

# SQLAlchemy requires postgresql:// not postgres:// (Railway older format fix)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Phase 10: Authentication
# ADR-023 D5: default flipped to SESSION_ONLY so that a missing variable fails safe.
_VALID_AUTH_MODES = ("SESSION_ONLY", "DUAL", "ADMIN_KEY_ONLY")
AUTH_MODE = os.environ.get('AUTH_MODE', 'SESSION_ONLY')
if AUTH_MODE not in _VALID_AUTH_MODES:
    raise RuntimeError(
        f"AUTH_MODE '{AUTH_MODE}' is not recognised. "
        f"Must be one of: {', '.join(_VALID_AUTH_MODES)}"
    )

DEBUG = os.environ.get("FLASK_ENV") == "development" or os.environ.get("DEBUG") == "1"
if not DEBUG:
    if SECRET_KEY == "oxford-crm-local-dev-key":
        raise RuntimeError("Production secrets missing: SECRET_KEY is using insecure default.")
    if ADMIN_KEY == "oxford_admin_2026":
        raise RuntimeError("Production secrets missing: ADMIN_KEY is using insecure default.")
    if BROADCAST_API_KEY == "oxford_broadcast_2026":
        raise RuntimeError("Production secrets missing: BROADCAST_API_KEY is using insecure default.")
    # ADR-023 D5: refuse production startup under any mode that bypasses session auth.
    if AUTH_MODE in ("ADMIN_KEY_ONLY", "DUAL"):
        raise RuntimeError(
            f"AUTH_MODE='{AUTH_MODE}' is not permitted in production. "
            "Set AUTH_MODE=SESSION_ONLY."
        )
