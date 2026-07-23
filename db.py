from datetime import datetime, timedelta, date
import json
import logging
import os
import uuid
from typing import List, Dict, Any, Optional

import pymysql
import pymysql.cursors

# MySQL configuration (override via environment variables, see .env.example)
DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", "root"),
    "database": os.getenv("MYSQL_DATABASE", "warehouse_db"),
    "autocommit": False,
}

logger = logging.getLogger(__name__)


def get_connection():
    return pymysql.connect(
        host=DB_CONFIG["host"],
        port=DB_CONFIG["port"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        database=DB_CONFIG["database"],
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


# Fetch multiple rows
def fetch_all(query: str, params=None) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            return list(cur.fetchall() or [])
    finally:
        conn.close()


# Fetch single row
def fetch_one(query: str, params=None) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            return cur.fetchone()
    finally:
        conn.close()



# Execute INSERT/UPDATE/DELETE
def execute_query(query: str, params=None):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            conn.commit()
            return cur.lastrowid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Load all users
def load_users():
    return fetch_all("SELECT * FROM users ORDER BY username;")


def load_all_order_items(order_id):
    query = """
        SELECT 
            p.order_date AS date,
            i.product_name,
            i.quantity,
            i.unit_price AS price,
            i.total_price
        FROM 
            purchases p
        JOIN 
            purchase_items i ON p.id = i.purchase_id
        WHERE 
            p.id = %s;
    """
    return fetch_all(query, (order_id,))


# Find user by username
def find_user(username):
    return fetch_one("SELECT * FROM users WHERE username = %s;", (username,))


# ---------------------------------------------------------------------------
# Multi-tenancy (shops) — superadmin creates/manages shops; every admin/
# seller belongs to exactly one shop via users.shop_id. Superadmin accounts
# have shop_id = NULL (not scoped to any single shop).
# ---------------------------------------------------------------------------

def get_user_shop_id(username):
    """Resolve a username straight to its shop_id (or None for superadmin)."""
    user = find_user(username)
    return user["shop_id"] if user else None


def list_shops():
    return fetch_all("SELECT * FROM shops ORDER BY created_at DESC;")


def get_shop(shop_id):
    return fetch_one("SELECT * FROM shops WHERE id = %s;", (shop_id,))


def set_shop_logo(shop_id, logo_url):
    execute_query("UPDATE shops SET logo_url = %s WHERE id = %s;", (logo_url or None, shop_id))


# ---------------------------------------------------------------------------
# Per-shop feature flags — superadmin can grant/revoke these per shop.
# Stored as a JSON blob in shops.features. Any flag not yet present in a
# shop's stored JSON defaults to True, so existing shops keep working
# exactly as before until a superadmin explicitly turns something off.
# ---------------------------------------------------------------------------

DEFAULT_SHOP_FEATURES = {
    'assistant': True,       # AI assistant chat
    'card_payment': True,    # Stripe Terminal card payments
    'barcode_printing': True,  # printable barcode labels for products
}


def get_shop_features(shop_id):
    features = dict(DEFAULT_SHOP_FEATURES)
    if shop_id is None:
        return features  # superadmin accounts aren't scoped to a shop
    shop = get_shop(shop_id)
    if shop and shop.get('features'):
        try:
            stored = json.loads(shop['features'])
            if isinstance(stored, dict):
                features.update(stored)
        except (TypeError, ValueError):
            pass
    return features


def set_shop_feature(shop_id, feature, enabled):
    """Flip a single feature flag for a shop and persist the full set."""
    if feature not in DEFAULT_SHOP_FEATURES:
        raise ValueError(f"Unknown feature: {feature}")
    features = get_shop_features(shop_id)
    features[feature] = bool(enabled)
    execute_query("UPDATE shops SET features = %s WHERE id = %s;", (json.dumps(features), shop_id))
    return features


def create_shop(name, plan="test", status="active"):
    return execute_query(
        "INSERT INTO shops (name, plan, status) VALUES (%s, %s, %s);",
        (name, plan, status),
    )


def toggle_shop_status(shop_id):
    shop = get_shop(shop_id)
    if not shop:
        return None
    new_status = "suspended" if shop["status"] == "active" else "active"
    execute_query("UPDATE shops SET status = %s WHERE id = %s;", (new_status, shop_id))
    return new_status


def list_shop_users(shop_id):
    return fetch_all(
        "SELECT * FROM users WHERE shop_id = %s ORDER BY role, username;", (shop_id,)
    )


# Insert user
def insert_user(user):
    query = """
        INSERT INTO users (username, password, role, profile_img, salary, activated, shop_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s);
    """
    params = (
        user["username"],
        user["password"],
        user["role"],
        user.get("profile_img", ""),
        user.get("salary", 0.0),
        user.get("activated", False),
        user.get("shop_id"),  # None for superadmin
    )
    execute_query(query, params)


# Update user
def update_user(username, updates):
    query = """
        UPDATE users
        SET profile_img = %s,
            salary = %s,
            activated = %s
        WHERE username = %s
    """
    params = (
        updates.get("profile_img", ""),
        updates.get("salary", 0.0),
        updates.get("activated", False),
        username,
    )
    execute_query(query, params)


def set_user_password(username, password_hash):
    """Superadmin resetting a shop admin's (or any account's) password."""
    execute_query("UPDATE users SET password = %s WHERE username = %s", (password_hash, username))


# Every table that stores a username as a plain string reference (there are
# no real foreign keys on this column across the schema) — kept in one place
# so a rename can cascade everywhere the old username would otherwise be
# orphaned. If a new business table starts recording usernames, add it here.
USERNAME_REFERENCE_COLUMNS = [
    ("sales", "username"),
    ("orders", "user"),
    ("salaries", "employee"),
    ("cash_transactions", "username"),
    ("debt_payments", "recorded_by"),
    ("audit_log", "actor"),
    ("factures", "created_by"),
    ("assistant_chat_history", "username"),
    ("products", "seller"),
    ("assistant_memory", "username"),
    ("assistant_chat_summary", "username"),
]


def rename_username(old_username, new_username):
    """Rename a login and cascade it across every table that references the
    username as plain text, in a single transaction (all-or-nothing) so
    historical sales/audit/salary records stay linked to the right person.
    Raises if new_username is already taken."""
    if find_user(new_username):
        raise ValueError(f'Username "{new_username}" is already taken.')

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for table, column in USERNAME_REFERENCE_COLUMNS:
                cur.execute(
                    f"UPDATE {table} SET {column} = %s WHERE {column} = %s",
                    (new_username, old_username),
                )
            cur.execute(
                "UPDATE users SET username = %s WHERE username = %s",
                (new_username, old_username),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Toggle just the activated flag, without touching profile_img/salary
def set_user_activated(username, activated):
    execute_query(
        "UPDATE users SET activated = %s WHERE username = %s", (activated, username)
    )


# All accounts that can log in at /login, across every shop — for the
# superadmin accounts-management screen.
# Store admin accounts only, across every shop — for the superadmin's
# grant/revoke card grid. Sellers are managed by their own shop's admin,
# not the superadmin.
def list_all_accounts():
    admins = fetch_all(
        """
        SELECT u.username, u.role, u.activated, u.shop_id,
               s.name AS shop_name, s.plan AS plan
        FROM users u
        LEFT JOIN shops s ON s.id = u.shop_id
        WHERE u.role = 'admin'
        ORDER BY u.username;
        """
    )
    seller_counts = fetch_all(
        "SELECT shop_id, COUNT(*) AS c FROM users WHERE role = 'seller' GROUP BY shop_id;"
    )
    counts_by_shop = {row["shop_id"]: row["c"] for row in seller_counts}
    for admin in admins:
        admin["seller_count"] = counts_by_shop.get(admin["shop_id"], 0)
    return admins


# Delete user
def delete_user(username):
    execute_query("DELETE FROM users WHERE username = %s", (username,))


# Load all items
def load_items():
    return fetch_all("SELECT * FROM products ORDER BY product_name;")


def find_item(barcode_value):
    return fetch_one("SELECT * FROM products WHERE barcode = %s;", (barcode_value,))


def query_one(query, params=None):
    return fetch_one(query, params)


def ensure_products_barcode_primary_key() -> None:
    """Databases created before this change have `products.id INT
    AUTO_INCREMENT PRIMARY KEY` with `barcode` as a separate UNIQUE column.
    The application now treats `barcode` as the product's real identity
    everywhere (URLs, the chat assistant, receipts/labels), so this makes
    that the actual primary key too: `id` is dropped and `barcode` becomes
    the PRIMARY KEY. Safe to run on every boot — it only touches databases
    that still have the old `id` column, and does nothing once migrated.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'products'
                  AND COLUMN_NAME = 'id';
                """
            )
            if not cur.fetchone():
                return  # already migrated (or a fresh DB that never had `id`)

            # barcode is already NOT NULL UNIQUE on every pre-existing
            # install, so it can become the primary key directly. Order
            # matters: AUTO_INCREMENT must be dropped from `id` before its
            # PRIMARY KEY can be dropped, and the old PK must go before a
            # new one can be added.
            cur.execute("ALTER TABLE products MODIFY id INT NOT NULL;")
            cur.execute("ALTER TABLE products DROP PRIMARY KEY;")
            cur.execute("ALTER TABLE products DROP COLUMN id;")
            cur.execute("ALTER TABLE products ADD PRIMARY KEY (barcode);")
            conn.commit()
            logger.info('Migrated products table: barcode is now the primary key (id column dropped).')
    except Exception:
        conn.rollback()
        logger.exception('Could not migrate products to a barcode primary key')
    finally:
        conn.close()


def ensure_products_condition_column() -> None:
    """Add an `item_condition` column to `products` if an older DB predates
    it. Stores the item's condition: 'neu' (new), 'gebraucht' (used), or
    'defekt' (defective). Defaults to 'neu' so existing products and new
    purchase orders are treated as new unless explicitly set otherwise.
    Same safe/idempotent pattern as ensure_products_sku_column().
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'products'
                  AND COLUMN_NAME = 'item_condition';
                """
            )
            if not cur.fetchone():
                cur.execute("ALTER TABLE products ADD COLUMN item_condition VARCHAR(20) NOT NULL DEFAULT 'neu';")
                cur.execute("ALTER TABLE products ADD INDEX idx_products_condition (item_condition);")
            conn.commit()
    except Exception:
        conn.rollback()
        logger.exception('Could not ensure products.item_condition column/index')
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Refunds — full-stack refund support for sales
# ---------------------------------------------------------------------------

def ensure_refunds_table() -> None:
    """Create the `refunds` table if it doesn't exist. Idempotent — safe to
    call on every app boot. Supports full and partial (per-line-item)
    refunds for both cash and card payments, with automatic stock
    restoration and Kasse logging."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS refunds (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    sale_id VARCHAR(36) NOT NULL,
                    sale_item_id BIGINT NULL,
                    barcode VARCHAR(255),
                    product_name VARCHAR(255),
                    quantity INT NOT NULL DEFAULT 0,
                    unit_price DECIMAL(12,2) NOT NULL DEFAULT 0,
                    total_refund_amount DECIMAL(12,2) NOT NULL DEFAULT 0,
                    refund_method VARCHAR(20) NOT NULL DEFAULT 'cash',
                    stripe_refund_id VARCHAR(255) NULL,
                    reason TEXT,
                    refunded_by VARCHAR(255),
                    refunded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_refunds_sale (sale_id),
                    INDEX idx_refunds_method (refund_method)
                ) ENGINE=InnoDB;
                """
            )
            conn.commit()
            logger.info("Ensured refunds table exists.")
    except Exception:
        conn.rollback()
        logger.exception('Could not ensure refunds table')
    finally:
        conn.close()


