from flask import Flask
from app.config import DATABASE_URL, SECRET_KEY, AUTH_MODE
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
