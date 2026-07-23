"""
billing.py — self-serve signup + Stripe subscription billing for KassenPilot.

Wires up:
  GET/POST /signup            shop name + admin account + plan picker
  GET      /billing           status page for the logged-in admin
  POST     /billing/checkout  starts a Stripe Checkout Session for a plan
  GET      /billing/success   Stripe redirects here after payment
  POST     /billing/portal    hand off to the Stripe Customer Portal
  POST     /billing/webhook   Stripe -> us (subscription lifecycle events)

Everything here degrades gracefully: if STRIPE_SECRET_KEY isn't set, /signup
still creates the shop + admin account and starts the 14-day trial, it just
skips the "go pay" redirect — useful for local dev / demos.
"""

import logging
import os
from datetime import datetime
from functools import wraps

from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify

from werkzeug.security import generate_password_hash

import billing_db as bdb
from db import find_user, insert_user, log_audit

logger = logging.getLogger(__name__)

billing_bp = Blueprint("billing", __name__)

try:
    import stripe
    STRIPE_SDK_AVAILABLE = True
except ImportError:
    STRIPE_SDK_AVAILABLE = False

STRIPE_SECRET_KEY = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
STRIPE_WEBHOOK_SECRET = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
STRIPE_BILLING_CONFIGURED = bool(STRIPE_SDK_AVAILABLE and STRIPE_SECRET_KEY)
if STRIPE_BILLING_CONFIGURED:
    stripe.api_key = STRIPE_SECRET_KEY

APP_BASE_URL = (os.getenv("APP_BASE_URL") or "").strip().rstrip("/")


def _base_url() -> str:
    return APP_BASE_URL or request.url_root.rstrip("/")


# ---------------------------------------------------------------------------
# Access gate — call once from app.py's before_request
# ---------------------------------------------------------------------------

# Routes that must stay reachable even when a subscription has lapsed, so a
# shop can still pay you.
BILLING_EXEMPT_ENDPOINTS = {
    "billing.signup", "billing.checkout", "billing.checkout_success",
    "billing.portal", "billing.stripe_webhook", "billing.locked",
    "login", "set_language", "static",
}


def subscription_gate():
    """Return a redirect Response if the shop's subscription doesn't allow
    access right now, else None. Call from an app.before_request hook."""
    if request.endpoint in BILLING_EXEMPT_ENDPOINTS or request.endpoint is None:
        return None
    settings = bdb.get_shop_settings()
    if not settings:
        # No shop provisioned yet on this deployment -> send to signup.
        if request.endpoint != "billing.signup":
            return redirect(url_for("billing.signup"))
        return None
    sub = bdb.get_subscription()
    if not bdb.is_access_active(sub):
        return redirect(url_for("billing.locked"))
    return None


# ---------------------------------------------------------------------------
# Self-serve signup
# ---------------------------------------------------------------------------

