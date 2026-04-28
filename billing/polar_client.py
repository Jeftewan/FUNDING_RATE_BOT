"""Polar.sh integration — checkout, subscriptions, webhooks.

Uses the REST API directly via requests (no SDK dependency).
Standard Webhooks signature spec: https://www.standardwebhooks.com/
Docs: https://polar.sh/docs/api-reference
"""
import base64
import hashlib
import hmac
import logging
import requests
from datetime import datetime
from flask import current_app

logger = logging.getLogger(__name__)

API_BASE = "https://api.polar.sh/v1"

# (plan, period) -> config key holding the Polar product ID
_PRODUCT_KEYS = {
    ('basic', 'monthly'): 'POLAR_PRODUCT_BASIC_MONTHLY',
    ('basic', 'annual'): 'POLAR_PRODUCT_BASIC_ANNUAL',
    ('standard', 'monthly'): 'POLAR_PRODUCT_STANDARD_MONTHLY',
    ('standard', 'annual'): 'POLAR_PRODUCT_STANDARD_ANNUAL',
    ('pro', 'monthly'): 'POLAR_PRODUCT_PRO_MONTHLY',
    ('pro', 'annual'): 'POLAR_PRODUCT_PRO_ANNUAL',
}


def _headers():
    token = current_app.config.get('POLAR_ACCESS_TOKEN', '')
    return {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {token}',
    }


def _get_product_id(plan, period):
    key = _PRODUCT_KEYS.get((plan, period))
    if not key:
        raise ValueError(f"Unknown plan/period combination: {plan}/{period}")
    product_id = current_app.config.get(key, '')
    if not product_id:
        raise ValueError(f"Polar product not configured for {plan}/{period} ({key})")
    return product_id


