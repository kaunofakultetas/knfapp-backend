"""knfapp-backend Flask application factory."""

import os

from flask import Flask
from flask_cors import CORS
from flask_socketio import SocketIO
from werkzeug.middleware.proxy_fix import ProxyFix

from app.database import init_db

# Module-level SocketIO instance so chat routes can import it
socketio = SocketIO()


def create_app():
    app = Flask(__name__)

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["DB_PATH"] = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "knfapp.db"))
    app.config["JWT_SECRET"] = os.environ.get("JWT_SECRET", "jwt-dev-secret-change-me")
    app.config["INVITATION_EXPIRY_HOURS"] = int(os.environ.get("INVITATION_EXPIRY_HOURS", "168"))

    # ProxyFix for running behind Caddy
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    CORS(app, resources={r"/api/*": {"origins": "*"}})

    # Initialize Socket.IO with CORS allowed for all origins (mobile app)
    socketio.init_app(app, cors_allowed_origins="*", async_mode="threading")

    # Ensure data directory exists
    db_dir = os.path.dirname(app.config["DB_PATH"])
    os.makedirs(db_dir, exist_ok=True)

    # Initialize database
    with app.app_context():
        init_db(app.config["DB_PATH"])

    # Register blueprints
    from app.auth.routes import auth_bp
    from app.news.routes import news_bp
    from app.schedule.routes import schedule_bp
    from app.admin.routes import admin_bp
    from app.scraper.routes import scraper_bp
    from app.chat.routes import chat_bp
    from app.social.routes import social_bp

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(news_bp, url_prefix="/api/news")
    app.register_blueprint(schedule_bp, url_prefix="/api/schedule")
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    app.register_blueprint(scraper_bp, url_prefix="/api/scraper")
    app.register_blueprint(chat_bp, url_prefix="/api/chat")
    app.register_blueprint(social_bp, url_prefix="/api/social")

    # Register Socket.IO events for real-time chat
    from app.chat.events import register_socket_events
    register_socket_events(socketio)

    # Start background scraper
    from app.scraper.scheduler import start_scraper_scheduler
    start_scraper_scheduler(app)

    @app.route("/api/health")
    def health():
        return {"status": "ok", "service": "knfapp-backend"}

    # JSON error handlers for API routes (Flask defaults return HTML)
    @app.errorhandler(400)
    def bad_request(e):
        return {"error": "Bad request"}, 400

    @app.errorhandler(404)
    def not_found(e):
        return {"error": "Not found"}, 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return {"error": "Method not allowed"}, 405

    @app.errorhandler(500)
    def internal_error(e):
        return {"error": "Internal server error"}, 500

    return app
