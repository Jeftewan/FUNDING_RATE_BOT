import logging
from flask import Blueprint, request, jsonify, redirect, current_app
from flask_login import login_required, current_user

from billing.plans import get_plan_limits, user_has_active_plan, trial_days_remaining, PLAN_LIMITS
from billing.lemonsqueezy_client import (
    create_checkout_session,
    create_customer_portal_session,
    get_customer_invoices,
    verify_webhook_signature,
    apply_webhook_event,
)

logger = logging.getLogger(__name__)
billing_bp = Blueprint('billing', __name__)


def _base_url():
    return request.host_url.rstrip('/')


@billing_bp.route('/api/billing/status')
@login_required
def billing_status():
    from billing.plans import get_effective_plan
    plan = getattr(current_user, 'plan', 'none')
    expires = getattr(current_user, 'plan_expires_at', None)
    override = getattr(current_user, 'plan_override', False)
    effective = get_effective_plan(current_user)
    limits = get_plan_limits(effective)
    return jsonify({
        'plan': plan,
        'effective_plan': effective,
        'plan_override': override,
        'plan_override_note': getattr(current_user, 'plan_override_note', None),
        'billing_period': getattr(current_user, 'plan_billing_period', None),
        'plan_expires_at': expires.isoformat() if expires else None,
        'trial_days_remaining': trial_days_remaining(current_user),
        'is_active': user_has_active_plan(current_user),
        'has_portal': bool(getattr(current_user, 'customer_portal_url', None)),
        'limits': limits,
        'plans': {k: {
            'label': v['label'],
            'price_monthly': v['price_monthly'],
            'price_annual': v['price_annual'],
        } for k, v in PLAN_LIMITS.items()},
    })


@billing_bp.route('/api/billing/checkout', methods=['POST'])
@login_required
def billing_checkout():
    data = request.get_json(silent=True) or {}
    plan = data.get('plan', '').lower()
    period = data.get('period', 'monthly').lower()

    if plan not in ('basic', 'standard', 'pro'):
        return jsonify({'error': 'Plan inválido'}), 400
    if period not in ('monthly', 'annual'):
        return jsonify({'error': 'Período inválido'}), 400

    base = _base_url()
    try:
        url = create_checkout_session(
            current_user, plan, period,
            success_url=f"{base}/?billing=success",
            cancel_url=f"{base}/?billing=cancel",
        )
        return jsonify({'url': url})
    except ValueError as e:
        logger.error("Checkout config error: %s", e)
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logger.error("Lemon Squeezy checkout error: %s", e)
        return jsonify({'error': 'Error al crear sesión de pago'}), 500


@billing_bp.route('/api/billing/portal')
@login_required
def billing_portal():
    url = create_customer_portal_session(current_user)
    if not url:
        return jsonify({'error': 'No tienes una suscripción activa'}), 400
    return redirect(url)


@billing_bp.route('/api/billing/invoices')
@login_required
def billing_invoices():
    provider_customer_id = getattr(current_user, 'provider_customer_id', None)
    if not provider_customer_id:
        return jsonify({'invoices': []})
    try:
        invoices = get_customer_invoices(provider_customer_id, limit=5)
        return jsonify({'invoices': invoices})
    except Exception as e:
        logger.error("Lemon Squeezy invoices error: %s", e)
        return jsonify({'invoices': []})


@billing_bp.route('/api/billing/webhook', methods=['POST'])
def billing_webhook():
    payload = request.get_data()
    sig_header = request.headers.get('X-Signature', '')
    webhook_secret = current_app.config.get('LEMONSQUEEZY_WEBHOOK_SECRET', '')

    if not webhook_secret:
        logger.warning("LEMONSQUEEZY_WEBHOOK_SECRET not configured")
        return jsonify({'error': 'Webhook not configured'}), 500

    if not verify_webhook_signature(payload, sig_header, webhook_secret):
        logger.warning("Lemon Squeezy webhook signature verification failed")
        return jsonify({'error': 'Invalid signature'}), 400

    try:
        event = request.get_json(force=True, silent=True) or {}
    except Exception:
        return jsonify({'error': 'Invalid payload'}), 400

    try:
        from core.db_models import User
        from core.database import db
        apply_webhook_event(event, db.session, User)
    except Exception as e:
        logger.error("Webhook handler error for event %s: %s",
                     (event.get('meta') or {}).get('event_name'), e)
        return jsonify({'error': 'Handler error'}), 500

    return jsonify({'received': True})
