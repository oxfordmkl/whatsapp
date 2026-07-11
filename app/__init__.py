from flask import Flask
from app.config import (
    DATABASE_URL, SECRET_KEY, AUTH_MODE,
    EMAIL_PROVIDER, BREVO_API_KEY, BREVO_SENDER_EMAIL,
    BREVO_SENDER_NAME, APP_URL, EMAIL_TIMEOUT_SECONDS,
    VERIFY_EMAIL_EXPIRY_SECONDS
)
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
    app.config["EMAIL_PROVIDER"] = EMAIL_PROVIDER
    app.config["BREVO_API_KEY"] = BREVO_API_KEY
    app.config["BREVO_SENDER_EMAIL"] = BREVO_SENDER_EMAIL
    app.config["BREVO_SENDER_NAME"] = BREVO_SENDER_NAME
    app.config["APP_URL"] = APP_URL
    app.config["EMAIL_TIMEOUT_SECONDS"] = EMAIL_TIMEOUT_SECONDS
    app.config["VERIFY_EMAIL_EXPIRY_SECONDS"] = VERIFY_EMAIL_EXPIRY_SECONDS

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
    if not app.config["WABA_ENCRYPTION_KEY"]:
        # Only log a warning here if you don't want to break local dev without WABA yet.
        # But instructions say "Fail-fast behavior" and "Startup validation".
        # Let's import the service which handles validation in _get_cipher but wait, we want to validate on startup.
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

    app.register_blueprint(public_bp)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(broadcast_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(tenant_bp)
    app.register_blueprint(billing_bp)

    # ── Start follow-up scheduler (needs app ref for DB context) ──────────
    from app.services.followup_service import init_followup_service
    init_followup_service(app)

    return app
