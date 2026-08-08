"""One-time migration: introduce multi-tenancy (shops) into warehouse_db.

Usage:
  python migrate_multitenancy.py

What it does:
  1. Creates a `shops` table (id, name, plan, status, created_at, logo_filename).
  2. Inserts a default shop (id=1) so existing data has a home.
  3. Adds a `shop_id` column (default 1, indexed) to every business table.
  4. Backfills existing rows to shop_id=1.
  5. Adds `role='superadmin'` capability by leaving shop_id NULL-able on users
     (a superadmin's shop_id stays NULL — they aren't scoped to one shop).

Safe to re-run: every step uses IF NOT EXISTS / catches "duplicate column"
errors so re-running won't break anything.
"""
from __future__ import annotations

import pymysql
from db import DB_CONFIG


def get_connection():
    # Plain (non-Dict) cursor for this script — simpler tuple indexing below.
    return pymysql.connect(
        host=DB_CONFIG["host"],
        port=DB_CONFIG["port"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        database=DB_CONFIG["database"],
        autocommit=False,
    )

# Tables that hold per-shop business data. `users` is handled separately
# because its shop_id must be NULLable (superadmins aren't tied to a shop).
BUSINESS_TABLES = [
    "products",
    "sales",
    "orders",
    "sale_items",
    "salaries",
    "cash_transactions",
    "daily_cash_balance",
    "debts",
    "debt_payments",
    "audit_log",
    "factures",
    "assistant_chat_history",
    "assistant_memory",
    "assistant_chat_summary",
]

DEFAULT_SHOP_ID = 1


def col_exists(cur, table, column):
    cur.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """,
        (DB_CONFIG["database"], table, column),
    )
    return cur.fetchone()[0] > 0


def table_exists(cur, table):
    cur.execute(
        """
        SELECT COUNT(*) FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """,
        (DB_CONFIG["database"], table),
    )
    return cur.fetchone()[0] > 0


def main():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            print("Creating shops table...")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS shops (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    plan VARCHAR(50) NOT NULL DEFAULT 'test',
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    logo_filename VARCHAR(255),
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB;
                """
            )

            print("Ensuring default shop (id=1) exists...")
            cur.execute("SELECT id FROM shops WHERE id = %s", (DEFAULT_SHOP_ID,))
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO shops (id, name, plan, status) VALUES (%s, %s, %s, %s)",
                    (DEFAULT_SHOP_ID, "Default Shop", "pro", "active"),
                )

            if not col_exists(cur, "shops", "features"):
                print("Adding shops.features (per-shop feature flags, JSON) ...")
                cur.execute("ALTER TABLE shops ADD COLUMN features TEXT NULL")

            if not col_exists(cur, "shops", "logo_url"):
                print("Adding shops.logo_url ...")
                cur.execute("ALTER TABLE shops ADD COLUMN logo_url VARCHAR(1024) NULL")

            # users.shop_id — NULLable (NULL = superadmin, not scoped to a shop)
            if not col_exists(cur, "users", "shop_id"):
                print("Adding users.shop_id (nullable)...")
                cur.execute("ALTER TABLE users ADD COLUMN shop_id BIGINT NULL")
                cur.execute(
                    "UPDATE users SET shop_id = %s WHERE role IN ('admin','seller') AND shop_id IS NULL",
                    (DEFAULT_SHOP_ID,),
                )
                cur.execute("ALTER TABLE users ADD INDEX idx_users_shop (shop_id)")

            for table in BUSINESS_TABLES:
                if not table_exists(cur, table):
                    print(f"Skipping {table} (table not present).")
                    continue
                if col_exists(cur, table, "shop_id"):
                    print(f"Skipping {table} (shop_id already present).")
                    continue
                print(f"Adding {table}.shop_id ...")
                cur.execute(
                    f"ALTER TABLE {table} ADD COLUMN shop_id BIGINT NOT NULL DEFAULT {DEFAULT_SHOP_ID}"
                )
                cur.execute(f"ALTER TABLE {table} ADD INDEX idx_{table}_shop (shop_id)")

        conn.commit()
        print("Migration complete.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_superadmin(username, password):
    """Bootstrap the first superadmin account (shop_id stays NULL)."""
    from werkzeug.security import generate_password_hash

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT username FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                print(f"User '{username}' already exists — skipping superadmin creation.")
                return
            cur.execute(
                "INSERT INTO users (username, password, role, activated, shop_id) "
                "VALUES (%s, %s, 'superadmin', TRUE, NULL)",
                (username, generate_password_hash(password)),
            )
        conn.commit()
        print(f"Superadmin '{username}' created.")
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    main()

    # Optional: python migrate_multitenancy.py --superadmin USERNAME PASSWORD
    if len(sys.argv) == 4 and sys.argv[1] == "--superadmin":
        create_superadmin(sys.argv[2], sys.argv[3])