def record_refund(sale_id, sale_item_id, barcode, product_name, quantity,
                  unit_price, total_refund_amount, refund_method, reason=None,
                  stripe_refund_id=None, refunded_by=None):
    """Record a refund for one or more line items in a sale.

    - Inserts a row into the `refunds` table
    - Restores the returned quantity to `products.stock` for the given barcode
    - If the refund method is 'cash', records a Kasse withdrawal
      (auszahlung) so the cash balance stays correct
    - Returns the new refund record id

    The caller (app route or assistant tool) is responsible for:
      - Processing Stripe Terminal refund (card payments) via
        stripe_refund_payment()
      - Verifying that the quantity being refunded does not exceed the
        quantity bought (minus any already-refunded qty for this item)
      - Calling log_audit for the audit trail
    """
    total_refund_amount = round(float(total_refund_amount), 2)
    unit_price = round(float(unit_price), 2)
    quantity = int(quantity)

    refund_id = execute_query(
        """
        INSERT INTO refunds
            (sale_id, sale_item_id, barcode, product_name, quantity, unit_price,
             total_refund_amount, refund_method, stripe_refund_id, reason, refunded_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """,
        (sale_id, sale_item_id, barcode, product_name, quantity, unit_price,
         total_refund_amount, refund_method, stripe_refund_id, reason, refunded_by),
    )

    # Restore stock
    if barcode:
        execute_query(
            "UPDATE products SET quantity = quantity + %s WHERE barcode = %s;",
            (quantity, barcode),
        )

    # Log Kasse withdrawal for cash refunds
    if refund_method == 'cash':
        username = refunded_by or 'system'
        execute_query(
            """
            INSERT INTO cash_transactions (date, amount, type, description, username, payment_method)
            VALUES (%s, %s, %s, %s, %s, %s);
            """,
            (datetime.now(), total_refund_amount, 'auszahlung',
             f'Rückerstattung für Verkauf #{sale_id}: {quantity} × {product_name}',
             username, 'cash'),
        )

    return refund_id


def get_sale_refunds(sale_id):
    """All refunds for a given sale, ordered most recent first."""
    return fetch_all(
        "SELECT * FROM refunds WHERE sale_id = %s ORDER BY refunded_at DESC;",
        (sale_id,),
    )


def get_total_refunded_for_sale(sale_id):
    """Total amount refunded so far for this sale (across all items)."""
    row = fetch_one(
        "SELECT COALESCE(SUM(total_refund_amount), 0) AS total FROM refunds WHERE sale_id = %s;",
        (sale_id,),
    )
    return round(float(row['total'] or 0), 2)


def get_total_refunded_qty_for_item(sale_item_id):
    """Total quantity already refunded for a specific sale line item."""
    row = fetch_one(
        "SELECT COALESCE(SUM(quantity), 0) AS total FROM refunds WHERE sale_item_id = %s;",
        (sale_item_id,),
    )
    return int(row['total'] or 0)


def load_sales_with_refunds():
    """Same as load_sales() but attaches refund data to each sale's items
    and adds a 'total_refunded' field at the sale level."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sale_id as order_id, username as user, sale_date as date,
                       total_sale_price as total_order_price
                FROM sales
                ORDER BY sale_date DESC;
                """
            )
            sales = list(cur.fetchall() or [])

            for sale in sales:
                sale_id = sale["order_id"]
                cur.execute(
                    """
                    SELECT id, product_name, barcode, quantity, sale_price, total_price, profit
                    FROM sale_items WHERE sale_id = %s;
                    """,
                    (sale_id,),
                )
                items = list(cur.fetchall() or [])
                sale["items"] = items
                sale["total_refunded"] = get_total_refunded_for_sale(sale_id)

                # Attach already-refunded qty per item
                for item in items:
                    item["refunded_qty"] = get_total_refunded_qty_for_item(item["id"])

            return sales
    finally:
        conn.close()


def stripe_refund_payment(payment_intent_id, amount_eur):
    """Process a refund via Stripe for a card payment.

    Requires STRIPE_CONFIGURED on the server. The PaymentIntent's
    stripe_payment_intent_id was stored on the original sale (see
    ensure_sales_stripe_payment_intent_column), so this looks it up and
    creates a partial/full refund on that same intent.

    Returns the Stripe Refund object. Raises RuntimeError if Stripe is
    not configured, or ValueError if the refund cannot be processed (e.g.
    already fully refunded, invalid amount).
    """
    import stripe

    if not bool(os.getenv('STRIPE_SECRET_KEY')):
        raise RuntimeError('Stripe is not configured on the server.')
    stripe.api_key = os.getenv('STRIPE_SECRET_KEY')

    amount_cents = int(round(float(amount_eur) * 100))
    if amount_cents <= 0:
        raise ValueError('Refund amount must be greater than 0.')

    try:
        refund = stripe.Refund.create(
            payment_intent=payment_intent_id,
            amount=amount_cents,
        )
        return refund
    except stripe.error.StripeError as e:
        raise ValueError(f'Stripe refund failed: {e.user_message or str(e)}')


def ensure_products_sku_column() -> None:
    """Add a `sku` column (+ index) to `products` if an older DB predates
    it. Needed for "search by SKU" support across the items page, the chat
    assistant, and the external API. Same safe/idempotent pattern as
    ensure_sales_extra_columns(): only issues DDL when actually missing, and
    swallows errors so two gunicorn workers booting at once can't crash
    each other.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'products'
                  AND COLUMN_NAME = 'sku';
                """
            )
            if not cur.fetchone():
                cur.execute("ALTER TABLE products ADD COLUMN sku VARCHAR(64) NULL;")

            cur.execute(
                """
                SELECT INDEX_NAME FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'products'
                  AND INDEX_NAME = 'idx_products_sku';
                """
            )
            if not cur.fetchone():
                # Speeds up exact/prefix SKU lookups (search_items() below);
                # a plain index, not UNIQUE, since older rows will have
                # sku = NULL and MySQL allows any number of NULLs in a
                # unique index anyway — application-level checks already
                # enforce "no two products share a non-empty SKU/barcode".
                cur.execute("ALTER TABLE products ADD INDEX idx_products_sku (sku);")
            conn.commit()
    except Exception:
        conn.rollback()
        logger.exception('Could not ensure products.sku column/index')
    finally:
        conn.close()


def ensure_seller_permissions_column() -> None:
    """Add an `assistant_permissions` column to `users` if an older DB
    predates it. Stores a comma-separated set of category keys an admin
    has granted a specific seller beyond the assistant's default
    read-only/sales access — e.g. 'kasse,items' — matching the "KI-Assistent
    — zusätzliche Berechtigungen" checkboxes on the Edit Seller page.
    Defaults to '' (no extra grants) so existing sellers keep today's
    restricted behaviour until an admin actively grants a category.
    Same safe/idempotent pattern as ensure_products_sku_column().
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users'
                  AND COLUMN_NAME = 'assistant_permissions';
                """
            )
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE users ADD COLUMN assistant_permissions VARCHAR(255) NOT NULL DEFAULT '';"
                )
            conn.commit()
    except Exception:
        conn.rollback()
        logger.exception('Could not ensure users.assistant_permissions column')
    finally:
        conn.close()


def get_seller_permission_categories(username: str) -> set:
    """The set of extra permission categories (e.g. {'kasse', 'items'})
    granted to this seller. Empty set if none, or if the account/column
    can't be read (fail closed — never silently grants access)."""
    row = fetch_one("SELECT assistant_permissions FROM users WHERE username = %s;", (username,))
    raw = (row or {}).get('assistant_permissions') or ''
    return {p.strip() for p in raw.split(',') if p.strip()}


def set_seller_permission_categories(username: str, categories) -> None:
    """Overwrite the full set of granted categories for this seller."""
    value = ','.join(sorted({str(c).strip() for c in categories if str(c).strip()}))
    execute_query("UPDATE users SET assistant_permissions = %s WHERE username = %s;", (value, username))


def search_items(query: str = '', limit: int = 50):
    """Single source of truth for product search — reused by the items
    page, the chat assistant's list_items tool, and the external
    /api/v1/items endpoint, so all three match the exact same rows the
    exact same way instead of each re-implementing their own filter.

    Matches by (in relevance order):
      1. exact barcode or exact SKU match (what a barcode-scanner produces)
      2. substring match on product name, barcode, or SKU (case-insensitive)

    An empty query returns the full catalog (unchanged existing
    behaviour), same as load_items().
    """
    q = (query or '').strip()
    if not q:
        return fetch_all("SELECT * FROM products ORDER BY product_name LIMIT %s;", (limit,))

    like = f"%{q}%"
    return fetch_all(
        """
        SELECT *,
            (barcode = %s) AS exact_barcode,
            (sku = %s) AS exact_sku
        FROM products
        WHERE barcode = %s
           OR sku = %s
           OR LOWER(product_name) LIKE LOWER(%s)
           OR LOWER(barcode) LIKE LOWER(%s)
           OR LOWER(sku) LIKE LOWER(%s)
        ORDER BY exact_barcode DESC, exact_sku DESC, product_name
        LIMIT %s;
        """,
        (q, q, q, q, like, like, like, limit),
    )


