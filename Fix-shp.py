"""
fix_provision_shop.py — one-off fix for: "login as admin redirects to /signup".

Cause: billing.py's subscription_gate() redirects every request to /signup
whenever the `shop_settings` table is empty. If your admin account was
created via seed_db.py (not via the /signup form), that row was never
created, so the gate never lets you past login.

This script creates that missing shop_settings + subscriptions row so your
existing admin account can log in normally, with a fresh 14-day trial.

Run once:  python fix_provision_shop.py
"""

import billing_db as bdb

SHOP_NAME = "My Shop"          # <-- change if you want a different name
CONTACT_EMAIL = ""             # <-- optional, e.g. "you@example.com"

def main():
    bdb.ensure_billing_tables()
    existing = bdb.get_shop_settings()
    if existing:
        print(f"A shop is already provisioned: {existing['shop_name']!r} "
              f"(shop_id={existing['shop_id']}). Nothing to do.")
        return

    shop_id = bdb.create_shop(SHOP_NAME, CONTACT_EMAIL)
    print(f"Created shop_settings row: shop_id={shop_id}, name={SHOP_NAME!r}")
    print(f"Started a {bdb.TRIAL_DAYS}-day trial subscription.")
    print("You should now be able to log in as admin without being bounced to /signup.")

if __name__ == "__main__":
    main()