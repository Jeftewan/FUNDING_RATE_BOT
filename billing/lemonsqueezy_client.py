"""Lemon Squeezy integration — checkout, subscriptions, webhooks.

Uses the REST API (JSON:API format) directly via requests. No SDK dependency.
Docs: https://docs.lemonsqueezy.com/api
"""
import hmac
import hashlib
import logging
import requests
from datetime import datetime
from flask import current_app

logger = logging.getLogger(__name__)

API_BASE = "https://api.lemonsqueezy.com/v1"

# (plan, period) -> config key that holds the Lemon Squeezy variant ID
_VARIANT_KEYS = {
    ('basic', 'monthly'): 'LEMONSQUEEZY_VARIANT_BASIC_MONTHLY',
    ('basic', 'annual'): 'LEMONSQUEEZY_VARIANT_BASIC_ANNUAL',
    ('standard', 'monthly'): 'LEMONSQUEEZY_VARIANT_STANDARD_MONTHLY',
    ('standard', 'annual'): 'LEMONSQUEEZY_VARIANT_STANDARD_ANNUAL',
    ('pro', 'monthly'): 'LEMONSQUEEZY_VARIANT_PRO_MONTHLY',
    ('pro', 'annual'): 'LEMONSQUEEZY_VARIANT_PRO_ANNUAL',
}


def _headers():
    api_key = current_app.config.get('LEMONSQUEEZY_API_KEY', '')
    return {
        'Accept': 'application/vnd.api+json',
        'Content-Type': 'application/vnd.api+json',
        'Authorization': f'Bearer {api_key}',
    }


def _get_variant_id(plan, period):
    key = _VARIANT_KEYS.get((plan, period))
    if not key:
        raise ValueError(f"Unknown plan/period combination: {plan}/{period}")
    variant_id = current_app.config.get(key, '')
    if not variant_id:
        raise ValueError(f"Lemon Squeezy variant not configured for {plan}/{period} ({key})")
    return variant_id