def update_item(barcode_value, updates):
    query = """
        UPDATE products SET
            product_name = %s,
            barcode = %s,
            sku = %s,
            purchase_price = %s,
            selling_price = %s,
            min_selling_price = %s,
            quantity = %s,
            description = %s,
            photo_link = %s,
            item_condition = %s,
            date_added = NOW()
        WHERE barcode = %s;
    """
    params = (
        updates.get("product_name"),
        updates.get("barcode"),
        updates.get("sku") or None,
        updates.get("purchase_price"),
        updates.get("selling_price"),
        updates.get("min_selling_price"),
        updates.get("quantity"),
        updates.get("description"),
        updates.get("photo_link"),
        updates.get("item_condition") or 'neu',
        barcode_value,
    )
    execute_query(query, params)


def db_delete_item(barcode_value):
    execute_query("DELETE FROM products WHERE barcode = %s;", (barcode_value,))


# --- Sales CRUD ---

def get_sales_for_user(username):
    query = """
        SELECT
            si.sale_id,
            si.product_name,
            si.quantity,
            si.sale_price,
            si.purchase_price,
            si.total_price,
            si.profit,
            s.sale_date
        FROM sale_items si
        JOIN sales s ON si.sale_id = s.sale_id
        WHERE s.username = %s
        ORDER BY s.sale_date DESC
    """
    return fetch_all(query, (username,))


def load_sales():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sale_id as order_id, username as user, sale_date as date, total_sale_price as total_order_price
                FROM sales
                ORDER BY sale_date DESC;
                """
            )
            sales = list(cur.fetchall() or [])

            for sale in sales:
                cur.execute(
                    """
                    SELECT id, product_name, barcode, quantity, sale_price, total_price, profit
                    FROM sale_items WHERE sale_id = %s;
                    """,
                    (sale["order_id"],),
                )
                sale["items"] = list(cur.fetchall() or [])

            return sales
    finally:
        conn.close()


def delete_sales_order(order_id: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sale_items WHERE sale_id = %s;", (order_id,))
            cur.execute("DELETE FROM sales WHERE sale_id = %s;", (order_id,))
        conn.commit()
        return True, f"Verkauf {order_id} gelöscht."
    except Exception as e:
        conn.rollback()
        return False, f"Fehler beim Löschen des Verkaufs: {str(e)}"
    finally:
        conn.close()


def get_sale_item(sale_item_id):
    """Fetch a single line item within a sale, by its own id (sale_items.id)."""
    return fetch_one(
        """
        SELECT si.id, si.sale_id, si.barcode, si.product_name, si.quantity,
               si.sale_price, si.total_price, si.purchase_price, si.profit
        FROM sale_items si
        WHERE si.id = %s;
        """,
        (sale_item_id,),
    )


def update_sale_item(sale_item_id, quantity, sale_price):
    """Update a single sale line item's quantity/unit price.

    Recomputes total_price and profit for the line, keeps the parent sale's
    total_sale_price in sync, and adjusts product stock by the difference
    between the old and new quantity (so correcting a sale doesn't silently
    leave stock counts wrong).
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sale_id, barcode, quantity, purchase_price FROM sale_items WHERE id = %s FOR UPDATE;",
                (sale_item_id,),
            )
            row = cur.fetchone()
            if not row:
                return False, "Verkaufsposition nicht gefunden."

            old_quantity = row["quantity"]
            purchase_price = row["purchase_price"] or 0
            barcode_value = row["barcode"]
            sale_id = row["sale_id"]

            quantity = int(quantity)
            sale_price = float(sale_price)
            if quantity <= 0:
                return False, "Menge muss größer als 0 sein."
            if sale_price < 0:
                return False, "Preis darf nicht negativ sein."

            total_price = round(quantity * sale_price, 2)
            profit = round(total_price - (float(purchase_price) * quantity), 2)

            # Adjust stock: if quantity went up, that's more stock sold (stock
            # decreases further); if it went down, stock is given back.
            qty_delta = quantity - old_quantity
            if barcode_value and qty_delta != 0:
                cur.execute(
                    "SELECT quantity FROM products WHERE barcode = %s FOR UPDATE;",
                    (barcode_value,),
                )
                product = cur.fetchone()
                if product is not None:
                    new_stock = max(0, product["quantity"] - qty_delta)
                    cur.execute(
                        "UPDATE products SET quantity = %s WHERE barcode = %s;",
                        (new_stock, barcode_value),
                    )

            cur.execute(
                """
                UPDATE sale_items
                SET quantity = %s, sale_price = %s, total_price = %s, profit = %s
                WHERE id = %s;
                """,
                (quantity, sale_price, total_price, profit, sale_item_id),
            )

            cur.execute(
                "SELECT COALESCE(SUM(total_price), 0) AS total FROM sale_items WHERE sale_id = %s;",
                (sale_id,),
            )
            new_sale_total = cur.fetchone()["total"]
            cur.execute(
                "UPDATE sales SET total_sale_price = %s WHERE sale_id = %s;",
                (new_sale_total, sale_id),
            )

        conn.commit()
        return True, "Verkaufsposition aktualisiert."
    except Exception as e:
        conn.rollback()
        return False, f"Fehler beim Aktualisieren: {str(e)}"
    finally:
        conn.close()


# --- Orders ---

def get_purchases_for_user(username):
    query = """
        SELECT
            order_number,
            product_name,
            quantity,
            price,
            total_price,
            date,
            `user`
        FROM orders
        WHERE `user` = %s
        ORDER BY date DESC
    """
    return fetch_all(query, (username,))


def get_orders(role=None, username=None, filter_user=None, filter_date=None):
    query = """
        SELECT
            order_number,
            product_name,
            price,
            selling_price,
            min_selling_price,
            quantity,
            description,
            total_price,
            date,
            `user`,
            ref_number
        FROM orders
        WHERE 1=1
    """
    params: List[Any] = []

    if role == "seller":
        query += " AND `user` = %s"
        params.append(username)

    if filter_user and role == "admin":
        query += " AND `user` = %s"
        params.append(filter_user)

    if filter_date:
        # partial match: YYYY-MM-DD%
        query += " AND CAST(date AS CHAR) LIKE %s"
        params.append(f"{filter_date}%")

    query += " ORDER BY date DESC"

    rows = fetch_all(query, tuple(params))

    orders = []
    for row in rows:
        raw_date = row.get("date")
        if isinstance(raw_date, (datetime, date)):
            date_str = raw_date.strftime("%Y-%m-%d")
        else:
            date_str = str(raw_date) if raw_date else ""

        orders.append(
            {
                "order_number": row["order_number"],
                "product_name": row["product_name"],
                "price": float(row["price"]) if row["price"] is not None else 0,
                "selling_price": float(row["selling_price"]) if row["selling_price"] is not None else 0,
                "min_selling_price": float(row["min_selling_price"]) if row["min_selling_price"] is not None else 0,
                "quantity": row["quantity"],
                "description": row["description"],
                "total_price": float(row["total_price"]) if row["total_price"] is not None else 0,
                "date": date_str,
                "user": row["user"],
                "ref_number": row["ref_number"],
            }
        )

    return orders


