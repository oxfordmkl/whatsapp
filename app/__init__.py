from flask import Flask
import logging

from flask_wtf.csrf import CSRFProtect

from app.config import (
    DATABASE_URL, SECRET_KEY, AUTH_MODE,
    EMAIL_PROVIDER, BREVO_API_KEY, BREVO_SENDER_EMAIL,
    BREVO_SENDER_NAME, APP_URL, EMAIL_TIMEOUT_SECONDS,
    VERIFY_EMAIL_EXPIRY_SECONDS, PRIMARY_TENANT_ID, SENTRY_DSN
)

# ── Phase 0 Sprint 3: structured logging + Sentry ──────────────────────────
# One consistent line format for everything that goes through logging.
# print() in legacy paths still reaches stdout; critical paths now use loggers.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# ── Phase RC2.5.4c-x-6f2b-B1b: CSRF foundation (factory + exemptions) ───────
#
# WHY
# ---
# The platform had no CSRF mechanism at all: Flask-WTF was not installed and
# CSRFProtect was never initialised, so every authenticated browser POST --
# lead edits, staff management, tenant configuration -- rested on SameSite=Lax
# alone. The x-6f2b-A audit named CSRF a HARD prerequisite for the future
# staff payment-verification POST, which confirms money and triggers an
# irreversible customer message.
#
# WHAT THIS IS NOT
# ----------------
# CSRF answers exactly one question: "did this state-changing request
# originate from a page served to this browser session?" It does NOT answer
# "is this user allowed to do this" (role decorators) or "does this row belong
# to this tenant" (tenant_query / tenant_filter / _actor_tenant_id). Those
# checks are untouched by this phase and remain mandatory.
#
# ONE EXTENSION INSTANCE, INITIALISED PER APP
# --------------------------------------------
# The module-level singleton is the extension object, not an app -- the same
# pattern as db/migrate in app.extensions. init_app() is called inside
# create_app(), so no app is created at import time.
csrf = CSRFProtect()

# The ONLY endpoints exempt from CSRF. Endpoint names, not path prefixes and
# not blueprints: a blueprint-wide exemption would silently unprotect every
# session-authenticated CRM route sharing that blueprint.
#
# Each entry is exempt because it is NOT a browser-session request and already
# carries a STRONGER, independent authenticity mechanism:
#
#   webhook.receive_message   Meta HMAC-SHA256 over the raw body, fail-closed
#                             when META_APP_SECRET is unset (webhook.py).
#   billing.razorpay_webhook  provider HMAC (X-Razorpay-Signature).
#   billing.stripe_webhook    provider HMAC (Stripe-Signature).
#   broadcast.*               X-API-Key header (BROADCAST_API_KEY).
#
# Phase RC2.5.12 removed a seventh entry, admin.trigger_followup: that route was
# retired outright, so the exemption had nothing left to exempt. The exemption
# contract below fails at boot on a missing target, which is exactly why the
# entry had to go with the route.
#
# A custom request header cannot be set cross-origin without a CORS preflight,
# and none of these carries a session cookie, so CSRF adds nothing to them --
# while applying it would break inbound WhatsApp entirely, since Meta cannot
# send a token.
#
# NOTE: exempting the two billing webhooks is only safe while their handlers
# are inert stubs; their signature check fails OPEN when the secret is unset
# (a pre-existing finding recorded by x-6f2, NOT addressed here).
_CSRF_EXEMPT_ENDPOINTS = (
    "webhook.receive_message",      # POST /webhook
    "billing.razorpay_webhook",     # POST /webhooks/razorpay
    "billing.stripe_webhook",       # POST /webhooks/stripe
    "broadcast.broadcast",          # POST /broadcast
    "broadcast.broadcast_template",  # POST /broadcast-template
    "broadcast.upload_media_route",  # POST /upload-media
)

