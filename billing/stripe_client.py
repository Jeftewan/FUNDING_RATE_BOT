import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Stripe price ID mapping: (plan, period) -> config key
_PRICE_KEYS = {
    ('basic', 'monthly'): 'STRIPE_PRICE_BASIC_MONTHLY',
    ('basic', 'annual'): 'STRIPE_PRICE_BASIC_ANNUAL',
    ('standard', 'monthly'): 'STRIPE_PRICE_STANDARD_MONTHLY',
    ('standard', 'annual'): 'STRIPE_PRICE_STANDARD_ANNUAL',
    ('pro', 'monthly'): 'STRIPE_PRICE_PRO_MONTHLY',
    ('pro', 'annual'): 'STRIPE_PRICE_PRO_ANNUAL',
}


def _get_stripe():
    """Lazy import stripe so app starts without the key configured."""
    import stripe as _stripe
    from flask import current_app
    _stripe.api_key = current_app.config.get('STRIPE_SECRET_KEY', '')
    return _stripe


def get_price_id(plan, period):
    from flask import current_app
    key = _PRICE_KEYS.get((plan, period))
    if not key:
        raise ValueError(f"Unknown plan/period combination: {plan}/{period}")
    price_id = current_app.config.get(key, '')
    if not price_id:
        raise ValueError(f"Stripe price ID not configured for {plan}/{period} ({key})")
    return price_id


def create_checkout_session(user, plan, period, success_url, cancel_url):
    stripe = _get_stripe()
    price_id = get_price_id(plan, period)

    params = {
        'mode': 'subscription',
        'line_items': [{'price': price_id, 'quantity': 1}],
        'success_url': success_url,
        'cancel_url': cancel_url,
        'metadata': {'user_id': str(user.id), 'plan': plan, 'period': period},
        'subscription_data': {
            'metadata': {'user_id': str(user.id), 'plan': plan, 'period': period}
        },
    }

    # Re-use existing Stripe customer if available
    if user.stripe_customer_id:
        params['customer'] = user.stripe_customer_id
    else:
        params['customer_email'] = user.email

    session = stripe.checkout.Session.create(**params)
    return session.url


def create_customer_portal_session(stripe_customer_id, return_url):
    stripe = _get_stripe()
    session = stripe.billing_portal.Session.create(
        customer=stripe_customer_id,
        return_url=return_url,
    )
    return session.url


def get_customer_invoices(stripe_customer_id, limit=5):
    stripe = _get_stripe()
    invoices = stripe.Invoice.list(customer=stripe_customer_id, limit=limit)
    result = []
    for inv in invoices.data:
        result.append({
            'id': inv.id,
            'amount': inv.amount_paid / 100,
            'currency': inv.currency.upper(),
            'status': inv.status,
            'date': datetime.fromtimestamp(inv.created, tz=timezone.utc).strftime('%Y-%m-%d'),
            'pdf': inv.invoice_pdf,
        })
    return result


def handle_webhook_event(payload, sig_header, webhook_secret):
    stripe = _get_stripe()
    try:
        return stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.warning("Stripe webhook rejected: %s", e)
        return None


def _ts_to_dt(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def apply_webhook_event(event, db_session, User):
    """Update user plan fields based on a verified Stripe webhook event."""
    etype = event['type']
    obj = event['data']['object']

    if etype == 'checkout.session.completed':
        meta = obj.get('metadata', {})
        user_id = meta.get('user_id')
        plan = meta.get('plan')
        period = meta.get('period')
        customer_id = obj.get('customer')
        subscription_id = obj.get('subscription')

        if not user_id or not plan:
            return

        user = db_session.get(User, int(user_id))
        if not user:
            return

        if user.plan_override:
            # Override active: only update customer/subscription IDs, leave plan alone
            if customer_id and not user.stripe_customer_id:
                user.stripe_customer_id = customer_id
            if subscription_id and not user.stripe_subscription_id:
                user.stripe_subscription_id = subscription_id
            db_session.commit()
            return

        stripe = _get_stripe()
        sub = stripe.Subscription.retrieve(subscription_id)
        period_end = _ts_to_dt(sub['current_period_end'])

        user.plan = plan
        user.plan_billing_period = period
        user.plan_expires_at = period_end
        user.stripe_customer_id = customer_id
        user.stripe_subscription_id = subscription_id
        db_session.commit()
        logger.info("User %s upgraded to plan=%s expires=%s", user_id, plan, period_end)

    elif etype == 'invoice.paid':
        sub_id = obj.get('subscription')
        if not sub_id:
            return
        user = User.query.filter_by(stripe_subscription_id=sub_id).first()
        if not user or user.plan_override:
            return
        stripe = _get_stripe()
        sub = stripe.Subscription.retrieve(sub_id)
        period_end = _ts_to_dt(sub['current_period_end'])
        user.plan_expires_at = period_end
        db_session.commit()
        logger.info("Invoice paid for user %s, plan renewed until %s", user.id, period_end)

    elif etype == 'customer.subscription.deleted':
        sub_id = obj.get('id')
        user = User.query.filter_by(stripe_subscription_id=sub_id).first()
        if not user or user.plan_override:
            return
        user.plan = 'none'
        user.plan_expires_at = None
        user.stripe_subscription_id = None
        db_session.commit()
        logger.info("Subscription cancelled for user %s", user.id)
