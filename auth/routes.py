"""Authentication routes: magic link login."""
import logging
import re
from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, jsonify, redirect, url_for, render_template
from flask_login import login_user, logout_user, login_required, current_user
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

log = logging.getLogger("bot.auth")

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

# Will be set by init_auth()
_serializer = None
_config = None


def init_auth(app, config):
    """Initialize auth blueprint with app context."""
    global _serializer, _config
    _config = config
    _serializer = URLSafeTimedSerializer(app.secret_key)
    app.register_blueprint(auth_bp)


def _generate_token(email: str) -> str:
    return _serializer.dumps(email, salt="magic-link")


def _verify_token(token: str, max_age: int = 900) -> str | None:
    """Verify token, return email or None. Default max_age: 15 minutes."""
    try:
        return _serializer.loads(token, salt="magic-link", max_age=max_age)
    except (SignatureExpired, BadSignature):
        return None


@auth_bp.route("/request-link", methods=["POST"])
def request_link():
    """Request a magic link email."""
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()

    if not email or not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
        return jsonify({"ok": False, "msg": "Email invalido"}), 400

    token = _generate_token(email)
    base_url = request.host_url.rstrip("/")
    link = f"{base_url}/auth/verify?token={token}"

    from auth.email_service import send_magic_link
    sent = send_magic_link(email, link, _config)

    if sent:
        return jsonify({"ok": True, "msg": "Enlace enviado a tu correo"})
    else:
        return jsonify({"ok": False, "msg": "Error al enviar email. Verifica la configuracion."}), 500


@auth_bp.route("/verify")
def verify():
    """Verify magic link token and log user in."""
    token = request.args.get("token", "")
    email = _verify_token(token)

    if not email:
        return render_template("login.html", error="Enlace invalido o expirado"), 401

    from core.database import db
    from core.db_models import User, UserConfig

    user = User.query.filter_by(email=email).first()
    if not user:
        # Auto-create user on first login
        user = User(email=email)
        db.session.add(user)
        db.session.flush()  # Get user.id

        # Create default config
        user_config = UserConfig(user_id=user.id)
        db.session.add(user_config)
        db.session.commit()
        log.info(f"New user created: {email}")
    elif not user.is_active:
        return render_template("login.html", error="Cuenta desactivada"), 403

    login_user(user, remember=True)
    log.info(f"User logged in: {email}")
    return redirect("/")


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