def _check_default_secrets(app):
    """Phase 14C: log a loud warning for every secret left on the fallback
    value committed to app/config.py.

    Those fallbacks are public — anyone who can read the repository knows them.
    A single line naming the variable is enough to act on, and the value itself
    is never logged.
    """
    from app.config import (VERIFY_TOKEN as _vt, ADMIN_KEY as _ak,
                            BROADCAST_API_KEY as _bk, SECRET_KEY as _sk)
    committed = {
        "SECRET_KEY":        (_sk, "oxford-crm-local-dev-key"),
        "ADMIN_KEY":         (_ak, "oxford_admin_2026"),
        "BROADCAST_API_KEY": (_bk, "oxford_broadcast_2026"),
        "VERIFY_TOKEN":      (_vt, "oxford2026"),
    }
    log = logging.getLogger(__name__)
    for name, (actual, default) in committed.items():
        if actual == default:
            log.warning(
                "⚠️ SECURITY: %s is using the DEFAULT value committed to "
                "app/config.py. It is public. Set a unique value in the "
                "environment.", name)


if SENTRY_DSN:
    import sentry_sdk
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        send_default_pii=False,   # never ship lead PII to a third party
        traces_sample_rate=0.0,   # errors only — no performance tracing
    )
    logging.getLogger(__name__).info("Sentry error monitoring initialised")
from app.extensions import db, migrate
from pathlib import Path

