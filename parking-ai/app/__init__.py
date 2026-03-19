from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from dotenv import load_dotenv
import os

load_dotenv()

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"  # redirect here if not logged in


def create_app():
    app = Flask(__name__, static_folder="static", template_folder="templates")

    # --- config ---
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    # Example MySQL URL for XAMPP:
    # mysql+pymysql://root:@127.0.0.1/parking_ai

    # --- init extensions ---
    db.init_app(app)
    login_manager.init_app(app)

    # --- blueprints ---
    from .auth import auth_bp
    from .views import views_bp
    from .detect import detect_bp  # import here to avoid circulars

    app.register_blueprint(auth_bp)
    app.register_blueprint(views_bp)
    app.register_blueprint(detect_bp)

    # --- db tables ---
    with app.app_context():
        # import ALL models so SQLAlchemy knows them before create_all()
        from . import models
        db.create_all()

    # --- breadcrumb injector (optional) ---
    try:
        from .breadcrumb import build_breadcrumb
        @app.context_processor
        def inject_breadcrumb():
            return {"build_breadcrumb": build_breadcrumb}
    except ImportError:
        pass

    return app
