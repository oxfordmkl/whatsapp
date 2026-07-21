import os

VERIFY_TOKEN         = os.environ.get("VERIFY_TOKEN", "oxford2026")
ACCESS_TOKEN         = os.environ.get("ACCESS_TOKEN", "")
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

# Phase 15C.5-B: Email Configuration
EMAIL_PROVIDER       = os.environ.get("EMAIL_PROVIDER", "brevo")
BREVO_API_KEY        = os.environ.get("BREVO_API_KEY", "")
BREVO_SENDER_EMAIL   = os.environ.get("BREVO_SENDER_EMAIL", "noreply@oxfordedu.com")
BREVO_SENDER_NAME    = os.environ.get("BREVO_SENDER_NAME", "Oxford CRM")
APP_URL              = os.environ.get("APP_URL", "http://localhost:5000")
VERIFY_EMAIL_EXPIRY_SECONDS = int(os.environ.get("VERIFY_EMAIL_EXPIRY_SECONDS", "86400"))
EMAIL_TIMEOUT_SECONDS = int(os.environ.get("EMAIL_TIMEOUT_SECONDS", "5"))

WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"

# ── PostgreSQL (Railway auto-sets DATABASE_URL) ────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is mandatory for persistent state.")

# SQLAlchemy requires postgresql:// not postgres:// (Railway older format fix)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Phase 10: Authentication
AUTH_MODE = os.environ.get('AUTH_MODE', 'ADMIN_KEY_ONLY')

DEBUG = os.environ.get("FLASK_ENV") == "development" or os.environ.get("DEBUG") == "1"
if not DEBUG:
    if SECRET_KEY == "oxford-crm-local-dev-key":
        raise RuntimeError("Production secrets missing: SECRET_KEY is using insecure default.")
    if ADMIN_KEY == "oxford_admin_2026":
        raise RuntimeError("Production secrets missing: ADMIN_KEY is using insecure default.")
    if BROADCAST_API_KEY == "oxford_broadcast_2026":
        raise RuntimeError("Production secrets missing: BROADCAST_API_KEY is using insecure default.")
