"""
app/routes/billing.py
Phase 13-B4.1C: SaaS Billing Foundation

Skeleton endpoints for provider-agnostic webhook synchronization.
NO LIVE PAYMENT EXECUTION. Do not store secrets.
"""

from flask import Blueprint, request, jsonify
import hashlib
import hmac
import logging

from app.config import RAZORPAY_WEBHOOK_SECRET, STRIPE_WEBHOOK_SECRET

billing_bp = Blueprint('billing', __name__, url_prefix='/webhooks')


def _verify_hmac(secret, header_value, prefix=""):
    """Phase 14C: constant-time HMAC-SHA256 check over the RAW request body.

    Returns True when the request may proceed. An unset secret returns True and
    warns, so behaviour is unchanged until the secret is configured — the same
    opt-in pattern used for the WhatsApp webhook.

    IMPORTANT: the event handlers below are still inert stubs (`pass`). They
    must NOT be implemented until the corresponding secret is set, or the
    provider's subscription state becomes forgeable by anyone who can POST to
    this URL. This function exists now so that gate is already in place.
    """
    if not secret:
        logging.warning(
            "⚠️ Billing webhook signature verification DISABLED "
            "(secret unset) — payload is unauthenticated")
        return True
    if not header_value:
        logging.warning("⚠️ Billing webhook rejected: signature header missing")
        return False
    supplied = header_value[len(prefix):] if prefix and header_value.startswith(prefix) else header_value
    expected = hmac.new(secret.encode("utf-8"),
                        request.get_data(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        logging.warning("⚠️ Billing webhook rejected: invalid signature")
        return False
    return True

@billing_bp.route('/razorpay', methods=['POST'])
def razorpay_webhook():
    """
    Razorpay Webhook Endpoint
    Listens for subscription state changes.
    """
    # Phase 14C: signature verification (was a commented placeholder).
    if not _verify_hmac(RAZORPAY_WEBHOOK_SECRET,
                        request.headers.get('X-Razorpay-Signature')):
        return jsonify({"error": "Invalid signature"}), 403

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "Empty payload"}), 400
        
    event = payload.get('event')
    logging.info(f"Received Razorpay Webhook Event: {event}")
    
    # 2. Event Routing Structure
    if event == 'subscription.activated':
        # handle_subscription_activated(payload)
        pass
    elif event == 'subscription.charged':
        # handle_subscription_charged(payload)
        pass
    elif event == 'subscription.halted':
        # handle_subscription_halted(payload)
        pass
    elif event == 'subscription.cancelled':
        # handle_subscription_cancelled(payload)
        pass
        
    # 3. Always return 200 OK so webhook doesn't retry infinitely
    return jsonify({"status": "ok"}), 200


@billing_bp.route('/stripe', methods=['POST'])
def stripe_webhook():
    """
    Stripe Webhook Endpoint
    Listens for subscription state changes.
    """
    # Phase 14C: signature verification (was a commented placeholder).
    if not _verify_hmac(STRIPE_WEBHOOK_SECRET,
                        request.headers.get('Stripe-Signature')):
        return jsonify({"error": "Invalid signature"}), 403
        
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "Empty payload"}), 400
        
    event = payload.get('type')
    logging.info(f"Received Stripe Webhook Event: {event}")
    
    # 2. Event Routing Structure
    if event == 'customer.subscription.created':
        pass
    elif event == 'customer.subscription.updated':
        pass
    elif event == 'customer.subscription.deleted':
        pass
    elif event == 'invoice.paid':
        pass
    elif event == 'invoice.payment_failed':
        pass
        
    return jsonify({"status": "ok"}), 200