def add_order(order):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            barcode = order.get("ref_number")
            if not barcode or barcode.strip() == "":
                import uuid

                barcode = str(uuid.uuid4()).replace("-", "")[:12]

            photo_link = f"barcodes/code_barres_{barcode}.png"
            payment_method = (order.get("payment_method") or "cash").strip().lower()
            if payment_method not in PAYMENT_METHODS:
                payment_method = "cash"
            # A "card" order must have already been captured by the physical
            # Stripe Terminal reader — the caller (web form / assistant) is
            # responsible for verifying the PaymentIntent with Stripe before
            # ever calling add_order(); this just persists the id so it can
            # be looked up later, same as sales/debt payments.
            stripe_payment_intent_id = order.get("stripe_payment_intent_id") or None

            cur.execute(
                """
                INSERT INTO orders
                (order_number, product_name, ref_number, description, price, selling_price, min_selling_price,
                 quantity, total_price, date, `user`, barcode, payment_method, stripe_payment_intent_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    barcode,
                    order["product_name"],
                    barcode,
                    order.get("description"),
                    order["price"],
                    order["selling_price"],
                    order["min_selling_price"],
                    order["quantity"],
                    order["total_price"],
                    order["date"],
                    order["user"],
                    barcode,
                    payment_method,
                    stripe_payment_intent_id,
                ),
            )

            cur.execute(
                """
                INSERT INTO products
                (product_name, barcode, purchase_price, selling_price, min_selling_price, quantity, description, seller, date_added, photo_link, item_condition)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    quantity = quantity + VALUES(quantity),
                    selling_price = VALUES(selling_price),
                    min_selling_price = VALUES(min_selling_price),
                    purchase_price = VALUES(purchase_price),
                    description = COALESCE(VALUES(description), description),
                    seller = COALESCE(VALUES(seller), seller),
                    date_added = COALESCE(VALUES(date_added), date_added),
                    photo_link = COALESCE(VALUES(photo_link), photo_link);
                """,
                (
                    order["product_name"],
                    barcode,
                    order["price"],
                    order["selling_price"],
                    order["min_selling_price"],
                    order["quantity"],
                    order.get("description"),
                    order.get("user"),
                    order.get("date"),
                    photo_link,
                    'neu',
                ),
            )

        conn.commit()
        return barcode
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_order(order_number, order_data):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE orders SET
                    product_name=%s,
                    ref_number=%s,
                    description=%s,
                    price=%s,
                    selling_price=%s,
                    min_selling_price=%s,
                    quantity=%s,
                    total_price=%s,
                    date=%s,
                    `user`=%s,
                    barcode=%s
                WHERE order_number=%s;
                """,
                (
                    order_data["product_name"],
                    order_data.get("ref_number"),
                    order_data.get("description"),
                    order_data["price"],
                    order_data["selling_price"],
                    order_data["min_selling_price"],
                    order_data["quantity"],
                    order_data["total_price"],
                    order_data["date"],
                    order_data["user"],
                    order_data.get("barcode"),
                    order_number,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_order(order_number):
    execute_query("DELETE FROM orders WHERE order_number = %s;", (order_number,))


def load_orders():
    """All purchase orders (admin-wide), used on the admin dashboard."""
    return get_orders(role="admin")


# --- Debts (Schulden) ---

def load_debts():
    return fetch_all("SELECT * FROM debts ORDER BY created_at DESC;")


PAYMENT_METHODS = ('cash', 'card')


def generate_debt_reference_number(cur=None) -> str:
    """A short random numeric code (e.g. "482913"), independent from the
    internal debt_id, meant for reading out loud to a client or printing on
    a receipt/contract. Collision odds are tiny (1 in ~900,000) but it still
    retries against the DB to guarantee uniqueness.
    """
    import random

    def _exists(value):
        if cur is not None:
            cur.execute("SELECT 1 FROM debts WHERE reference_number = %s;", (value,))
            return cur.fetchone() is not None
        return fetch_one("SELECT 1 FROM debts WHERE reference_number = %s;", (value,)) is not None

    for _ in range(25):
        candidate = str(random.randint(100000, 999999))
        if not _exists(candidate):
            return candidate
    # Astronomically unlikely fallback: widen to 9 digits.
    return str(random.randint(100000000, 999999999))


def ensure_debts_reference_number_column() -> None:
    """Add debts.reference_number for databases created before this field
    existed, and backfill a unique random number onto every existing debt
    row so nothing is left blank. Same safe/idempotent pattern as the other
    ensure_* migrations in this file.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'debts'
                  AND COLUMN_NAME = 'reference_number';
                """
            )
            if not cur.fetchone():
                cur.execute("ALTER TABLE debts ADD COLUMN reference_number VARCHAR(12) NULL;")
                cur.execute("ALTER TABLE debts ADD UNIQUE INDEX idx_debts_reference_number (reference_number);")

            cur.execute("SELECT debt_id FROM debts WHERE reference_number IS NULL;")
            missing = [row['debt_id'] for row in (cur.fetchall() or [])]
            for debt_id in missing:
                ref = generate_debt_reference_number(cur=cur)
                cur.execute("UPDATE debts SET reference_number = %s WHERE debt_id = %s;", (ref, debt_id))
            conn.commit()
    except Exception:
        conn.rollback()
        logger.exception('Could not ensure debts.reference_number column at startup')
    finally:
        conn.close()


def ensure_debt_payment_support() -> None:
    """Add what's needed for payment-method tracking and a full payment
    history on debts:
      - debts.original_amount — the debt's size when first created. `amount`
        keeps meaning "remaining balance owed" (unchanged for every other
        query in the app that already reads it that way), so a separate
        column is needed to still know the original total once partial
        payments start reducing `amount`.
      - debt_payments — one row per payment (full or partial), with the
        amount actually paid and the method (cash/card).
    Same safe/idempotent/error-swallowing pattern as the other ensure_*
    migrations in this file.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'debts'
                  AND COLUMN_NAME = 'original_amount';
                """
            )
            if not cur.fetchone():
                cur.execute("ALTER TABLE debts ADD COLUMN original_amount DECIMAL(12,2) NULL;")
                # One-time backfill: at the moment this column is added, no
                # partial payments exist yet anywhere (the feature is brand
                # new), so the current `amount` on every existing row IS
                # still the original amount.
                cur.execute("UPDATE debts SET original_amount = amount WHERE original_amount IS NULL;")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS debt_payments (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    debt_id VARCHAR(32) NOT NULL,
                    amount DECIMAL(12,2) NOT NULL,
                    payment_method VARCHAR(20) NOT NULL DEFAULT 'cash',
                    paid_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    recorded_by VARCHAR(255),
                    stripe_payment_intent_id VARCHAR(255) NULL,
                    INDEX idx_debt_payments_debt (debt_id)
                ) ENGINE=InnoDB;
                """
            )

            # A DB created before Stripe Terminal support existed will have
            # this table already, without the column above (CREATE TABLE IF
            # NOT EXISTS is a no-op then) — add it explicitly so upgrades
            # aren't stuck on an old schema.
            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'debt_payments'
                  AND COLUMN_NAME = 'stripe_payment_intent_id';
                """
            )
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE debt_payments ADD COLUMN stripe_payment_intent_id VARCHAR(255) NULL;"
                )

            # Backfill: debts that were already marked paid before this
            # payment-history feature existed have no debt_payments rows.
            # Give each of them one synthetic row (method unknown, so
            # 'cash' by convention, recorded_by flags it as inferred) so
            # "keep a complete payment history" holds for every debt, not
            # just ones paid after this migration. Safe to re-run: only
            # touches debts that still have zero payment rows.
            cur.execute(
                """
                INSERT INTO debt_payments (debt_id, amount, payment_method, paid_at, recorded_by)
                SELECT d.debt_id, COALESCE(d.original_amount, d.amount), 'cash', d.created_at, 'system_backfill'
                FROM debts d
                WHERE d.paid = TRUE
                  AND NOT EXISTS (SELECT 1 FROM debt_payments p WHERE p.debt_id = d.debt_id);
                """
            )
            # Bring those legacy paid debts' `amount` in line with the
            # invariant every other query in the app now relies on
            # (amount == remaining balance, i.e. 0 once fully paid).
            cur.execute("UPDATE debts SET amount = 0 WHERE paid = TRUE AND amount <> 0;")

            conn.commit()
    except Exception:
        conn.rollback()
        logger.exception('Could not ensure debt payment history support (original_amount / debt_payments)')
    finally:
        conn.close()


def list_debt_payments(debt_id):
    """Full payment history for one debt, most recent first."""
    return fetch_all(
        "SELECT * FROM debt_payments WHERE debt_id = %s ORDER BY paid_at DESC, id DESC;",
        (debt_id,),
    )


def get_debt_payment(payment_id):
    return fetch_one("SELECT * FROM debt_payments WHERE id = %s;", (payment_id,))


def record_debt_payment(debt_id, amount, payment_method, recorded_by=None, stripe_payment_intent_id=None):
    """Record a cash/card payment against a debt — full or partial (e.g.
    debt of €50, client pays €30 now, €20 remains owed).

    `debts.amount` is always kept as the live remaining balance and
    `debts.paid` as whether that balance has reached zero — the same two
    fields every other query in the app (client totals, get_summary,
    open-debts list, ...) already reads. Because of that, there is no
    separate "statistics" to keep in sync: as long as this function (and
    edit/delete below) always leaves `amount`/`paid` correct, every report
    that reads them is automatically correct on its very next query.

    Returns the updated debt row. Raises ValueError on invalid input
    (unknown debt, non-positive amount, overpayment, already fully paid,
    unknown payment method) so callers share one consistent error path —
    same convention as _find_open_debt/_update_item_record etc. in app.py.
    """
    payment_method = (payment_method or '').strip().lower()
    if payment_method not in PAYMENT_METHODS:
        raise ValueError(f"payment_method must be one of {PAYMENT_METHODS}")

    debt = fetch_one("SELECT * FROM debts WHERE debt_id = %s;", (debt_id,))
    if not debt:
        raise ValueError(f'no debt found with id "{debt_id}"')
    if debt.get('paid'):
        raise ValueError('this debt is already fully paid')

    remaining = float(debt['amount'])
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise ValueError('amount must be a number')
    if amount <= 0:
        raise ValueError('amount must be greater than 0')
    if amount - remaining > 0.01:  # small float tolerance
        raise ValueError(f'amount (€{amount:.2f}) cannot exceed the remaining balance (€{remaining:.2f})')

    new_remaining = round(remaining - amount, 2)
    now_paid = new_remaining <= 0.01
    if now_paid:
        new_remaining = 0.0

    execute_query(
        "INSERT INTO debt_payments (debt_id, amount, payment_method, paid_at, recorded_by, stripe_payment_intent_id) VALUES (%s, %s, %s, NOW(), %s, %s);",
        (debt_id, amount, payment_method, recorded_by, stripe_payment_intent_id),
    )
    execute_query(
        "UPDATE debts SET amount = %s, paid = %s WHERE debt_id = %s;",
        (new_remaining, now_paid, debt_id),
    )
    return fetch_one("SELECT * FROM debts WHERE debt_id = %s;", (debt_id,))


def edit_debt_payment(payment_id, amount=None, payment_method=None):
    """Edit a previously recorded payment's amount and/or method, then
    recompute the parent debt's remaining balance/paid flag from scratch
    (old payment amount added back, new payment amount subtracted) —
    "recalculate all statistics automatically" for the edit case.
    Returns (updated_payment, updated_debt). Raises ValueError if the new
    amount would take the debt's remaining balance out of the valid
    [0, original_amount] range.
    """
    payment = fetch_one("SELECT * FROM debt_payments WHERE id = %s;", (payment_id,))
    if not payment:
        raise ValueError(f'no payment found with id {payment_id}')
    debt = fetch_one("SELECT * FROM debts WHERE debt_id = %s;", (payment['debt_id'],))
    if not debt:
        raise ValueError(f'the debt for this payment no longer exists')

    new_amount = float(payment['amount']) if amount is None else round(float(amount), 2)
    if new_amount <= 0:
        raise ValueError('amount must be greater than 0')
    new_method = (payment_method or payment['payment_method'] or 'cash').strip().lower()
    if new_method not in PAYMENT_METHODS:
        raise ValueError(f"payment_method must be one of {PAYMENT_METHODS}")

    original_amount = float(debt.get('original_amount') if debt.get('original_amount') is not None else debt['amount'])
    current_remaining = float(debt['amount'])
    # Undo the old payment's effect, then apply the new amount.
    remaining_before_this_payment = round(current_remaining + float(payment['amount']), 2)
    new_remaining = round(remaining_before_this_payment - new_amount, 2)
    if new_remaining < -0.01:
        raise ValueError(f'this amount (€{new_amount:.2f}) would overpay the debt (only €{remaining_before_this_payment:.2f} was owed at this point)')
    if new_remaining > original_amount + 0.01:
        raise ValueError('this amount is larger than the original debt')
    new_remaining = max(0.0, new_remaining)
    now_paid = new_remaining <= 0.01
    if now_paid:
        new_remaining = 0.0

    execute_query(
        "UPDATE debt_payments SET amount = %s, payment_method = %s WHERE id = %s;",
        (new_amount, new_method, payment_id),
    )
    execute_query(
        "UPDATE debts SET amount = %s, paid = %s WHERE debt_id = %s;",
        (new_remaining, now_paid, payment['debt_id']),
    )
    return (
        fetch_one("SELECT * FROM debt_payments WHERE id = %s;", (payment_id,)),
        fetch_one("SELECT * FROM debts WHERE debt_id = %s;", (payment['debt_id'],)),
    )


