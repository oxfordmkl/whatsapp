from flask import Flask
from app.config import DATABASE_URL
from app.extensions import db, migrate


def create_app():
    from pathlib import Path

    BASE_DIR = Path(__file__).resolve().parent.parent

    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "templates")
)

    # ── SQLAlchemy / PostgreSQL config ────────────────────────────────────
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,       # Detect stale connections
        "pool_recycle":  1800,       # Recycle connections every 30 min
    }

    # ── Initialise extensions ─────────────────────────────────────────────
    db.init_app(app)
    migrate.init_app(app, db)

    # ── Import models so SQLAlchemy registers them with Alembic ───────────
    from app import models  # noqa: F401

    # ── Register Flask blueprints ─────────────────────────────────────────
    from app.routes.webhook import webhook_bp
    from app.routes.admin import admin_bp
    from app.routes.broadcast import broadcast_bp
    from app.routes.health import health_bp

    app.register_blueprint(webhook_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(broadcast_bp)
    app.register_blueprint(health_bp)

    # ── Start follow-up scheduler (needs app ref for DB context) ──────────
    from app.services.followup_service import init_followup_service
    init_followup_service(app)

    return app
