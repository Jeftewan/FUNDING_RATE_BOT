from datetime import datetime

PLAN_LIMITS = {
    'basic': {
        'max_positions': 1,
        'alerts': False,
        'auto_trading': False,
        'price_monthly': 10,
        'price_annual': 100,
        'label': 'Basic',
    },
    'standard': {
        'max_positions': 5,
        'alerts': True,
        'auto_trading': False,
        'price_monthly': 20,
        'price_annual': 200,
        'label': 'Standard',
    },
    'pro': {
        'max_positions': 999,
        'alerts': True,
        'auto_trading': True,
        'price_monthly': 40,
        'price_annual': 400,
        'label': 'Pro',
    },
}

PLAN_NONE = {'max_positions': 0, 'alerts': False, 'auto_trading': False}


def get_plan_limits(plan):
    return PLAN_LIMITS.get(plan, PLAN_NONE)


def user_has_active_plan(user):
    # Admin override: perpetual license, ignores Stripe and expiry dates
    if getattr(user, 'plan_override', False) and getattr(user, 'plan', 'none') != 'none':
        return True
    # Normal Stripe-managed plan
    plan = getattr(user, 'plan', 'none')
    expires = getattr(user, 'plan_expires_at', None)
    if plan != 'none' and expires and expires > datetime.utcnow():
        return True
    # Trial period
    trial = getattr(user, 'trial_ends_at', None)
    if trial and trial > datetime.utcnow():
        return True
    return False


def get_effective_plan(user):
    """Return the plan string that should be used for feature gating."""
    if not user_has_active_plan(user):
        return 'none'
    plan = getattr(user, 'plan', 'none')
    # During trial with no paid plan, grant basic limits
    if plan == 'none':
        return 'basic'
    return plan


def trial_days_remaining(user):
    trial = getattr(user, 'trial_ends_at', None)
    if not trial:
        return 0
    delta = trial - datetime.utcnow()
    return max(0, delta.days)