def delete_debt_payment(payment_id):
    """Delete a payment and add its amount back onto the debt's remaining
    balance (un-marking it paid if it had reached zero) — "recalculate all
    statistics automatically" for the delete case. Returns the updated
    debt row.
    """
    payment = fetch_one("SELECT * FROM debt_payments WHERE id = %s;", (payment_id,))
    if not payment:
        raise ValueError(f'no payment found with id {payment_id}')
    debt = fetch_one("SELECT * FROM debts WHERE debt_id = %s;", (payment['debt_id'],))
    if not debt:
        raise ValueError('the debt for this payment no longer exists')

    original_amount = float(debt.get('original_amount') if debt.get('original_amount') is not None else debt['amount'])
    new_remaining = round(float(debt['amount']) + float(payment['amount']), 2)
    new_remaining = min(new_remaining, original_amount)  # guard against float drift
    now_paid = new_remaining <= 0.01
    if now_paid:
        new_remaining = 0.0

    execute_query("DELETE FROM debt_payments WHERE id = %s;", (payment_id,))
    execute_query(
        "UPDATE debts SET amount = %s, paid = %s WHERE debt_id = %s;",
        (new_remaining, now_paid, payment['debt_id']),
    )
    return fetch_one("SELECT * FROM debts WHERE debt_id = %s;", (payment['debt_id'],))


# --- Factures (invoice / bill management: eBay, electricity, internet, suppliers, ...) ---

FACTURE_TYPES = {
    "ebay": "eBay",
    "electricity": "Strom / Électricité",
    "water": "Wasser / Eau",
    "internet": "Internet",
    "phone": "Telefon / Téléphone",
    "supplier": "Lieferant / Fournisseur",
    "rent": "Miete / Loyer",
    "other": "Sonstiges / Autre",
}


def get_factures(facture_type=None, status=None, search=None, limit=None, offset=None):
    """`search` filters by issuer (case-insensitive substring). `limit`/`offset`
    page the result set — needed once the factures table holds thousands+ rows,
    so callers (like the assistant widget) aren't forced to pull everything
    just to show a handful at a time."""
    query = "SELECT * FROM factures WHERE 1=1"
    params = []
    if facture_type:
        query += " AND facture_type = %s"
        params.append(facture_type)
    if status:
        query += " AND status = %s"
        params.append(status)
    if search:
        query += " AND issuer ILIKE %s"
        params.append(f"%{search}%")
    query += " ORDER BY issue_date DESC, id DESC"
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
        if offset:
            query += " OFFSET %s"
            params.append(offset)
    query += ";"
    return fetch_all(query, tuple(params))


def count_factures(facture_type=None, status=None, search=None):
    """Cheap COUNT(*) counterpart to get_factures's filters, so the caller
    can show 'N of TOTAL' and know when to stop paging without ever fetching
    the full row set."""
    query = "SELECT COUNT(*) AS cnt FROM factures WHERE 1=1"
    params = []
    if facture_type:
        query += " AND facture_type = %s"
        params.append(facture_type)
    if status:
        query += " AND status = %s"
        params.append(status)
    if search:
        query += " AND issuer ILIKE %s"
        params.append(f"%{search}%")
    query += ";"
    row = fetch_one(query, tuple(params))
    return int(row["cnt"] or 0)


def get_facture(facture_id):
    return fetch_one("SELECT * FROM factures WHERE id = %s;", (facture_id,))


def add_facture(data):
    return execute_query(
        """
        INSERT INTO factures
            (facture_type, reference, issuer, amount, currency, issue_date, due_date, status, notes, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """,
        (
            data.get("facture_type", "other"),
            data.get("reference"),
            data["issuer"],
            data["amount"],
            data.get("currency", "EUR"),
            data["issue_date"],
            data.get("due_date"),
            data.get("status", "unpaid"),
            data.get("notes"),
            data.get("created_by"),
        ),
    )


def update_facture(facture_id, data):
    execute_query(
        """
        UPDATE factures
        SET facture_type = %s, reference = %s, issuer = %s, amount = %s, currency = %s,
            issue_date = %s, due_date = %s, status = %s, notes = %s
        WHERE id = %s;
        """,
        (
            data.get("facture_type", "other"),
            data.get("reference"),
            data["issuer"],
            data["amount"],
            data.get("currency", "EUR"),
            data["issue_date"],
            data.get("due_date"),
            data.get("status", "unpaid"),
            data.get("notes"),
            facture_id,
        ),
    )


def delete_facture(facture_id):
    execute_query("DELETE FROM factures WHERE id = %s;", (facture_id,))


def get_facture_summary():
    total = fetch_one("SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total FROM factures;")
    unpaid = fetch_one("SELECT COALESCE(SUM(amount), 0) AS total FROM factures WHERE status = 'unpaid';")
    paid = fetch_one("SELECT COALESCE(SUM(amount), 0) AS total FROM factures WHERE status = 'paid';")
    return {
        "count": int(total["cnt"] or 0),
        "total_amount": round(float(total["total"] or 0), 2),
        "unpaid_amount": round(float(unpaid["total"] or 0), 2),
        "paid_amount": round(float(paid["total"] or 0), 2),
    }


# --- Salary ---

def insert_salary_payment(record):
    execute_query(
        """
        INSERT INTO salaries (employee, amount, source, note, payment_date)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            record["employee"],
            record["amount"],
            record["source"],
            record["note"],
            record["payment_date"],
        ),
    )


def load_salary_payments():
    return fetch_all(
        """
        SELECT id, employee, amount, source, note, payment_date
        FROM salaries
        ORDER BY payment_date DESC;
        """
    )


def get_salary_payment(payment_id):
    return fetch_one(
        "SELECT id, employee, amount, source, note, payment_date FROM salaries WHERE id = %s;",
        (payment_id,),
    )


def update_salary_payment(payment_id, amount, source, note):
    execute_query(
        "UPDATE salaries SET amount = %s, source = %s, note = %s WHERE id = %s;",
        (amount, source, note, payment_id),
    )


def delete_salary_payment(payment_id):
    execute_query("DELETE FROM salaries WHERE id = %s;", (payment_id,))


# --- Kasse / Cash transactions ---

def ensure_cash_transactions_payment_method_column() -> None:
    """Add `payment_method` (cash/card) to `cash_transactions` if an older
    DB predates it — same idea as debts.payment_method: a Kasse withdrawal
    or deposit isn't always physical cash (e.g. a business-card expense
    logged through the same register ledger), and the dashboard needs to
    tell Cash Withdrawals apart from Card Withdrawals. Existing rows
    default to 'cash', which is accurate for every row recorded before
    this feature existed (the register was cash-only until now).
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'cash_transactions'
                  AND COLUMN_NAME = 'payment_method';
                """
            )
            if not cur.fetchone():
                cur.execute("ALTER TABLE cash_transactions ADD COLUMN payment_method VARCHAR(20) NOT NULL DEFAULT 'cash';")
            conn.commit()
    except Exception:
        conn.rollback()
        logger.exception('Could not ensure cash_transactions.payment_method column')
    finally:
        conn.close()


def get_cash_transaction(transaction_id):
    return fetch_one(
        "SELECT id, date, amount, type, description, username, payment_method FROM cash_transactions WHERE id = %s;",
        (transaction_id,),
    )


def update_cash_transaction(transaction_id, amount, typ, description, payment_method=None):
    """Update a cash register entry. `amount` is stored as a positive number;
    `typ` ('einzahlung'/'auszahlung') carries the sign in every balance calc.
    `payment_method` ('cash'/'card') defaults to leaving it unchanged."""
    payment_method = (payment_method or '').strip().lower()
    if payment_method not in PAYMENT_METHODS:
        payment_method = None
    if payment_method is None:
        execute_query(
            "UPDATE cash_transactions SET amount = %s, type = %s, description = %s WHERE id = %s;",
            (amount, typ, description, transaction_id),
        )
    else:
        execute_query(
            "UPDATE cash_transactions SET amount = %s, type = %s, description = %s, payment_method = %s WHERE id = %s;",
            (amount, typ, description, payment_method, transaction_id),
        )


def get_kasse_balance_for_date(dt):
    if isinstance(dt, datetime):
        dt = dt.date()

    result = fetch_one(
        "SELECT closing_balance FROM daily_cash_balance WHERE date = %s",
        (dt,),
    )
    if result and result.get("closing_balance") is not None:
        return float(result["closing_balance"])
    return None


def calculate_sales_for_date(dt):
    """Cash sales only — Kasse is the physical cash drawer, so a card sale
    never touches it even though it's still real revenue elsewhere (reports,
    dashboards). Rows from before the `sales.payment_method` column existed
    have it as NULL; those are treated as cash (that was the only option
    back then) rather than silently dropped from historical balances."""
    if isinstance(dt, datetime):
        dt = dt.date()

    start_datetime = datetime.combine(dt, datetime.min.time())
    end_datetime = datetime.combine(dt, datetime.max.time())

    result = fetch_one(
        """
        SELECT COALESCE(SUM(si.total_price), 0) AS total
        FROM sale_items si
        JOIN sales s ON si.sale_id = s.sale_id
        WHERE s.sale_date >= %s AND s.sale_date <= %s
          AND (s.payment_method = 'cash' OR s.payment_method IS NULL);
        """,
        (start_datetime, end_datetime),
    )
    return round(float(result["total"] or 0), 2)


def calculate_purchases_for_date(dt):
    """Cash purchases only — same reasoning as calculate_sales_for_date:
    a purchase paid by card doesn't reduce the physical cash on hand."""
    if isinstance(dt, datetime):
        dt = dt.date()

    result = fetch_one(
        """
        SELECT COALESCE(SUM(total_price), 0) AS total
        FROM orders
        WHERE CAST(date AS DATE) = %s
          AND (payment_method = 'cash' OR payment_method IS NULL);
        """,
        (dt,),
    )
    return round(float(result["total"] or 0), 2)