@billing_bp.route("/signup", methods=["GET", "POST"])
def signup():
    existing = bdb.get_shop_settings()
    if existing:
        # Already provisioned — signup is a one-time thing per deployment.
        flash("This shop is already set up. Please log in.", "info")
        return redirect(url_for("login"))

    if request.method == "POST":
        shop_name = (request.form.get("shop_name") or "").strip()
        admin_username = (request.form.get("username") or "").strip()
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        plan = (request.form.get("plan") or "starter").strip()

        errors = []
        if not shop_name:
            errors.append("Shop name is required.")
        if not admin_username:
            errors.append("Choose a username.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if plan not in bdb.PLANS:
            errors.append("Choose a valid plan.")
        if find_user(admin_username):
            errors.append("That username is already taken.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("signup.html", plans=bdb.PLANS, form=request.form)

        shop_id = bdb.create_shop(shop_name, email)
        insert_user({
            "username": admin_username,
            "password": generate_password_hash(password),
            "role": "admin",
            "profile_img": "",
            "salary": 0.0,
            "activated": True,
        })
        log_audit("signup", "shop", shop_id, f"Shop '{shop_name}' created by {admin_username}",
                  actor=admin_username, module="billing")

        session["username"] = admin_username
        session["role"] = "admin"

        if STRIPE_BILLING_CONFIGURED and request.form.get("skip_payment") != "1":
            return redirect(url_for("billing.checkout", plan=plan))

        flash(f"Welcome to KassenPilot! Your {bdb.TRIAL_DAYS}-day trial has started.", "success")
        return redirect(url_for("index"))

    return render_template("signup.html", plans=bdb.PLANS, form={})


# ---------------------------------------------------------------------------
# Stripe Checkout (subscription creation)
# ---------------------------------------------------------------------------

@billing_bp.route("/billing/checkout", methods=["GET", "POST"])
def checkout():
    if "username" not in session:
        return redirect(url_for("login"))
    if not STRIPE_BILLING_CONFIGURED:
        flash("Card payments aren't configured on this deployment yet.", "warning")
        return redirect(url_for("billing.locked"))

    plan = request.values.get("plan", "starter")
    if plan not in bdb.PLANS:
        plan = "starter"

    settings = bdb.get_shop_settings()
    sub = bdb.get_subscription()
    try:
        price_id = bdb.plan_price_id(plan)
    except RuntimeError as e:
        flash(str(e), "danger")
        return redirect(url_for("billing.locked"))

    checkout_kwargs = dict(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=_base_url() + url_for("billing.checkout_success") + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=_base_url() + url_for("billing.locked"),
        client_reference_id=settings["shop_id"],
        subscription_data={"metadata": {"shop_id": settings["shop_id"], "plan": plan}},
        allow_promotion_codes=True,
    )
    if sub and sub.get("stripe_customer_id"):
        checkout_kwargs["customer"] = sub["stripe_customer_id"]
    elif settings.get("contact_email"):
        checkout_kwargs["customer_email"] = settings["contact_email"]

    try:
        checkout_session = stripe.checkout.Session.create(**checkout_kwargs)
    except Exception:
        logger.exception("Stripe Checkout session creation failed")
        flash("Could not start checkout — please try again in a moment.", "danger")
        return redirect(url_for("billing.locked"))

    return redirect(checkout_session.url, code=303)


@billing_bp.route("/billing/success")
def checkout_success():
    flash("Payment received — welcome aboard!", "success")
    return redirect(url_for("index"))


@billing_bp.route("/billing/portal", methods=["POST"])
def portal():
    if "username" not in session:
        return redirect(url_for("login"))
    if not STRIPE_BILLING_CONFIGURED:
        flash("Billing portal isn't configured on this deployment yet.", "warning")
        return redirect(url_for("billing.status"))
    sub = bdb.get_subscription()
    if not sub or not sub.get("stripe_customer_id"):
        flash("No billing account on file yet — start a subscription first.", "warning")
        return redirect(url_for("billing.locked"))
    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=sub["stripe_customer_id"],
            return_url=_base_url() + url_for("billing.status"),
        )
    except Exception:
        logger.exception("Stripe portal session creation failed")
        flash("Could not open the billing portal — please try again.", "danger")
        return redirect(url_for("billing.status"))
    return redirect(portal_session.url, code=303)


@billing_bp.route("/billing", methods=["GET"])
def status():
    if "username" not in session:
        return redirect(url_for("login"))
    settings = bdb.get_shop_settings()
    sub = bdb.get_subscription()
    return render_template(
        "billing.html", settings=settings, sub=sub, plans=bdb.PLANS,
        stripe_configured=STRIPE_BILLING_CONFIGURED,
        access_active=bdb.is_access_active(sub),
    )


@billing_bp.route("/locked")
def locked():
    """Shown instead of the app when there's no active subscription."""
    settings = bdb.get_shop_settings()
    sub = bdb.get_subscription()
    return render_template(
        "billing_locked.html", settings=settings, sub=sub, plans=bdb.PLANS,
        stripe_configured=STRIPE_BILLING_CONFIGURED,
        logged_in="username" in session,
    )


# ---------------------------------------------------------------------------
# Stripe webhook — source of truth for subscription state
# ---------------------------------------------------------------------------

@billing_bp.route("/billing/webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_BILLING_CONFIGURED:
        return jsonify({"error": "not configured"}), 503

    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        else:
            # No signing secret set — accept but log loudly. Fine for a
            # quick demo, not for production (anyone could POST fake events).
            logger.warning("STRIPE_WEBHOOK_SECRET not set — webhook signature NOT verified.")
            event = stripe.Event.construct_from(request.get_json(force=True), stripe.api_key)
    except Exception:
        logger.exception("Invalid Stripe webhook payload")
        return jsonify({"error": "invalid payload"}), 400

    is_new = bdb.log_billing_event(event["id"], event["type"], str(event["type"]))
    if not is_new:
        return jsonify({"received": True, "duplicate": True})

    etype = event["type"]
    obj = event["data"]["object"]

    try:
        if etype == "checkout.session.completed":
            shop_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("shop_id")
            customer_id = obj.get("customer")
            if shop_id and customer_id:
                bdb.set_stripe_customer(shop_id, customer_id)
                # Tag the Stripe customer with shop_id/shop_name so a
                # cross-shop super-admin dashboard can read everything it
                # needs straight from Stripe, without a separate registry.
                try:
                    settings = bdb.get_shop_settings()
                    if settings:
                        stripe.Customer.modify(
                            customer_id,
                            name=settings.get("shop_name") or None,
                            metadata={"shop_id": shop_id, "shop_name": settings.get("shop_name") or ""},
                        )
                except Exception:
                    logger.exception("Could not tag Stripe customer %s with shop metadata", customer_id)

        elif etype in ("customer.subscription.created", "customer.subscription.updated"):
            shop_id = (obj.get("metadata") or {}).get("shop_id")
            if shop_id:
                plan = (obj.get("metadata") or {}).get("plan", "starter")
                period_end = datetime.utcfromtimestamp(obj["current_period_end"]) if obj.get("current_period_end") else None
                bdb.upsert_subscription_from_stripe(
                    shop_id=shop_id,
                    plan=plan,
                    status=obj.get("status", "active"),
                    stripe_subscription_id=obj.get("id"),
                    current_period_end=period_end,
                    cancel_at_period_end=bool(obj.get("cancel_at_period_end")),
                )

        elif etype == "customer.subscription.deleted":
            bdb.mark_subscription_status(obj.get("id"), status="canceled")

        elif etype == "invoice.payment_failed":
            sub_id = obj.get("subscription")
            if sub_id:
                bdb.mark_subscription_status(sub_id, status="past_due")

        elif etype == "invoice.payment_succeeded":
            sub_id = obj.get("subscription")
            if sub_id:
                period_end = datetime.utcfromtimestamp(obj["lines"]["data"][0]["period"]["end"]) \
                    if obj.get("lines", {}).get("data") else None
                bdb.mark_subscription_status(sub_id, status="active", current_period_end=period_end)

    except Exception:
        logger.exception("Error handling Stripe webhook event %s", etype)
        # Still 200 — we've recorded the event id, so we won't silently
        # lose it, and returning 500 would just make Stripe hammer retries.

    return jsonify({"received": True})
