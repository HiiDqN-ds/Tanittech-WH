"""One-time migration: add per-seller assistant permissions to warehouse_db.

Usage:
  python migrate_seller_permissions.py

What it does:
  Adds a `permissions` column (TEXT, JSON blob) to the `users` table, same
  storage pattern already used for `shops.features` (see
  migrate_multitenancy.py). Every existing seller starts with the column
  NULL, which get_seller_permissions() in db.py treats as "every permission
  off" — i.e. exactly the access sellers already had before this feature
  existed. An admin then opts individual sellers into more via
  /admin/sellers/edit/<username>.

Safe to re-run: uses the same col_exists() IF-NOT-EXISTS guard as
migrate_multitenancy.py, so re-running is a no-op if already applied.
"""
from __future__ import annotations

import pymysql
from db import DB_CONFIG


def get_connection():
    return pymysql.connect(
        host=DB_CONFIG["host"],
        port=DB_CONFIG["port"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        database=DB_CONFIG["database"],
        autocommit=False,
    )


def col_exists(cur, table, column):
    cur.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """,
        (DB_CONFIG["database"], table, column),
    )
    return cur.fetchone()[0] > 0


def main():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if col_exists(cur, "users", "permissions"):
                print("users.permissions already present — nothing to do.")
            else:
                print("Adding users.permissions (per-seller permissions, JSON) ...")
                cur.execute("ALTER TABLE users ADD COLUMN permissions TEXT NULL")
        conn.commit()
        print("Migration complete.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