def calculate_cash_debt_payments_for_date(dt):
    """Cash collected today against open debts — money physically handed
    over the counter, so it belongs in the Kasse balance. Card-paid debt
    settlements don't (the money never sat in the drawer)."""
    if isinstance(dt, datetime):
        dt = dt.date()

    result = fetch_one(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM debt_payments
        WHERE DATE(paid_at) = %s AND payment_method = 'cash';
        """,
        (dt,),
    )
    return round(float(result["total"] or 0), 2)


def calculate_cash_salary_payments_for_date(dt):
    """Salaries paid out of the cash drawer today (source = 'Kasse') — the
    other option, 'Privat' (paid from the owner's own pocket), never
    touches Kasse at all, so it's excluded here."""
    if isinstance(dt, datetime):
        dt = dt.date()

    result = fetch_one(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM salaries
        WHERE DATE(payment_date) = %s AND LOWER(source) = 'kasse';
        """,
        (dt,),
    )
    return round(float(result["total"] or 0), 2)


def calculate_cash_deposits_for_date(dt):
    """Manual cash deposits (Kasse 'Einzahlung') booked on this date — cash
    only, same rule as everywhere else in Kasse (a card-tagged manual entry
    never touches the physical drawer)."""
    if isinstance(dt, datetime):
        dt = dt.date()
    result = fetch_one(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM cash_transactions
        WHERE DATE(date) = %s AND type = 'einzahlung'
          AND COALESCE(payment_method, 'cash') = 'cash';
        """,
        (dt,),
    )
    return round(float(result["total"] or 0), 2)


def calculate_cash_withdrawals_for_date(dt):
    """Manual cash withdrawals (Kasse 'Auszahlung') booked on this date."""
    if isinstance(dt, datetime):
        dt = dt.date()
    result = fetch_one(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM cash_transactions
        WHERE DATE(date) = %s AND type = 'auszahlung'
          AND COALESCE(payment_method, 'cash') = 'cash';
        """,
        (dt,),
    )
    return round(float(result["total"] or 0), 2)


def save_kasse_balance_for_date(dt, balance):
    if isinstance(dt, datetime):
        dt = dt.date()

    execute_query(
        """
        INSERT INTO daily_cash_balance (date, closing_balance)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE closing_balance = VALUES(closing_balance);
        """,
        (dt, balance),
    )


def get_latest_balance_date():
    result = fetch_one("SELECT MAX(date) AS last_date FROM daily_cash_balance")
    return result["last_date"] if result else None


def calculate_and_save_today_closing_balance():
    last_date = get_latest_balance_date()
    today = datetime.now().date()

    if last_date is None:
        last_date = today - timedelta(days=1)

    if isinstance(last_date, datetime):
        last_date = last_date.date()

    current_date = last_date + timedelta(days=1)

    while current_date <= today:
        if get_kasse_balance_for_date(current_date) is None:
            prev_balance = get_kasse_balance_for_date(current_date - timedelta(days=1)) or 0
            sales = calculate_sales_for_date(current_date)
            purchases = calculate_purchases_for_date(current_date)
            debt_cash_in = calculate_cash_debt_payments_for_date(current_date)
            salary_cash_out = calculate_cash_salary_payments_for_date(current_date)
            # Manual cash deposits/withdrawals (the chat/Kasse page's "Record
            # a deposit/withdrawal" action) were previously missing from
            # this rolling total entirely — see calculate_cash_deposits_for_date.
            cash_deposits = calculate_cash_deposits_for_date(current_date)
            cash_withdrawals = calculate_cash_withdrawals_for_date(current_date)
            balance = round(
                prev_balance + sales - purchases + debt_cash_in - salary_cash_out
                + cash_deposits - cash_withdrawals,
                2,
            )
            save_kasse_balance_for_date(current_date, balance)
            logger.info(f"Saved balance for {current_date}: {balance}")
        current_date += timedelta(days=1)


# --- Sales / profit summaries used by the dashboards ---

def calculate_today_sales(username: Optional[str] = None) -> float:
    """Total sale revenue for today, optionally scoped to one seller."""
    query = """
        SELECT COALESCE(SUM(si.total_price), 0) AS total
        FROM sale_items si
        JOIN sales s ON si.sale_id = s.sale_id
        WHERE DATE(s.sale_date) = CURDATE()
    """
    params: List[Any] = []
    if username:
        query += " AND s.username = %s"
        params.append(username)
    result = fetch_one(query, tuple(params))
    return round(float(result["total"] or 0), 2)


def calculate_today_purchases(username: Optional[str] = None) -> float:
    """Total purchase (stock-in) cost for today, optionally scoped to one user."""
    query = "SELECT COALESCE(SUM(total_price), 0) AS total FROM orders WHERE DATE(`date`) = CURDATE()"
    params: List[Any] = []
    if username:
        query += " AND `user` = %s"
        params.append(username)
    result = fetch_one(query, tuple(params))
    return round(float(result["total"] or 0), 2)


def calculate_today_profit() -> float:
    """Total profit (sale price - purchase price) for all sales made today."""
    result = fetch_one(
        """
        SELECT COALESCE(SUM(si.profit), 0) AS total
        FROM sale_items si
        JOIN sales s ON si.sale_id = s.sale_id
        WHERE DATE(s.sale_date) = CURDATE();
        """
    )
    return round(float(result["total"] or 0), 2)


def get_dashboard_stats() -> dict:
    """Everything the live dashboard cards need, computed fresh on every
    call with exactly 6 SQL queries total — one aggregate query per
    underlying table, using conditional (CASE WHEN) aggregation to get
    several numbers per query instead of issuing a separate query per
    card. There is no caching layer here, so every call reflects the
    database as it stands *right now*: any create/update/delete anywhere
    in the app is automatically reflected the next time this is called —
    "live" comes from always computing fresh, not from invalidating a
    cache.

    Where an equally efficient single-purpose helper already existed
    (calculate_today_purchases), it's reused rather than re-implemented,
    per "avoid duplicate database queries."
    """
    today = datetime.now().date()

    # 1) Sales + profit for today — one query, not two separate round
    #    trips over the same sale_items/sales rows.
    sales_row = fetch_one(
        """
        SELECT COALESCE(SUM(si.total_price), 0) AS today_sales,
               COALESCE(SUM(si.profit), 0) AS today_profit
        FROM sale_items si
        JOIN sales s ON si.sale_id = s.sale_id
        WHERE DATE(s.sale_date) = %s;
        """,
        (today,),
    )

    # 2) Purchases for today — reuses the existing single-query helper
    #    instead of duplicating its SQL here.
    today_purchases = calculate_today_purchases()

    # 3) Inventory value + low-stock count — one query over `products`.
    inventory_row = fetch_one(
        """
        SELECT COALESCE(SUM(purchase_price * quantity), 0) AS inventory_value,
               COALESCE(SUM(CASE WHEN quantity <= 5 THEN 1 ELSE 0 END), 0) AS low_stock_count
        FROM products;
        """
    )

    # 4) Outstanding debt — `amount` is always the live remaining balance
    #    (see ensure_debt_payment_support), so this is a direct sum.
    debts_row = fetch_one("SELECT COALESCE(SUM(amount), 0) AS outstanding FROM debts;")

    # 5) Paid debt (all-time) + today's cash/card debt payments — one
    #    query over `debt_payments` using conditional aggregation for the
    #    two "today, by method" breakdowns alongside the lifetime total.
    debt_payments_row = fetch_one(
        """
        SELECT
            COALESCE(SUM(amount), 0) AS paid_total,
            COALESCE(SUM(CASE WHEN DATE(paid_at) = %s AND payment_method = 'cash' THEN amount ELSE 0 END), 0) AS cash_today,
            COALESCE(SUM(CASE WHEN DATE(paid_at) = %s AND payment_method = 'card' THEN amount ELSE 0 END), 0) AS card_today
        FROM debt_payments;
        """,
        (today, today),
    )

    # 6) Today's withdrawals by method — one query over `cash_transactions`.
    withdrawals_row = fetch_one(
        """
        SELECT
            COALESCE(SUM(CASE WHEN payment_method = 'cash' THEN amount ELSE 0 END), 0) AS cash_today,
            COALESCE(SUM(CASE WHEN payment_method = 'card' THEN amount ELSE 0 END), 0) AS card_today
        FROM cash_transactions
        WHERE type = 'auszahlung' AND DATE(date) = %s;
        """,
        (today,),
    )

    return {
        'today_sales': round(float(sales_row['today_sales'] or 0), 2),
        'today_profit': round(float(sales_row['today_profit'] or 0), 2),
        'today_purchases': today_purchases,
        'inventory_value': round(float(inventory_row['inventory_value'] or 0), 2),
        'low_stock_count': int(inventory_row['low_stock_count'] or 0),
        'outstanding_debt': round(float(debts_row['outstanding'] or 0), 2),
        'paid_debt': round(float(debt_payments_row['paid_total'] or 0), 2),
        'cash_debt_payments_today': round(float(debt_payments_row['cash_today'] or 0), 2),
        'card_debt_payments_today': round(float(debt_payments_row['card_today'] or 0), 2),
        'cash_withdrawals_today': round(float(withdrawals_row['cash_today'] or 0), 2),
        'card_withdrawals_today': round(float(withdrawals_row['card_today'] or 0), 2),
    }


def backfill_sale_item_profits() -> int:
    """Repair historical sale_items rows whose `profit` column was never set.

    Every place that inserts into sale_items used to omit the `profit`
    column entirely, so it silently kept the table's DEFAULT 0.00 for every
    sale ever recorded — sale_price/purchase_price/quantity were always
    correct, but "Gewinn" (profit) showed up as 0 everywhere (dashboards,
    reports, the assistant chat) regardless of how the sale was made. The
    INSERTs are now fixed, but existing rows still need a one-time repair.
    This recomputes profit = total_price - purchase_price * quantity for any
    row still sitting at 0/NULL. It's a pure function of other columns, so
    it's safe to call on every app startup.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sale_items
                SET profit = ROUND(total_price - COALESCE(purchase_price, 0) * quantity, 2)
                WHERE profit = 0 OR profit IS NULL;
                """
            )
            affected = cur.rowcount
            conn.commit()
            return affected
    except Exception:
        conn.rollback()
        logger.exception('Failed to backfill sale_items.profit')
        return 0
    finally:
        conn.close()


def ensure_sales_extra_columns() -> None:
    """Add customer_name/payment_method to `sales` if an older DB predates them.

    Needed for the AI-first chat checkout flow (which now asks who the sale
    is for and cash-or-card before executing quick_sell). Safe to call on
    every startup: it only issues the ALTER when the column is actually
    missing, and swallows errors so two gunicorn workers booting at once
    can't crash each other.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sales'
                  AND COLUMN_NAME IN ('customer_name', 'payment_method');
                """
            )
            existing = {row['COLUMN_NAME'] for row in cur.fetchall()}
            if 'customer_name' not in existing:
                cur.execute("ALTER TABLE sales ADD COLUMN customer_name VARCHAR(255) NULL;")
            if 'payment_method' not in existing:
                cur.execute("ALTER TABLE sales ADD COLUMN payment_method VARCHAR(20) NULL;")
            conn.commit()
    except Exception:
        conn.rollback()
        logger.exception('Could not ensure sales.customer_name/payment_method columns')
    finally:
        conn.close()


def ensure_sales_stripe_payment_intent_column() -> None:
    """Add `stripe_payment_intent_id` to `sales` if an older DB predates it.

    Card sales taken at the register go through a physical Stripe Terminal
    reader; the resulting PaymentIntent id is stored on the sale so it can
    be looked up / refunded later from the Stripe Dashboard. Safe to call
    on every startup: only issues the ALTER when the column is missing.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sales'
                  AND COLUMN_NAME = 'stripe_payment_intent_id';
                """
            )
            existing = {row['COLUMN_NAME'] for row in cur.fetchall()}
            if 'stripe_payment_intent_id' not in existing:
                cur.execute(
                    "ALTER TABLE sales ADD COLUMN stripe_payment_intent_id VARCHAR(255) NULL;"
                )
            conn.commit()
    except Exception:
        conn.rollback()
        logger.exception('Could not ensure sales.stripe_payment_intent_id column')
    finally:
        conn.close()


def ensure_orders_payment_method_column() -> None:
    """Add `payment_method` (cash/card) to `orders` (purchases/Einkauf) if
    an older DB predates it. Same idea as sales.payment_method: only a cash
    purchase should reduce the physical Kasse balance, so purchases need
    this too — previously there was no way to tell them apart at all, and
    every purchase silently reduced Kasse regardless of how it was paid.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders'
                  AND COLUMN_NAME = 'payment_method';
                """
            )
            existing = {row['COLUMN_NAME'] for row in cur.fetchall()}
            if 'payment_method' not in existing:
                cur.execute("ALTER TABLE orders ADD COLUMN payment_method VARCHAR(20) NULL;")
            conn.commit()
    except Exception:
        conn.rollback()
        logger.exception('Could not ensure orders.payment_method column')
    finally:
        conn.close()


def ensure_orders_stripe_payment_intent_column() -> None:
    """Add `stripe_payment_intent_id` to `orders` if an older DB predates it.

    Purchase orders (Einkauf) paid by card must go through the same physical
    Stripe Terminal reader as sales/debt payments — the resulting
    PaymentIntent id is stored on the order so it can be looked up / refunded
    later from the Stripe Dashboard. Safe to call on every startup: only
    issues the ALTER when the column is missing.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders'
                  AND COLUMN_NAME = 'stripe_payment_intent_id';
                """
            )
            existing = {row['COLUMN_NAME'] for row in cur.fetchall()}
            if 'stripe_payment_intent_id' not in existing:
                cur.execute(
                    "ALTER TABLE orders ADD COLUMN stripe_payment_intent_id VARCHAR(255) NULL;"
                )
            conn.commit()
    except Exception:
        conn.rollback()
        logger.exception('Could not ensure orders.stripe_payment_intent_id column')
    finally:
        conn.close()


def calculate_monthly_sales() -> float:
    """Total sale revenue for the current calendar month."""
    result = fetch_one(
        """
        SELECT COALESCE(SUM(si.total_price), 0) AS total
        FROM sale_items si
        JOIN sales s ON si.sale_id = s.sale_id
        WHERE YEAR(s.sale_date) = YEAR(CURDATE()) AND MONTH(s.sale_date) = MONTH(CURDATE());
        """
    )
    return round(float(result["total"] or 0), 2)


# --- Warehouse notifications ---

def ensure_audit_log_module_column() -> None:
    """Add a `module` column to `audit_log` if an older DB predates it.

    `entity` already names the underlying record type (e.g. 'sale',
    'debt') but was also being reused, for AI-assistant actions, to hold
    the raw tool name (e.g. 'add_debt') instead — an inconsistency that
    made the log hard to filter/scan. `module` is a clean, separate
    business-domain tag ('sales', 'inventory', 'debts', 'cash', ...) that
    is always populated the same way regardless of source. Same safe,
    idempotent, error-swallowing pattern as the other ensure_* migrations
    in this file.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'audit_log'
                  AND COLUMN_NAME = 'module';
                """
            )
            if not cur.fetchone():
                cur.execute("ALTER TABLE audit_log ADD COLUMN module VARCHAR(50) NULL;")
            conn.commit()
    except Exception:
        conn.rollback()
        logger.exception('Could not ensure audit_log.module column')
    finally:
        conn.close()


# Fallback used when a call site doesn't pass module= explicitly (older
# call sites, or ones where the entity alone already says it clearly).
# Keyed by entity name, which — with the assistant_tool renaming below —
# is always the real record type now, never a raw tool name.
_ENTITY_MODULE_MAP = {
    'sale': 'sales', 'sale_item': 'sales',
    'order': 'purchases',
    'item': 'inventory', 'product': 'inventory',
    'seller': 'sellers',
    'client': 'customers', 'debt': 'debts',
    'cash_transaction': 'cash', 'kasse_balance': 'cash',
    'salary_payment': 'salaries',
    'facture': 'invoices',
    'user': 'auth', 'session': 'auth',
    'chat': 'assistant',
}


def format_money(value) -> str:
    try:
        return f'€{float(value):,.2f}'
    except (TypeError, ValueError):
        return f'€{value}'


def format_sale_details(product_name, quantity, barcode=None, total_price=None,
                         payment_method=None, customer_name=None) -> str:
    """'Sold 1 × iPhone / Barcode: 4325 / Total: €44 / Payment: Cash'-style
    summary, reused by every place a sale gets created or edited — the web
    checkout, the AI assistant's quick_sell, and the external sell API —
    so the activity log reads identically no matter where the sale came
    from."""
    lines = [f'Sold {quantity} × {product_name}']
    if barcode:
        lines.append(f'Barcode: {barcode}')
    if total_price is not None:
        lines.append(f'Total: {format_money(total_price)}')
    lines.append(f"Payment: {(payment_method or 'unspecified').title()}")
    if customer_name:
        lines.append(f'Customer: {customer_name}')
    return '\n'.join(lines)


def format_product_details(product_name, barcode=None, sku=None, quantity=None,
                            selling_price=None) -> str:
    lines = [product_name or 'Unnamed product']
    if barcode:
        lines.append(f'Barcode: {barcode}')
    if sku:
        lines.append(f'SKU: {sku}')
    if quantity is not None:
        lines.append(f'Quantity: {quantity}')
    if selling_price is not None:
        lines.append(f'Selling price: {format_money(selling_price)}')
    return '\n'.join(lines)


def format_debt_payment_details(client_name, amount, payment_method, remaining=None, original_amount=None) -> str:
    lines = [f'{client_name or "Unknown customer"} paid {format_money(amount)} via {(payment_method or "cash").title()}']
    if original_amount is not None:
        lines.append(f'Debt: {format_money(original_amount)}')
    if remaining is not None:
        lines.append(f'Remaining: {format_money(remaining)}' if remaining > 0.01 else 'Fully paid')
    return '\n'.join(lines)


def format_debt_details(client_name, amount=None, description=None, phone_number=None) -> str:
    lines = [client_name or 'Unknown customer']
    if amount is not None:
        lines.append(f'Amount: {format_money(amount)}')
    if description:
        lines.append(f'Note: {description}')
    if phone_number:
        lines.append(f'Phone: {phone_number}')
    return '\n'.join(lines)


def format_seller_details(username, salary=None) -> str:
    lines = [username or 'Unknown seller']
    if salary is not None:
        lines.append(f'Salary: {format_money(salary)}')
    return '\n'.join(lines)


def format_kasse_details(kind, amount, description=None) -> str:
    label = 'Deposit' if kind == 'einzahlung' else 'Withdrawal' if kind == 'auszahlung' else (kind or 'Transaction').title()
    lines = [f'{label}: {format_money(amount)}']
    if description:
        lines.append(f'Note: {description}')
    return '\n'.join(lines)


def format_salary_details(username, amount) -> str:
    return f'Paid {format_money(amount)} salary to {username}'


def format_purchase_details(product_name, quantity=None, purchase_price=None, supplier=None) -> str:
    lines = [product_name or 'Unnamed product']
    if quantity is not None:
        lines.append(f'Quantity: {quantity}')
    if purchase_price is not None:
        lines.append(f'Purchase price: {format_money(purchase_price)}')
    if supplier:
        lines.append(f'Supplier: {supplier}')
    return '\n'.join(lines)


def format_invoice_details(issuer, amount=None, facture_type=None, status=None) -> str:
    lines = [issuer or 'Unknown issuer']
    if facture_type:
        lines.append(f'Type: {facture_type}')
    if amount is not None:
        lines.append(f'Amount: {format_money(amount)}')
    if status:
        lines.append(f'Status: {status}')
    return '\n'.join(lines)


def _humanize_dict(d: dict) -> str:
    """Generic fallback for the handful of call sites that don't have a
    bespoke formatter above: still turns a payload into readable
    "Label: value" lines instead of a raw JSON blob."""
    lines = []
    for key, value in d.items():
        if value is None or value == '':
            continue
        label = key.replace('_', ' ').strip().capitalize()
        if isinstance(value, float) or (key.endswith(('price', 'amount', 'total', 'balance')) and isinstance(value, (int, float))):
            value = format_money(value)
        lines.append(f'{label}: {value}')
    return '\n'.join(lines) if lines else ''


def log_audit(action, entity, entity_id=None, details=None, actor=None, source='web', module=None):
    """Record an action for the audit trail.

    `details` may be:
      - a plain string — already human-readable, stored as-is (the
        preferred path; see format_*_details() helpers above), or
      - a dict — converted to readable "Label: value" lines via
        _humanize_dict() rather than stored as raw JSON.

    `module` tags the business domain (sales/inventory/debts/cash/...);
    if not given explicitly it's derived from `entity` via
    _ENTITY_MODULE_MAP so older call sites don't all need updating at once.

    Deliberately swallows errors instead of raising: this runs on the
    write-path of every admin action and the chat assistant, and a missing
    audit_log table (e.g. before re-running seed_db.py after an upgrade)
    must never break the actual operation being audited.
    """
    try:
        if isinstance(details, dict):
            details_text = _humanize_dict(details)
        else:
            details_text = details
        resolved_module = module or _ENTITY_MODULE_MAP.get(entity, entity)
        execute_query(
            """
            INSERT INTO audit_log (actor, action, entity, entity_id, details, source, module)
            VALUES (%s, %s, %s, %s, %s, %s, %s);
            """,
            (actor, action, entity, str(entity_id) if entity_id is not None else None,
             details_text or None, source, resolved_module),
        )
    except Exception:
        logger.exception('audit log write failed (non-fatal)')


def get_audit_log(limit=200, entity=None, module=None):
    conditions = []
    params = []
    if entity:
        conditions.append("entity = %s")
        params.append(entity)
    if module:
        conditions.append("module = %s")
        params.append(module)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    return fetch_all(f"SELECT * FROM audit_log {where} ORDER BY created_at DESC LIMIT %s;", tuple(params))


def get_low_stock_notifications(items, threshold: int = 5):
    """Build dashboard notifications for products running low on stock."""
    notes = []
    for item in items:
        quantity = item.get("quantity", 0) or 0
        if quantity <= threshold:
            notes.append({
                "type": "warning",
                "barcode": item.get("barcode"),
                "message": f"Nur noch {quantity} Stück von '{item.get('product_name', 'Produkt')}' auf Lager.",
            })
    return notes


def get_old_stock_notifications(items, days_old: int = 21):
    """Build dashboard notifications for products that have been sitting in stock too long."""
    notes = []
    today = datetime.now().date()
    for item in items:
        date_added = item.get("date_added")
        if isinstance(date_added, datetime):
            date_added = date_added.date()
        elif isinstance(date_added, str):
            try:
                date_added = datetime.fromisoformat(date_added).date()
            except ValueError:
                date_added = None

        if isinstance(date_added, date) and (today - date_added).days >= days_old:
            notes.append({
                "type": "info",
                "barcode": item.get("barcode"),
                "message": f"'{item.get('product_name', 'Produkt')}' liegt seit {(today - date_added).days} Tagen auf Lager.",
            })
    return notes


# ---------------------------------------------------------------------------
# Assistant chat history (server-side)
# ---------------------------------------------------------------------------
# Chat history used to live ONLY in the browser: the LLM's own memory of the
# conversation was rebuilt from a `history` array the client sent on every
# request, and the on-screen bubbles were cached in localStorage. That means
# a cleared browser, a different device, or "Nicht mehr anzeigen" wiping
# storage lost the whole conversation. This mirrors the approach modern chat
# products (ChatGPT, Claude, etc.) actually use: the server is the source of
# truth for history, keyed by user, so the same conversation follows the
# person across devices/browsers and survives a local storage wipe. The
# client can still keep a local cache for instant rendering, but it now
# reads from here on load instead of purely trusting localStorage.

def save_assistant_message(username: str, role: str, content: str, lang: str = None, conversation_id: str = None):
    """Append one turn (role='user'|'assistant') to a user's assistant chat history,
    scoped to a specific conversation (see list_assistant_conversations)."""
    if not username or not content:
        return
    execute_query(
        "INSERT INTO assistant_chat_history (username, role, content, lang, conversation_id) VALUES (%s, %s, %s, %s, %s);",
        (username, role, content, lang, conversation_id),
    )


def get_assistant_history(username: str, limit: int = 40, conversation_id: str = None):
    """Most recent `limit` turns for this user, oldest first (ready to feed
    straight to the LLM). Scoped to one conversation_id when given; if not,
    falls back to that user's single most recent conversation (keeps this
    working for any caller that predates multi-conversation support)."""
    if not username:
        return []
    if not conversation_id:
        latest = fetch_one(
            "SELECT conversation_id FROM assistant_chat_history WHERE username = %s "
            "AND conversation_id IS NOT NULL ORDER BY created_at DESC LIMIT 1;",
            (username,),
        )
        conversation_id = latest['conversation_id'] if latest else None
    if conversation_id:
        rows = fetch_all(
            """
            SELECT role, content, lang, created_at
            FROM assistant_chat_history
            WHERE username = %s AND conversation_id = %s
            ORDER BY created_at DESC
            LIMIT %s;
            """,
            (username, conversation_id, limit),
        )
    else:
        rows = fetch_all(
            """
            SELECT role, content, lang, created_at
            FROM assistant_chat_history
            WHERE username = %s
            ORDER BY created_at DESC
            LIMIT %s;
            """,
            (username, limit),
        )
    return list(reversed(rows))


def list_assistant_conversations(username: str, limit: int = 30):
    """One row per distinct conversation for this user, most-recently-active
    first — the data behind the chat's GPT-style history sidebar. Title is
    just the first user message of that conversation, trimmed, since these
    conversations were never explicitly named."""
    if not username:
        return []
    rows = fetch_all(
        """
        SELECT conversation_id,
               MIN(created_at) AS started_at,
               MAX(created_at) AS last_message_at,
               COUNT(*) AS message_count
        FROM assistant_chat_history
        WHERE username = %s AND conversation_id IS NOT NULL
        GROUP BY conversation_id
        ORDER BY last_message_at DESC
        LIMIT %s;
        """,
        (username, limit),
    )
    out = []
    for r in rows:
        first_user_msg = fetch_one(
            "SELECT content FROM assistant_chat_history "
            "WHERE username = %s AND conversation_id = %s AND role = 'user' "
            "ORDER BY created_at ASC LIMIT 1;",
            (username, r['conversation_id']),
        )
        title = (first_user_msg['content'] if first_user_msg else '').strip()
        title = (title[:60] + '…') if len(title) > 60 else title
        out.append({
            'conversation_id': r['conversation_id'],
            'title': title or None,
            'started_at': r['started_at'],
            'last_message_at': r['last_message_at'],
            'message_count': r['message_count'],
        })
    return out


def delete_assistant_conversation(username: str, conversation_id: str):
    """Deletes one conversation (all its messages) for this user — the
    sidebar's per-conversation delete action. Scoped to `username` so one
    account can never delete another's conversation by guessing an id."""
    if not username or not conversation_id:
        return
    execute_query(
        "DELETE FROM assistant_chat_history WHERE username = %s AND conversation_id = %s;",
        (username, conversation_id),
    )


def clear_assistant_history(username: str):
    """Wipe a user's stored conversation (used by the chat's 'clear history' action).
    Also drops the rolling summary of that conversation (see
    assistant_chat_summary below) since it summarized turns that no longer
    exist — but NOT the user's durable memory notes (assistant_memory),
    which are meant to survive a "start over" the same way a human
    assistant wouldn't forget your preferences just because you started a
    new conversation.
    """
    if not username:
        return
    execute_query("DELETE FROM assistant_chat_history WHERE username = %s;", (username,))
    execute_query("DELETE FROM assistant_chat_summary WHERE username = %s;", (username,))


def count_assistant_history(username: str) -> int:
    """Total number of stored turns for this user (used to decide when a
    rolling summary needs refreshing)."""
    if not username:
        return 0
    row = fetch_one("SELECT COUNT(*) AS c FROM assistant_chat_history WHERE username = %s;", (username,))
    return int(row['c']) if row else 0


# ---------------------------------------------------------------------------
# Assistant long-term memory + rolling history summary
# ---------------------------------------------------------------------------
# Two different kinds of "the assistant remembers things", kept in separate
# tables because they behave differently:
#
#   assistant_memory — durable facts/preferences the assistant has chosen to
#   remember about this user/shop (e.g. "usually pays suppliers by card",
#   "closes on Sundays"). Small, hand-curated by the AI itself via the
#   remember_note tool, and NOT cleared when the conversation is reset.
#
#   assistant_chat_summary — a compressed summary of the *older* part of a
#   conversation that has scrolled out of the raw window sent to the LLM
#   (see MAX turns in app.py's _run_assistant_chat). Without this, context
#   from more than ~20 turns ago was just silently dropped; now it's
#   compressed instead of lost. Cleared whenever the conversation itself is
#   cleared, since it only makes sense relative to that conversation.
def ensure_assistant_chat_history_conversation_column() -> None:
    """Adds assistant_chat_history.conversation_id, needed for GPT-style
    multiple/named conversations instead of one endless rolling history.
    Existing rows (which predate this column) are backfilled into one
    shared conversation per user, titled from their first message, so
    nothing already stored just disappears from the history list."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'assistant_chat_history'
                  AND COLUMN_NAME = 'conversation_id';
                """
            )
            if not cur.fetchone():
                cur.execute("ALTER TABLE assistant_chat_history ADD COLUMN conversation_id VARCHAR(36) NULL;")
                cur.execute("ALTER TABLE assistant_chat_history ADD INDEX idx_chat_history_conversation (conversation_id);")
                # Backfill: one legacy conversation_id per user, covering all
                # of their pre-existing messages (grouped as a single
                # "Older conversation" entry rather than one row per message).
                cur.execute("SELECT DISTINCT username FROM assistant_chat_history WHERE conversation_id IS NULL;")
                usernames = [row[0] for row in cur.fetchall()]
                for uname in usernames:
                    legacy_id = str(uuid.uuid4())
                    cur.execute(
                        "UPDATE assistant_chat_history SET conversation_id = %s WHERE username = %s AND conversation_id IS NULL;",
                        (legacy_id, uname),
                    )
            conn.commit()
    except Exception:
        conn.rollback()
        logger.exception('Could not ensure assistant_chat_history.conversation_id column')
    finally:
        conn.close()


def ensure_assistant_memory_tables() -> None:
    """Create the tables above if they don't exist yet. Safe/idempotent —
    same pattern as the other ensure_* migrations in this file."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_memory (
                    username VARCHAR(255) PRIMARY KEY,
                    notes TEXT NOT NULL,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB;
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_chat_summary (
                    username VARCHAR(255) PRIMARY KEY,
                    summary TEXT NOT NULL,
                    covered_turns INT NOT NULL DEFAULT 0,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB;
                """
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_assistant_memory(username: str) -> str:
    """This user's durable memory notes (empty string if none yet)."""
    if not username:
        return ''
    row = fetch_one("SELECT notes FROM assistant_memory WHERE username = %s;", (username,))
    return row['notes'] if row else ''


def save_assistant_memory(username: str, notes: str) -> None:
    """Overwrite this user's memory notes with the given text (the caller
    is responsible for merging with any existing notes first — see the
    remember_note tool in app.py)."""
    if not username:
        return
    notes = (notes or '').strip()
    if not notes:
        return
    execute_query(
        """
        INSERT INTO assistant_memory (username, notes) VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE notes = VALUES(notes), updated_at = CURRENT_TIMESTAMP;
        """,
        (username, notes[:4000]),
    )


def clear_assistant_memory(username: str) -> None:
    """Wipe a user's durable memory notes entirely (a deliberate 'forget
    what you know about me' action, distinct from just clearing chat
    history)."""
    if not username:
        return
    execute_query("DELETE FROM assistant_memory WHERE username = %s;", (username,))


def get_assistant_summary(username: str):
    """(summary_text, covered_turns) for this user's rolling history
    summary, or (None, 0) if none exists yet."""
    if not username:
        return None, 0
    row = fetch_one(
        "SELECT summary, covered_turns FROM assistant_chat_summary WHERE username = %s;",
        (username,),
    )
    if not row:
        return None, 0
    return row['summary'], int(row['covered_turns'] or 0)


def save_assistant_summary(username: str, summary: str, covered_turns: int) -> None:
    if not username or not summary:
        return
    execute_query(
        """
        INSERT INTO assistant_chat_summary (username, summary, covered_turns) VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE summary = VALUES(summary), covered_turns = VALUES(covered_turns),
            updated_at = CURRENT_TIMESTAMP;
        """,
        (username, summary[:6000], covered_turns),
    )

