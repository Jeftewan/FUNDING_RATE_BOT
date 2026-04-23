"""Admin blueprint — management interface for the business owner."""
import logging
from datetime import datetime
from functools import wraps
from flask import Blueprint, jsonify, request, render_template, abort
from flask_login import login_required, current_user

log = logging.getLogger(__name__)
admin_bp = Blueprint('admin', __name__)


def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not getattr(current_user, 'is_admin', False):
            abort(403)
        return f(*args, **kwargs)
    return decorated


@admin_bp.route('/admin')
@admin_required
def admin_dashboard():
    return render_template('admin.html')


# ── Users ──────────────────────────────────────────────────────────────────

@admin_bp.route('/admin/api/users')
@admin_required
def admin_users():
    from core.db_models import User
    from billing.plans import user_has_active_plan, trial_days_remaining

    users = User.query.order_by(User.created_at.desc()).all()
    result = []
    for u in users:
        expires = u.plan_expires_at.isoformat() if u.plan_expires_at else None
        trial = u.trial_ends_at.isoformat() if u.trial_ends_at else None
        result.append({
            'id': u.id,
            'email': u.email,
            'plan': u.plan or 'none',
            'plan_billing_period': u.plan_billing_period,
            'plan_expires_at': expires,
            'trial_ends_at': trial,
            'trial_days_remaining': trial_days_remaining(u),
            'is_active_plan': user_has_active_plan(u),
            'plan_override': bool(u.plan_override),
            'plan_override_note': u.plan_override_note,
            'stripe_customer_id': u.stripe_customer_id,
            'is_admin': u.is_admin,
            'is_active': u.is_active,
            'created_at': u.created_at.isoformat() if u.created_at else None,
        })
    return jsonify({'users': result})


@admin_bp.route('/admin/api/users/<int:user_id>')
@admin_required
def admin_user_detail(user_id):
    from core.db_models import User
    from billing.plans import user_has_active_plan, trial_days_remaining
    from billing.stripe_client import get_customer_invoices

    u = User.query.get_or_404(user_id)

    invoices = []
    if u.stripe_customer_id:
        try:
            invoices = get_customer_invoices(u.stripe_customer_id, limit=10)
        except Exception as e:
            log.warning("Could not fetch invoices for user %s: %s", user_id, e)

    return jsonify({
        'id': u.id,
        'email': u.email,
        'plan': u.plan or 'none',
        'plan_billing_period': u.plan_billing_period,
        'plan_expires_at': u.plan_expires_at.isoformat() if u.plan_expires_at else None,
        'trial_ends_at': u.trial_ends_at.isoformat() if u.trial_ends_at else None,
        'trial_days_remaining': trial_days_remaining(u),
        'is_active_plan': user_has_active_plan(u),
        'plan_override': bool(u.plan_override),
        'plan_override_note': u.plan_override_note,
        'stripe_customer_id': u.stripe_customer_id,
        'stripe_subscription_id': u.stripe_subscription_id,
        'is_admin': u.is_admin,
        'is_active': u.is_active,
        'created_at': u.created_at.isoformat() if u.created_at else None,
        'invoices': invoices,
    })


@admin_bp.route('/admin/api/users/<int:user_id>/plan', methods=['PATCH'])
@admin_required
def admin_update_user_plan(user_id):
    """Manually set plan, override flag, and expiry for a user."""
    from core.db_models import User
    from core.database import db

    u = User.query.get_or_404(user_id)
    data = request.get_json(silent=True) or {}

    if 'plan' in data:
        plan = data['plan']
        if plan not in ('none', 'basic', 'standard', 'pro'):
            return jsonify({'error': 'Plan inválido'}), 400
        u.plan = plan

    if 'plan_override' in data:
        u.plan_override = bool(data['plan_override'])

    if 'plan_override_note' in data:
        u.plan_override_note = data['plan_override_note'] or None

    if 'plan_expires_at' in data:
        raw = data['plan_expires_at']
        if raw:
            try:
                u.plan_expires_at = datetime.fromisoformat(raw.replace('Z', '+00:00')).replace(tzinfo=None)
            except ValueError:
                return jsonify({'error': 'Formato de fecha inválido'}), 400
        else:
            u.plan_expires_at = None

    if 'plan_billing_period' in data:
        period = data['plan_billing_period']
        if period and period not in ('monthly', 'annual'):
            return jsonify({'error': 'Período inválido'}), 400
        u.plan_billing_period = period or None

    if 'is_active' in data:
        u.is_active = bool(data['is_active'])

    db.session.commit()
    log.info("Admin %s updated plan for user %s: plan=%s override=%s",
             current_user.id, user_id, u.plan, u.plan_override)

    return jsonify({'ok': True, 'user_id': user_id, 'plan': u.plan, 'plan_override': u.plan_override})


# ── Billing summary ────────────────────────────────────────────────────────

@admin_bp.route('/admin/api/billing/summary')
@admin_required
def admin_billing_summary():
    from core.db_models import User
    from billing.plans import user_has_active_plan, PLAN_LIMITS
    from datetime import datetime

    users = User.query.all()
    now = datetime.utcnow()

    counts = {'none': 0, 'basic': 0, 'standard': 0, 'pro': 0}
    active = 0
    in_trial = 0
    expired = 0
    overrides = 0

    for u in users:
        plan = u.plan or 'none'
        counts[plan] = counts.get(plan, 0) + 1

        if u.plan_override and plan != 'none':
            overrides += 1
            active += 1
        elif plan != 'none' and u.plan_expires_at and u.plan_expires_at > now:
            active += 1
        elif u.trial_ends_at and u.trial_ends_at > now:
            in_trial += 1
        else:
            expired += 1

    # Monthly recurring revenue estimate (paid plans only, excluding overrides)
    mrr = 0
    for u in users:
        if u.plan_override:
            continue
        p = u.plan or 'none'
        if p == 'none':
            continue
        if u.plan_expires_at and u.plan_expires_at > now:
            period_price = PLAN_LIMITS.get(p, {}).get('price_monthly', 0)
            if u.plan_billing_period == 'annual':
                period_price = PLAN_LIMITS.get(p, {}).get('price_annual', 0) / 12
            mrr += period_price

    return jsonify({
        'total_users': len(users),
        'active': active,
        'in_trial': in_trial,
        'expired': expired,
        'overrides': overrides,
        'by_plan': counts,
        'mrr_usd': round(mrr, 2),
    })
