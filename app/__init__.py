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

    # ── Register CLI Commands ─────────────────────────────────────────────
    @app.cli.command("seed-superadmin")
    def seed_superadmin():
        """Seed the initial Super Admin account."""
        from werkzeug.security import generate_password_hash
        import uuid
        
        super_admin = models.User.query.filter_by(role="SUPER_ADMIN").first()
        if super_admin:
            print("Super Admin already exists.")
            return

        print("Creating default Super Admin...")
        user = models.User(
            username="superadmin",
            email="super@admin.com",
            password_hash=generate_password_hash("supersecret"),
            role="SUPER_ADMIN",
            tenant_id=None,
            is_active=True
        )
        db.session.add(user)
        db.session.commit()
        print("Super Admin created: Email=super@admin.com, Password=supersecret")

    # ── Register Flask blueprints ─────────────────────────────────────────
    from app.routes.webhook import webhook_bp
    from app.routes.admin import admin_bp
    from app.routes.broadcast import broadcast_bp
    from app.routes.health import health_bp
    from app.routes.public import public_bp
    from app.routes.tenant import tenant_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(broadcast_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(tenant_bp)

    # ── Start follow-up scheduler (needs app ref for DB context) ──────────
    from app.services.followup_service import init_followup_service
    init_followup_service(app)

    return app