def create_checkout_session(user, plan, period, success_url, cancel_url):
    store_id = current_app.config.get('LEMONSQUEEZY_STORE_ID', '')
    if not store_id:
        raise ValueError("LEMONSQUEEZY_STORE_ID not configured")
    variant_id = _get_variant_id(plan, period)

    payload = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "checkout_data": {
                    "email": user.email,
                    "custom": {
                        "user_id": str(user.id),
                        "plan": plan,
                        "period": period,
                    },
                },
                "product_options": {
                    "redirect_url": success_url,
                },
                "checkout_options": {
                    "embed": False,
                },
            },
            "relationships": {
                "store": {"data": {"type": "stores", "id": str(store_id)}},
                "variant": {"data": {"type": "variants", "id": str(variant_id)}},
            },
        }
    }

    r = requests.post(f"{API_BASE}/checkouts", json=payload, headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()['data']['attributes']['url']


def create_customer_portal_session(user, return_url=None):
    """Return the stored per-subscription signed portal URL.

    Lemon Squeezy issues a unique `urls.customer_portal` per subscription
    during subscription_created, which we store on the user. There is no
    separate "create portal session" API call.
    """
    url = getattr(user, 'customer_portal_url', None)
    if url:
        return url
    # Fallback: fetch from API using the stored subscription id
    sub_id = getattr(user, 'provider_subscription_id', None)
    if not sub_id:
        return None
    r = requests.get(f"{API_BASE}/subscriptions/{sub_id}", headers=_headers(), timeout=10)
    if r.status_code != 200:
        return None
    return r.json().get('data', {}).get('attributes', {}).get('urls', {}).get('customer_portal')


def get_customer_invoices(provider_customer_id, limit=5):
    """Return last `limit` subscription invoices for a customer."""
    if not provider_customer_id:
        return []
    params = {
        'filter[customer_id]': provider_customer_id,
        'page[size]': limit,
        'sort': '-created_at',
    }
    r = requests.get(f"{API_BASE}/subscription-invoices", headers=_headers(),
                     params=params, timeout=10)
    if r.status_code != 200:
        logger.warning("Lemon Squeezy invoices fetch failed: %s", r.text)
        return []

    invoices = []
    for inv in r.json().get('data', []):
        a = inv['attributes']
        invoices.append({
            'id': inv['id'],
            'amount': (a.get('total') or 0) / 100,
            'currency': a.get('currency', 'USD'),
            'status': a.get('status', ''),
            'date': (a.get('created_at') or '')[:10],
            'pdf': a.get('urls', {}).get('invoice_url'),
        })
    return invoices


def verify_webhook_signature(payload_bytes, sig_header, webhook_secret):
    if not webhook_secret or not sig_header:
        return False
    expected = hmac.new(webhook_secret.encode('utf-8'), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)


def _parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00')).replace(tzinfo=None)
    except ValueError:
        return None


def apply_webhook_event(event, db_session, User):
    """Update user plan fields based on a verified Lemon Squeezy webhook.

    Event structure:
      event['meta']['event_name']           -> e.g. 'subscription_created'
      event['meta']['custom_data']          -> {user_id, plan, period}
      event['data']['id']                   -> subscription id (for sub events)
      event['data']['attributes']           -> subscription attributes
    """
    meta = event.get('meta', {}) or {}
    etype = meta.get('event_name', '')
    data = event.get('data', {}) or {}
    attrs = data.get('attributes', {}) or {}
    custom = meta.get('custom_data', {}) or {}

    user_id = custom.get('user_id')
    plan = custom.get('plan')
    period = custom.get('period')

    subscription_id = str(data.get('id', '')) if data.get('type') == 'subscriptions' else None
    customer_id = str(attrs.get('customer_id', '')) if attrs.get('customer_id') else None
    renews_at = _parse_iso(attrs.get('renews_at'))
    ends_at = _parse_iso(attrs.get('ends_at'))
    portal_url = (attrs.get('urls', {}) or {}).get('customer_portal')

    if etype == 'subscription_created':
        if not user_id or not plan:
            return
        user = db_session.get(User, int(user_id))
        if not user:
            return
        if customer_id and not user.provider_customer_id:
            user.provider_customer_id = customer_id
        if subscription_id:
            user.provider_subscription_id = subscription_id
        if portal_url:
            user.customer_portal_url = portal_url
        if not user.plan_override:
            user.plan = plan
            user.plan_billing_period = period
            user.plan_expires_at = renews_at or ends_at
        db_session.commit()
        logger.info("User %s subscribed plan=%s expires=%s", user_id, plan, user.plan_expires_at)

    elif etype in ('subscription_updated', 'subscription_resumed', 'subscription_unpaused',
                   'subscription_payment_success'):
        if not subscription_id:
            return
        user = User.query.filter_by(provider_subscription_id=subscription_id).first()
        if not user or user.plan_override:
            return
        # Renewal: advance expiry to the new renews_at
        if renews_at:
            user.plan_expires_at = renews_at
        if portal_url:
            user.customer_portal_url = portal_url
        # Re-activate plan if the webhook indicates active status after a pause
        status = (attrs.get('status') or '').lower()
        if status in ('active', 'on_trial') and (user.plan == 'none') and plan:
            user.plan = plan
        db_session.commit()
        logger.info("Subscription event %s for user %s, expires=%s", etype, user.id, user.plan_expires_at)

    elif etype in ('subscription_cancelled', 'subscription_expired', 'subscription_paused'):
        if not subscription_id:
            return
        user = User.query.filter_by(provider_subscription_id=subscription_id).first()
        if not user or user.plan_override:
            return
        # On cancel, Lemon Squeezy keeps the sub active until ends_at. Only
        # wipe the plan when it has actually expired.
        if etype == 'subscription_expired' or (ends_at and ends_at <= datetime.utcnow()):
            user.plan = 'none'
            user.plan_expires_at = None
            user.provider_subscription_id = None
            logger.info("Plan cleared for user %s (event=%s)", user.id, etype)
        else:
            user.plan_expires_at = ends_at or user.plan_expires_at
            logger.info("Subscription %s for user %s, active until %s", etype, user.id, ends_at)
        db_session.commit()
