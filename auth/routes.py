"""Authentication routes: email + password login/register."""
import logging
import re
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, redirect, render_template
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

log = logging.getLogger("bot.auth")

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

_config = None


def init_auth(app, config):
    """Initialize auth blueprint with app context."""
    global _config
    _config = config
    app.register_blueprint(auth_bp)


@auth_bp.route("/register", methods=["POST"])
def register():
    """Create a new account with email + password."""
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    terms_accepted = bool(data.get("terms_accepted"))

    if not email or not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
        return jsonify({"ok": False, "msg": "Email invalido"}), 400

    if len(password) < 6:
        return jsonify({"ok": False, "msg": "La contrasena debe tener al menos 6 caracteres"}), 400

    if not terms_accepted:
        return jsonify({"ok": False, "msg": "Debes aceptar los Terminos y la Politica de Privacidad"}), 400

    from core.database import db
    from core.db_models import User, UserConfig

    existing = User.query.filter_by(email=email).first()
    if existing:
        return jsonify({"ok": False, "msg": "Ya existe una cuenta con ese email"}), 409

    user = User(
        email=email,
        password_hash=generate_password_hash(password),
        terms_accepted_at=datetime.now(timezone.utc),
    )
    db.session.add(user)
    db.session.flush()

    user_config = UserConfig(user_id=user.id)
    db.session.add(user_config)
    db.session.commit()

    login_user(user, remember=True)
    log.info(f"New user registered: {email}")
    return jsonify({"ok": True, "msg": "Cuenta creada"})


@auth_bp.route("/login", methods=["POST"])
def login():
    """Log in with email + password."""
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"ok": False, "msg": "Email y contrasena requeridos"}), 400

    from core.db_models import User

    user = User.query.filter_by(email=email).first()
    if not user or not user.password_hash:
        return jsonify({"ok": False, "msg": "Email o contrasena incorrectos"}), 401

    if not check_password_hash(user.password_hash, password):
        return jsonify({"ok": False, "msg": "Email o contrasena incorrectos"}), 401

    if not user.is_active:
        return jsonify({"ok": False, "msg": "Cuenta desactivada"}), 403

    login_user(user, remember=True)
    log.info(f"User logged in: {email}")
    return jsonify({"ok": True, "msg": "Sesion iniciada"})


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return jsonify({"ok": True, "msg": "Sesion cerrada"})


@auth_bp.route("/me")
@login_required
def me():
    return jsonify({
        "ok": True,
        "user": {
            "id": current_user.id,
            "email": current_user.email,
            "is_admin": current_user.is_admin,
            "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
        }
    })
