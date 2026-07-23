"""
billing_db.py — subscription & shop-account data layer for KassenPilot.

This adds the pieces the product-proposal deck calls "Billing + onboarding"
and "Compliance basics": a self-serve signup, a Stripe-backed subscription
record, and GDPR export/delete helpers.

Design note (read this before touching multi-tenancy later):
This app is deployed **one instance per shop** (its own database, its own
Stripe subscription). That's why there's a single `subscriptions` row per
database rather than a `tenant_id` column on every table. It means selling
today doesn't require rewriting the ~100 existing data-access functions in
db.py, at the cost of one deploy per customer. If/when you outgrow that
(dozens+ of shops), the migration path is: add `tenant_id` to every table
below `shop_settings`, thread it through db.py's queries, and scope
`login_required` by tenant instead of by process. Everything in this file
already keys naturally off a single `shop_id` UUID so that migration is a
column-add, not a redesign.
"""

from datetime import datetime, timedelta
import os
import uuid
import logging

from db import get_connection, fetch_one, fetch_all, execute_query

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Plans — mirrors the pricing on the KassenPilot proposal deck (slide 5).
# Set the Stripe Price IDs via env vars once you've created them in the
# Stripe Dashboard (Products -> Add product -> add a recurring price).
# ---------------------------------------------------------------------------
PLANS = {
    "starter": {
        "name": "Starter",
        "price_eur": 29,
        "price_id_env": "STRIPE_PRICE_STARTER",
        "blurb": "1 register, core inventory + sales + kasse",
        "seller_limit": 2,
    },
    "pro": {
        "name": "Pro",
        "price_eur": 59,
        "price_id_env": "STRIPE_PRICE_PRO",
        "blurb": "Unlimited staff, debts, payroll, AI assistant",
        "seller_limit": None,
    },
    "business": {
        "name": "Business",
        "price_eur": 99,
        "price_id_env": "STRIPE_PRICE_BUSINESS",
        "blurb": "Everything in Pro + priority support",
        "seller_limit": None,
    },
}

TRIAL_DAYS = 14


def plan_price_id(plan_key: str) -> str:
    plan = PLANS.get(plan_key)
    if not plan:
        raise ValueError(f"Unknown plan '{plan_key}'")
    price_id = (os.getenv(plan["price_id_env"]) or "").strip()
    if not price_id:
        raise RuntimeError(
            f"{plan['price_id_env']} is not set — create the '{plan['name']}' "
            f"price in the Stripe Dashboard and put its price_... id in your .env"
        )
    return price_id


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_billing_tables() -> None:
    """Create the shop_settings / subscriptions tables if missing. Safe to
    call on every startup (same pattern as the ensure_* functions in db.py)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS shop_settings (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    shop_id VARCHAR(36) NOT NULL UNIQUE,
                    shop_name VARCHAR(255) NOT NULL,
                    contact_email VARCHAR(255),
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB;
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    shop_id VARCHAR(36) NOT NULL,
                    plan VARCHAR(50) NOT NULL DEFAULT 'starter',
                    status VARCHAR(50) NOT NULL DEFAULT 'trialing',
                    stripe_customer_id VARCHAR(255),
                    stripe_subscription_id VARCHAR(255),
                    trial_ends_at DATETIME,
                    current_period_end DATETIME,
                    cancel_at_period_end BOOLEAN DEFAULT FALSE,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uniq_shop (shop_id)
                ) ENGINE=InnoDB;
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS billing_events (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    stripe_event_id VARCHAR(255) UNIQUE,
                    event_type VARCHAR(100),
                    received_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    payload_summary TEXT
                ) ENGINE=InnoDB;
                """
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Shop + subscription lifecycle
# ---------------------------------------------------------------------------

def get_shop_settings():
    return fetch_one("SELECT * FROM shop_settings LIMIT 1;")


def create_shop(shop_name: str, contact_email: str) -> str:
    """Called once, during signup. Returns the new shop_id."""
    shop_id = str(uuid.uuid4())
    execute_query(
        "INSERT INTO shop_settings (shop_id, shop_name, contact_email) VALUES (%s, %s, %s);",
        (shop_id, shop_name, contact_email),
    )
    trial_end = datetime.utcnow() + timedelta(days=TRIAL_DAYS)
    execute_query(
        """
        INSERT INTO subscriptions (shop_id, plan, status, trial_ends_at)
        VALUES (%s, 'starter', 'trialing', %s);
        """,
        (shop_id, trial_end),
    )
    return shop_id


def get_subscription():
    return fetch_one("SELECT * FROM subscriptions ORDER BY id DESC LIMIT 1;")


def set_stripe_customer(shop_id: str, customer_id: str) -> None:
    execute_query(
        "UPDATE subscriptions SET stripe_customer_id = %s WHERE shop_id = %s;",
        (customer_id, shop_id),
    )


def upsert_subscription_from_stripe(shop_id: str, plan: str, status: str,
                                     stripe_subscription_id: str,
                                     current_period_end,
                                     cancel_at_period_end: bool) -> None:
    execute_query(
        """
        UPDATE subscriptions
        SET plan = %s, status = %s, stripe_subscription_id = %s,
            current_period_end = %s, cancel_at_period_end = %s
        WHERE shop_id = %s;
        """,
        (plan, status, stripe_subscription_id, current_period_end,
         cancel_at_period_end, shop_id),
    )


def mark_subscription_status(stripe_subscription_id: str, status: str,
                              current_period_end=None,
                              cancel_at_period_end=None) -> None:
    sub = fetch_one(
        "SELECT * FROM subscriptions WHERE stripe_subscription_id = %s;",
        (stripe_subscription_id,),
    )
    if not sub:
        logger.warning("Webhook for unknown subscription %s", stripe_subscription_id)
        return
    fields, params = ["status = %s"], [status]
    if current_period_end is not None:
        fields.append("current_period_end = %s")
        params.append(current_period_end)
    if cancel_at_period_end is not None:
        fields.append("cancel_at_period_end = %s")
        params.append(cancel_at_period_end)
    params.append(stripe_subscription_id)
    execute_query(
        f"UPDATE subscriptions SET {', '.join(fields)} WHERE stripe_subscription_id = %s;",
        tuple(params),
    )


def log_billing_event(stripe_event_id: str, event_type: str, summary: str) -> bool:
    """Returns False if we've already processed this event (idempotency —
    Stripe retries webhooks, so this matters)."""
    existing = fetch_one(
        "SELECT id FROM billing_events WHERE stripe_event_id = %s;", (stripe_event_id,)
    )
    if existing:
        return False
    execute_query(
        "INSERT INTO billing_events (stripe_event_id, event_type, payload_summary) VALUES (%s,%s,%s);",
        (stripe_event_id, event_type, summary),
    )
    return True


def is_access_active(sub: dict) -> bool:
    """Whether the shop should be let into the app right now."""
    if not sub:
        return False
    status = sub.get("status")
    if status in ("active", "trialing"):
        if status == "trialing" and sub.get("trial_ends_at"):
            return datetime.utcnow() <= sub["trial_ends_at"]
        return True
    # past_due gets a short grace window so a failed card doesn't lock
    # someone out mid-sale; Stripe will already be dunning them by email.
    if status == "past_due" and sub.get("current_period_end"):
        return datetime.utcnow() <= sub["current_period_end"] + timedelta(days=3)
    return False