def create_checkout_session(user, plan, period, success_url, cancel_url):
    product_id = _get_product_id(plan, period)
    payload = {
        "products": [product_id],
        "customer_email": user.email,
        # external_customer_id links this customer to our user ID for future lookups
        "external_customer_id": str(user.id),
        "metadata": {
            "user_id": str(user.id),
            "plan": plan,
            "period": period,
        },
        "success_url": success_url,
    }
    r = requests.post(f"{API_BASE}/checkouts/", json=payload, headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()['url']


def create_customer_portal_session(user, return_url=None):
    """Create a short-lived authenticated portal session for the customer."""
    customer_id = getattr(user, 'provider_customer_id', None)
    if not customer_id:
        return None
    payload = {"customer_id": customer_id}
    if return_url:
        payload["return_url"] = return_url
    r = requests.post(f"{API_BASE}/customer-sessions/", json=payload, headers=_headers(), timeout=10)
    if r.status_code != 201:
        logger.warning("Polar customer session failed: %s", r.text)
        return None
    return r.json().get('customer_portal_url')


def get_customer_invoices(provider_customer_id, limit=5):
    """Return last `limit` orders for a customer."""
    if not provider_customer_id:
        return []
    params = {'customer_id': provider_customer_id, 'limit': limit}
    r = requests.get(f"{API_BASE}/orders/", headers=_headers(), params=params, timeout=10)
    if r.status_code != 200:
        logger.warning("Polar orders fetch failed: %s", r.text)
        return []
    invoices = []
    for order in r.json().get('items', []):
        invoices.append({
            'id': order.get('id'),
            'amount': (order.get('total_amount') or 0) / 100,
            'currency': (order.get('currency') or 'USD').upper(),
            'status': order.get('status', ''),
            'date': (order.get('created_at') or '')[:10],
            'pdf': None,
        })
    return invoices


def verify_webhook_signature(payload_bytes, headers, webhook_secret):
    """Verify Standard Webhooks HMAC-SHA256 signature used by Polar.

    Polar signs: "{webhook-id}.{webhook-timestamp}.{body}"
    Secret is base64-encoded (optionally prefixed with "whsec_").
    Signature header: "v1,<base64-sig>" (space-separated for multiple).
    """
    msg_id = headers.get('webhook-id', '')
    msg_ts = headers.get('webhook-timestamp', '')
    msg_sig = headers.get('webhook-signature', '')

    if not all([msg_id, msg_ts, msg_sig, webhook_secret]):
        return False

    secret = webhook_secret
    if secret.startswith('whsec_'):
        secret = secret[6:]
    try:
        secret_bytes = base64.b64decode(secret)
    except Exception:
        return False

    to_sign = f"{msg_id}.{msg_ts}.".encode() + payload_bytes
    expected = base64.b64encode(
        hmac.new(secret_bytes, to_sign, hashlib.sha256).digest()
    ).decode()

    for versioned in msg_sig.split(' '):
        parts = versioned.split(',', 1)
        if len(parts) == 2 and parts[0] == 'v1' and hmac.compare_digest(expected, parts[1]):
            return True
    return False


def _parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00')).replace(tzinfo=None)
    except ValueError:
        return None


def apply_webhook_event(event, db_session, User):
    """Update user plan fields based on a verified Polar webhook.

    Polar event structure:
      event['type']                              -> 'subscription.created', etc.
      event['data']['id']                        -> subscription UUID
      event['data']['customer_id']               -> Polar customer UUID
      event['data']['customer']['external_id']   -> our user.id (set at checkout)
      event['data']['metadata']                  -> {user_id, plan, period}
      event['data']['status']                    -> active, canceled, revoked, …
      event['data']['current_period_end']        -> ISO timestamp of next renewal
      event['data']['ended_at']                  -> ISO timestamp when ended (if revoked)
    """
    etype = event.get('type', '')
    data = event.get('data', {}) or {}
    metadata = data.get('metadata', {}) or {}
    customer = data.get('customer', {}) or {}

    subscription_id = data.get('id')
    customer_id = data.get('customer_id')
    plan = metadata.get('plan')
    period = metadata.get('period')

    # external_id links back to our User.id; metadata.user_id is a belt-and-suspenders backup
    external_id = (customer.get('external_id')
                   or customer.get('external_customer_id')
                   or metadata.get('user_id'))

    renews_at = _parse_iso(data.get('current_period_end'))
    ended_at = _parse_iso(data.get('ended_at'))
    status = (data.get('status') or '').lower()

    if etype == 'subscription.created':
        if not external_id or not plan:
            return
        try:
            user = db_session.get(User, int(external_id))
        except (ValueError, TypeError):
            return
        if not user:
            return
        if customer_id and not user.provider_customer_id:
            user.provider_customer_id = customer_id
        if subscription_id:
            user.provider_subscription_id = subscription_id
        if not user.plan_override:
            user.plan = plan
            user.plan_billing_period = period
            user.plan_expires_at = renews_at
        db_session.commit()
        logger.info("User %s subscribed plan=%s expires=%s", external_id, plan, user.plan_expires_at)

    elif etype in ('subscription.updated', 'subscription.active', 'subscription.uncanceled'):
        if not subscription_id:
            return
        user = User.query.filter_by(provider_subscription_id=subscription_id).first()
        if not user or user.plan_override:
            return
        if renews_at:
            user.plan_expires_at = renews_at
        # Re-activate if it was previously downgraded
        if status == 'active' and user.plan == 'none' and plan:
            user.plan = plan
        db_session.commit()
        logger.info("Subscription event %s for user %s, expires=%s", etype, user.id, user.plan_expires_at)

    elif etype in ('subscription.canceled', 'subscription.revoked', 'subscription.past_due'):
        if not subscription_id:
            return
        user = User.query.filter_by(provider_subscription_id=subscription_id).first()
        if not user or user.plan_override:
            return
        # revoked = ended immediately; canceled = active until current_period_end
        if etype == 'subscription.revoked' or (ended_at and ended_at <= datetime.utcnow()):
            user.plan = 'none'
            user.plan_expires_at = None
            user.provider_subscription_id = None
            logger.info("Plan cleared for user %s (event=%s)", user.id, etype)
        else:
            if renews_at:
                user.plan_expires_at = renews_at
            logger.info("Subscription %s for user %s, active until %s", etype, user.id, renews_at)
        db_session.commit()
