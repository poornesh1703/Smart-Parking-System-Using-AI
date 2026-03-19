from app import create_app, db
from app.models import User
from werkzeug.security import generate_password_hash

app = create_app()

with app.app_context():
    email = "poornesh1703@gmail.com"
    if not User.query.filter_by(email=email).first():
        admin = User(
            name="Admin",
            email=email,
            password_hash=generate_password_hash("12345678"),
            role="admin",
        )
        db.session.add(admin)
        db.session.commit()
        print("Admin created:", email, "password=12345678")
    else:
        print("Admin already exists")