def create_app():
    

    BASE_DIR = Path(__file__).resolve().parent.parent

    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "templates")
)

    # ── Session / flash support ──────────────────────────────────────────
    app.config["SECRET_KEY"] = SECRET_KEY
    app.config["AUTH_MODE"] = AUTH_MODE
    logging.getLogger(__name__).info("AUTH_MODE resolved: %s", AUTH_MODE)

    # ── Phase 14C: warn when a secret is still its committed default ──────
    # app/config.py supplies fallback values so local dev works without a .env.
    # Those fallbacks are IN THE REPOSITORY and therefore public. If one reaches
    # production the "secret" is known to anyone who can read the source —
    # catastrophic for SECRET_KEY, which signs session cookies.
    #
    # Deliberately a warning, not a hard failure: refusing to boot could take
    # production down on deploy, which is a worse outcome than a loud log line
    # for a risk that is not new. Alert on this message.
    _check_default_secrets(app)

    # ── Phase 8.2E.5: Cookie security hardening (ADR-023 D2 minimum bar) ──
    # HTTPONLY prevents JS from reading the session cookie.
    # SAMESITE=Lax blocks cross-site POST forgery for the common case and is
    # now asserted explicitly rather than relying on the browser default.
    # SECURE is True only outside DEBUG so local HTTP dev sessions still work.
    from app.config import DEBUG as _DEBUG
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"]   = not _DEBUG
    app.config["EMAIL_PROVIDER"] = EMAIL_PROVIDER
    app.config["BREVO_API_KEY"] = BREVO_API_KEY
    app.config["BREVO_SENDER_EMAIL"] = BREVO_SENDER_EMAIL
    app.config["BREVO_SENDER_NAME"] = BREVO_SENDER_NAME
    app.config["APP_URL"] = APP_URL
    app.config["EMAIL_TIMEOUT_SECONDS"] = EMAIL_TIMEOUT_SECONDS
    app.config["VERIFY_EMAIL_EXPIRY_SECONDS"] = VERIFY_EMAIL_EXPIRY_SECONDS
    # Phase 0 Sprint 2: explicit primary-tenant context. webhook.py's grace-path
    # already reads this key; it was never loaded into config until now.
    app.config["PRIMARY_TENANT_ID"] = PRIMARY_TENANT_ID

    # ── SQLAlchemy / PostgreSQL config ────────────────────────────────────
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,       # Detect stale connections
        "pool_recycle":  1800,       # Recycle connections every 30 min
    }

    # ── Phase 13-B4B2: WABA Encryption Setup ─────────────────────────────
    import os
    app.config["WABA_ENCRYPTION_KEY"] = os.environ.get("WABA_ENCRYPTION_KEY", "")
    
    # Fail-fast validation at boot
    #
    # Phase RC2.5.8 (P2-2): this guard ran ONLY when the key was empty, so an
    # empty key failed at boot while a MALFORMED one booted happily and failed
    # later, mid-request, at the first encrypt/decrypt -- the worst moment to
    # discover it. The key is now validated whatever it holds; the message and
    # the exception type are unchanged, and no key is ever generated,
    # defaulted or logged here.
    try:
        from cryptography.fernet import Fernet
        Fernet(app.config["WABA_ENCRYPTION_KEY"].encode('utf-8'))
    except ValueError as e:
        raise RuntimeError(f"CRITICAL: WABA_ENCRYPTION_KEY is missing or invalid. It must be a 32-byte base64 URL-safe string. Details: {e}")
    except Exception as e:
        raise RuntimeError(f"CRITICAL: Failed to initialize WABA encryption: {e}")

    from app.config import DEBUG, ADMIN_KEY
    if not ADMIN_KEY:
        raise RuntimeError("CRITICAL: ADMIN_KEY is not set.")

    # Phase 15C.5-B: Initialize Email Service
    from app.services.email_service import email_service
    email_service.init_app(app)

    # ── Initialise extensions ─────────────────────────────────────────────
    db.init_app(app)
    migrate.init_app(app, db)
    
    from flask_login import LoginManager
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = 'admin.crm_login'
    login_manager.login_message = "Please log in to access the CRM."

    # ── Import models so SQLAlchemy registers them with Alembic ───────────
    from app import models  # noqa: F401
    
    @login_manager.user_loader
    def load_user(user_id):
        return models.User.query.get(int(user_id))

    # ── Register CLI Commands ─────────────────────────────────────────────
    import click as _click

    @app.cli.command("provision-tenants")
    @_click.option("--live", is_flag=True, default=False,
                   help="Apply changes. Without this the command only reports.")
    def provision_tenants(live):
        """Phase 14D: provision existing tenants (pipeline, stages, settings).

        DRY RUN BY DEFAULT — pass --live to write. Idempotent: a tenant that is
        already provisioned is reported as reused and left untouched. Existing
        tenant data is never modified; only missing resources are added.
        """
        import click
        from app.services.tenant_provisioning_service import backfill_all_tenants

        mode = "LIVE" if live else "DRY RUN"
        click.echo(f"Tenant provisioning backfill — {mode}")

        reports = backfill_all_tenants(dry_run=not live)

        changed = 0
        for r in reports:
            d = r.as_dict()
            if r.errors:
                click.echo(f"  ERROR  {r.tenant_id}: {r.errors}")
                continue
            if r.changed:
                changed += 1
                click.echo(
                    f"  {'PROVISIONED' if live else 'WOULD PROVISION'} "
                    f"{r.tenant_id}: pipeline+{d['pipeline_created']} "
                    f"stages+{d['stages_created']} settings+{d['settings_created']}")
            else:
                click.echo(f"  ok       {r.tenant_id} (already provisioned)")

        click.echo(f"\n{len(reports)} tenants inspected, {changed} "
                   f"{'provisioned' if live else 'need provisioning'}, "
                   f"{sum(1 for r in reports if r.errors)} errors")
        if not live and changed:
            click.echo("Re-run with --live to apply.")

    @app.cli.command("backfill-staff-identity")
    @_click.option("--live", is_flag=True, default=False,
                   help="Apply changes. Without this the command only reports.")
    def backfill_staff_identity(live):
        """Phase RC2.3C: populate assigned_user_id from assigned_staff.

        DRY RUN BY DEFAULT — pass --live to write. Writes ONLY
        ConversationState.assigned_user_id and Task.assigned_user_id; the
        legacy assigned_staff strings are never modified, so rollback is
        clearing the FK and nothing else.

        Idempotent: an already-populated row is reported and skipped, so a
        second --live run performs zero writes. Unresolvable values are
        reported and skipped, never guessed, and never fail their tenant.
        """
        import click
        from app.services.staff_backfill_service import backfill_all_tenants

        mode = "LIVE" if live else "DRY RUN"
        click.echo(f"Staff identity backfill — {mode}")
        click.echo("  writes: conversation_state.assigned_user_id, "
                   "tasks.assigned_user_id  (assigned_staff untouched)\n")

        reports = backfill_all_tenants(dry_run=not live)

        t_res = t_skip = t_already = 0
        for r in reports:
            if r.errors:
                click.echo(f"Tenant {r.tenant_id}")
                click.echo(f"  ERROR: {r.errors}")
                continue
            if not (r.resolved or r.skipped or r.already):
                continue                      # nothing to say about this tenant

            click.echo(f"Tenant {r.tenant_id}")
            for table in r.TABLES:
                c = r.counts[table]
                if not (c["resolved"] or c["skipped"] or c["already"]):
                    continue
                label = ("Resolved" if live else "Would resolve")
                click.echo(f"  {table}")
                click.echo(f"    {label:18} {c['resolved']}")
                click.echo(f"    {'Skipped':18} {c['skipped']}")
                click.echo(f"    {'Already populated':18} {c['already']}")
            for table, row_id, value, reason in r.skipped_rows:
                click.echo(f"    SKIP {table} id={row_id} "
                           f"value={value!r} — {reason}")
            click.echo("")
            t_res += r.resolved
            t_skip += r.skipped
            t_already += r.already

        click.echo("TOTAL")
        click.echo(f"  {'resolved' if live else 'would resolve':18} {t_res}")
        click.echo(f"  {'skipped':18} {t_skip}")
        click.echo(f"  {'already populated':18} {t_already}")
        errs = sum(1 for r in reports if r.errors)
        click.echo(f"  {'tenants inspected':18} {len(reports)}")
        click.echo(f"  {'tenant errors':18} {errs}")
        if not live and t_res:
            click.echo("\nRe-run with --live to apply.")

    @app.cli.command("seed-superadmin")
    def seed_superadmin():
        """Seed the initial Super Admin account securely."""
        import click
        from werkzeug.security import generate_password_hash
        import sqlalchemy.exc
        
        super_admin = models.User.query.filter_by(role="SUPER_ADMIN").first()
        if super_admin:
            click.echo("A Super Admin account already exists. No changes were made.")
            return

        email = click.prompt("Email", type=str).strip().lower()
        if not email:
            click.echo("Email cannot be empty. Aborting.")
            return

        if models.User.query.filter_by(email=email).first():
            click.echo("Email is already in use. Aborting.")
            return

        username = click.prompt("Username", type=str).strip()
        if not username:
            click.echo("Username cannot be empty. Aborting.")
            return

        if models.User.query.filter_by(username=username, tenant_id=None).first():
            click.echo("Username is already in use by another platform account. Aborting.")
            return

        password = click.prompt("Password", type=str, hide_input=True, confirmation_prompt=True)
        if len(password) < 8:
            click.echo("Password must be at least 8 characters long. Aborting.")
            return

        try:
            user = models.User(
                username=username,
                email=email,
                password_hash=generate_password_hash(password),
                role="SUPER_ADMIN",
                tenant_id=None,
                is_active=True
            )
            db.session.add(user)
            db.session.commit()
            click.echo("Super Admin account created successfully.")
        except sqlalchemy.exc.SQLAlchemyError:
            db.session.rollback()
            click.echo("An error occurred while creating the Super Admin. No changes were made.")

    # ── Register Flask blueprints ─────────────────────────────────────────
    from app.routes.webhook import webhook_bp
    from app.routes.admin import admin_bp
    from app.routes.broadcast import broadcast_bp
    from app.routes.health import health_bp
    from app.routes.public import public_bp
    from app.routes.tenant import tenant_bp
    from app.routes.billing import billing_bp
    from app.routes.marketing import marketing_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(broadcast_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(tenant_bp)
    app.register_blueprint(billing_bp)
    app.register_blueprint(marketing_bp)

    # ── Phase RC2.5.4c-x-6f2b-B1b: CSRF enforcement ───────────────────────
    #
    # Initialised AFTER the blueprints so every exempt endpoint already exists
    # in app.view_functions and can be resolved by name.
    #
    # Enforcement is CSRFProtect's own before_request hook, so a bad or missing
    # token is rejected BEFORE any view body runs -- no business mutation can
    # precede the check. No route performs its own CSRF check.
    #
    # WTF_CSRF_TIME_LIMIT = None makes the token SESSION-scoped rather than
    # expiring after Flask-WTF's default hour: the CRM is a long-lived,
    # multi-tab surface, and a token that dies while an operator is still
    # logged in produces mysterious failures on a form that looks fine. The
    # token still dies with the session -- logout calls session.clear().
    app.config.setdefault("WTF_CSRF_TIME_LIMIT", None)
    csrf.init_app(app)

    for _endpoint in _CSRF_EXEMPT_ENDPOINTS:
        _view = app.view_functions.get(_endpoint)
        if _view is None:
            # A renamed or removed endpoint must not degrade into "silently not
            # exempt": the Meta webhook would start rejecting Meta's own POSTs.
            # Fail at boot, loudly, rather than at 3am on an inbound message.
            raise RuntimeError(
                f"CSRF exemption target {_endpoint!r} is not a registered "
                "endpoint -- refusing to start with an unenforceable "
                "exemption contract."
            )
        csrf.exempt(_view)

    # ── Phase 1.5.5D: State Engine UnitOfWork teardown safety net ─────────
    # Gated by STATE_UOW_CONTEXT (default OFF → no-op). The webhook's
    # `with state_unit_of_work()` already commits/rolls back and resets its own
    # token; this net only fires if a unit leaked past an abnormal exit, to
    # avoid carrying an open unit into the next request on the same worker.
    @app.teardown_request
    def _state_uow_safety_net(exc):
        from app.flags import state_uow_context_enabled
        if not state_uow_context_enabled():
            return
        from app.persistence.unit_of_work import (
            current_unit_of_work, reset_active_unit_of_work,
        )
        uow = current_unit_of_work()
        if uow is None:
            return
        logging.getLogger(__name__).warning(
            "[state-uow] leaked unit of work at teardown — cleaning up (exc=%s)", exc
        )
        try:
            if exc is None:
                uow.commit()
            else:
                uow.rollback()
        finally:
            reset_active_unit_of_work()

    # ── Start follow-up scheduler (needs app ref for DB context) ──────────
    from app.services.followup_service import init_followup_service
    init_followup_service(app)

    # ── Phase RC2.5.10 (P2-1, R1-A): CRM Sheets writer needs the same app ref
    # Its two writers run in bare threads with no context of their own, and
    # they must resolve each tenant's spreadsheet from TenantSettings before
    # writing. Without this the module's _app stays None and every write is
    # refused -- a CRM that writes nowhere, never one that writes to the wrong
    # tenant. Starts no thread; captures the app object only.
    from app.services.crm_service import init_crm_service
    init_crm_service(app)

    # ── Phase 8.2C.3: Campaign worker (CAMPAIGN_ENGINE_V2 gated, default OFF) ─
    # Mirrors the FollowUpJob startup pattern exactly. The worker thread is a
    # daemon and will not prevent process exit. With the flag OFF (production
    # today) this block is never entered and the worker is never started.
    from app.flags import campaign_engine_v2_enabled
    if campaign_engine_v2_enabled():
        from app.marketing.campaign_worker import init_campaign_worker
        init_campaign_worker(app)

    return app
