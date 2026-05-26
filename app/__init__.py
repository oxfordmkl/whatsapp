from flask import Flask

def create_app():
    app = Flask(__name__)

    from app.routes.webhook import webhook_bp
    from app.routes.admin import admin_bp
    from app.routes.broadcast import broadcast_bp
    from app.routes.health import health_bp

    app.register_blueprint(webhook_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(broadcast_bp)
    app.register_blueprint(health_bp)

    return app
