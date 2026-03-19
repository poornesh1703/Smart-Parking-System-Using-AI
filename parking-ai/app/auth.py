from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
from .models import User
from . import db

auth_bp = Blueprint("auth", __name__)

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        user = User.query.filter_by(email=email).first()
        if not user:
            flash("No account found. Please sign up.", "warning")
            return redirect(url_for("auth.signup", email=email))

        if not check_password_hash(user.password_hash, password):
            flash("Incorrect password.", "danger")
            return redirect(url_for("auth.login"))

        login_user(user)
        if user.role == "admin":
            return redirect(url_for("views.admin_dashboard"))
        return redirect(url_for("views.user_dashboard"))

    return render_template("login.html")

@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    preset_email = request.args.get("email", "")
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        if not name or not email or not password:
            flash("All fields are required.", "warning")
            return redirect(url_for("auth.signup", email=email))

        if User.query.filter_by(email=email).first():
            flash("Email already registered. Please log in.", "info")
            return redirect(url_for("auth.login"))

        user = User(
            name=name,
            email=email,
            password_hash=generate_password_hash(password),
            role="user",
        )
        db.session.add(user)
        db.session.commit()

        flash("Account created! You can log in now.", "success")
        return redirect(url_for("auth.login"))

    return render_template("signup.html", preset_email=preset_email)

@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out successfully.", "success")
    return redirect(url_for("auth.login"))
