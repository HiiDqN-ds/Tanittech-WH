"""Seed database for Ria-main.

Usage (run in project folder):
  python seed_db.py

Assumptions:
- MySQL is running and matches db.py DB_CONFIG (see .env.example).
- This script creates the schema used by db.py and app.py if missing.
- Then it inserts baseline users (admin + seller) if they do not already exist.

Default credentials created by this script (change them after first login!):
  admin / admin123
  seller1 / seller123
"""

from __future__ import annotations

import os

import pymysql
from werkzeug.security import generate_password_hash


DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", "root"),
    "database": os.getenv("MYSQL_DATABASE", "warehouse_db"),
    "autocommit": False,
}


def get_connection(database: str | None = None):
    cfg = dict(DB_CONFIG)
    if database is not None:
        cfg["database"] = database
    return pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        autocommit=False,
    )




def ensure_schema(cur):

    def safe_execute(sql: str):
        # Print the statement header for debugging.
        first_line = sql.strip().splitlines()[0][:120]
        print("Executing:", first_line)
        cur.execute(sql)

    # 1) Parent tables first (no FKs)
    safe_execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            username VARCHAR(255) PRIMARY KEY,
            password VARCHAR(255) NOT NULL,
            role VARCHAR(50) NOT NULL,
            profile_img TEXT,
            salary DECIMAL(12,2) DEFAULT 0.0,
            activated BOOLEAN DEFAULT TRUE
        ) ENGINE=InnoDB;
        """
    )

    safe_execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            barcode VARCHAR(64) PRIMARY KEY,
            product_name VARCHAR(255) NOT NULL,
            sku VARCHAR(64),
            purchase_price DECIMAL(12,2) DEFAULT 0.00,
            selling_price DECIMAL(12,2) DEFAULT 0.00,
            min_selling_price DECIMAL(12,2) DEFAULT 0.00,
            quantity INT NOT NULL DEFAULT 0,
            description TEXT,
            photo_link TEXT,
            seller VARCHAR(255),
            date_added DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_products_sku (sku)
        ) ENGINE=InnoDB;
        """
    )

    safe_execute(
        """
        CREATE TABLE IF NOT EXISTS sales (
            sale_id VARCHAR(64) PRIMARY KEY,
            username VARCHAR(255) NOT NULL,
            sale_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            total_sale_price DECIMAL(12,2) DEFAULT 0.00,
            customer_name VARCHAR(255) NULL,
            payment_method VARCHAR(20) NULL,
            INDEX idx_sales_date (sale_date),
            INDEX idx_sales_user (username)
        ) ENGINE=InnoDB;
        """
    )

    # 2) Child tables (no FKs during initial create)
    safe_execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            order_number VARCHAR(64) PRIMARY KEY,
            product_name VARCHAR(255) NOT NULL,
            ref_number VARCHAR(64),
            description TEXT,
            price DECIMAL(12,2) DEFAULT 0.00,
            selling_price DECIMAL(12,2) DEFAULT 0.00,
            min_selling_price DECIMAL(12,2) DEFAULT 0.00,
            quantity INT NOT NULL DEFAULT 1,
            total_price DECIMAL(12,2) DEFAULT 0.00,
            `date` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `user` VARCHAR(255),
            barcode VARCHAR(64),
            INDEX idx_orders_date (`date`),
            INDEX idx_orders_user (`user`),
            INDEX idx_orders_product (product_name)
        ) ENGINE=InnoDB;
        """
    )

    safe_execute(
        """
        CREATE TABLE IF NOT EXISTS sale_items (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            sale_id VARCHAR(64) NOT NULL,
            barcode VARCHAR(64),
            product_name VARCHAR(255) NOT NULL,
            quantity INT NOT NULL,
            sale_price DECIMAL(12,2) NOT NULL,
            total_price DECIMAL(12,2) NOT NULL,
            purchase_price DECIMAL(12,2) DEFAULT 0.00,
            profit DECIMAL(12,2) DEFAULT 0.00,
            INDEX idx_sale_items_sale (sale_id)
        ) ENGINE=InnoDB;
        """
    )

    safe_execute(
        """
        CREATE TABLE IF NOT EXISTS salaries (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            employee VARCHAR(255) NOT NULL,
            amount DECIMAL(12,2) NOT NULL,
            source VARCHAR(255),
            note TEXT,
            payment_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_salaries_employee (employee)
        ) ENGINE=InnoDB;
        """
    )

    safe_execute(
        """
        CREATE TABLE IF NOT EXISTS cash_transactions (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            amount DECIMAL(12,2) NOT NULL,
            type VARCHAR(20) NOT NULL,
            description TEXT,
            username VARCHAR(255),
            INDEX idx_cash_transactions_date (date),
            INDEX idx_cash_transactions_type (type)
        ) ENGINE=InnoDB;
        """
    )

    safe_execute(
        """
        CREATE TABLE IF NOT EXISTS daily_cash_balance (
            date DATE PRIMARY KEY,
            closing_balance DECIMAL(12,2) NOT NULL DEFAULT 0.00
        ) ENGINE=InnoDB;
        """
    )

    safe_execute(
        """
        CREATE TABLE IF NOT EXISTS debts (
            debt_id VARCHAR(32) PRIMARY KEY,
            reference_number VARCHAR(12) UNIQUE,
            client_name VARCHAR(255) NOT NULL,
            description TEXT,
            amount DECIMAL(12,2) NOT NULL,
            original_amount DECIMAL(12,2) NULL,
            phone_number VARCHAR(64),
            paid BOOLEAN DEFAULT FALSE,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_debts_client (client_name),
            INDEX idx_debts_paid (paid)
        ) ENGINE=InnoDB;
        """
    )

    safe_execute(
        """
        CREATE TABLE IF NOT EXISTS debt_payments (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            debt_id VARCHAR(32) NOT NULL,
            amount DECIMAL(12,2) NOT NULL,
            payment_method VARCHAR(20) NOT NULL DEFAULT 'cash',
            paid_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            recorded_by VARCHAR(255),
            INDEX idx_debt_payments_debt (debt_id)
        ) ENGINE=InnoDB;
        """
    )

    safe_execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            actor VARCHAR(255),
            action VARCHAR(50) NOT NULL,
            entity VARCHAR(50) NOT NULL,
            entity_id VARCHAR(64),
            details TEXT,
            source VARCHAR(20) NOT NULL DEFAULT 'web',
            INDEX idx_audit_created (created_at),
            INDEX idx_audit_entity (entity, entity_id)
        ) ENGINE=InnoDB;
        """
    )

    safe_execute(
        """
        CREATE TABLE IF NOT EXISTS factures (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            facture_type VARCHAR(30) NOT NULL DEFAULT 'other',
            reference VARCHAR(100),
            issuer VARCHAR(255) NOT NULL,
            amount DECIMAL(12,2) NOT NULL,
            currency VARCHAR(8) NOT NULL DEFAULT 'EUR',
            issue_date DATE NOT NULL,
            due_date DATE,
            status VARCHAR(20) NOT NULL DEFAULT 'unpaid',
            notes TEXT,
            created_by VARCHAR(255),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_factures_type (facture_type),
            INDEX idx_factures_status (status),
            INDEX idx_factures_issue_date (issue_date)
        ) ENGINE=InnoDB;
        """
    )

    safe_execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_chat_history (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(255) NOT NULL,
            role VARCHAR(16) NOT NULL,
            content MEDIUMTEXT NOT NULL,
            lang VARCHAR(8),
            conversation_id VARCHAR(36),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_chat_history_user_time (username, created_at),
            INDEX idx_chat_history_conversation (conversation_id)
        ) ENGINE=InnoDB;
        """
    )

    # 3) Add FKs after tables exist
    # If they already exist, ALTER will fail; ignore those errors.
    fk_statements = [
        ("orders", "fk_orders_user", "ALTER TABLE orders ADD CONSTRAINT fk_orders_user FOREIGN KEY (`user`) REFERENCES users(username) ON UPDATE CASCADE ON DELETE SET NULL"),
        ("sales", "fk_sales_username", "ALTER TABLE sales ADD CONSTRAINT fk_sales_username FOREIGN KEY (username) REFERENCES users(username) ON UPDATE CASCADE ON DELETE CASCADE"),
        ("sale_items", "fk_sale_items_sale", "ALTER TABLE sale_items ADD CONSTRAINT fk_sale_items_sale FOREIGN KEY (sale_id) REFERENCES sales(sale_id) ON UPDATE CASCADE ON DELETE CASCADE"),
        ("salaries", "fk_salaries_employee", "ALTER TABLE salaries ADD CONSTRAINT fk_salaries_employee FOREIGN KEY (employee) REFERENCES users(username) ON UPDATE CASCADE ON DELETE SET NULL"),
        ("cash_transactions", "fk_cash_username", "ALTER TABLE cash_transactions ADD CONSTRAINT fk_cash_username FOREIGN KEY (username) REFERENCES users(username) ON UPDATE CASCADE ON DELETE SET NULL"),
        ("debt_payments", "fk_debt_payments_debt", "ALTER TABLE debt_payments ADD CONSTRAINT fk_debt_payments_debt FOREIGN KEY (debt_id) REFERENCES debts(debt_id) ON UPDATE CASCADE ON DELETE CASCADE"),
    ]

    for _, _, stmt in fk_statements:
        try:
            print("Applying FK:", stmt)
            cur.execute(stmt)
        except Exception:
            # FK probably already exists.
            pass



def seed_users(cur):
    admin = {
        "username": "admin",
        "password": generate_password_hash("admin123"),
        "role": "admin",
        "profile_img": "",
        "salary": 0.0,
        "activated": True,
    }
    seller1 = {
        "username": "seller1",
        "password": generate_password_hash("seller123"),
        "role": "seller",
        "profile_img": "",
        "salary": 1000.0,
        "activated": True,
    }

    for u in (admin, seller1):
        cur.execute("SELECT 1 FROM users WHERE username = %s;", (u["username"],))
        if cur.fetchone():
            continue
        cur.execute(
            """
            INSERT INTO users (username, password, role, profile_img, salary, activated)
            VALUES (%s, %s, %s, %s, %s, %s);
            """,
            (
                u["username"],
                u["password"],
                u["role"],
                u["profile_img"],
                u["salary"],
                u["activated"],
            ),
        )


def main():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            ensure_schema(cur)
            seed_users(cur)
        conn.commit()
        print("DB initialized: schema created (if missing) + baseline users ensured.")
        print("Login with admin/admin123 or seller1/seller123 (change these after first login).")
    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()

