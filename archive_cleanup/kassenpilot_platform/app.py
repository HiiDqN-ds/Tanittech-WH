"""
kassenpilot_platform/app.py — accounts, subscriptions & permissions core.

Three roles, one login:
  superadmin — you. Creates subscriptions (=shops+admin accounts) instantly,
               no Stripe needed for a "test" €0 plan. Can grant/revoke
               ANY account anywhere (activate/deactivate, edit permissions,
               suspend a whole shop).
  admin      — represents one subscribing store. Created by superadmin.
               Creates seller/worker accounts for their own shop and
               grants/revokes what each one can access (inventory, kasse,
               debts, payroll, invoices, reports, the AI assistant, etc).
  seller     — a worker account, scoped to whatever permissions their
               admin (or superadmin, overriding) granted them.

This is a self-contained accounts/permissions system with its own SQLite
DB. It's deliberately separate from KassenPilot's existing POS app.py so
building it doesn't risk touching your live inventory/sales/payroll code.
Once you're happy with it, the natural next step is either (a) pointing
KassenPilot's login at this same users table, or (b) this app calling
KassenPilot's API with the permissions it computes. Ask me when you're
ready for that wiring.

Run:
    pip install -r requirements.txt
    export SUPERADMIN_PASSWORD=choose-a-strong-password
    export SECRET_KEY=any-random-string
    python app.py
Open http://localhost:5060 — log in with username "superadmin" and the
password above.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                    session, flash, g, send_from_directory)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-only-change-me")

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "platform.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads", "logos")
os.makedirs(UPLOAD_DIR, exist_ok=True)

SUPERADMIN_PASSWORD = os.getenv("SUPERADMIN_PASSWORD", "")

# ---------------------------------------------------------------------------
# Feature/permission catalogue — mirrors KassenPilot's real modules,
# including the AI assistant (assistant.js / /assistant route).
# ---------------------------------------------------------------------------
CAPABILITIES = [
    ("pos_sell",   "Kasse / Sell items"),
    ("inventory",  "Inventory (add/edit items)"),
    ("debts",      "Schulden (debts)"),
    ("payroll",    "Gehälter (payroll)"),
    ("invoices",   "Rechnungen (invoices)"),
    ("reports",    "Reports / audit log"),
    ("clients",    "Clients / purchases"),
    ("assistant",  "AI assistant (assistant.js)"),
]
CAP_KEYS = [c[0] for c in CAPABILITIES]

PLANS = {
    "test":     {"name": "Test",     "price_eur": 0,  "tier": "bronze"},
    "starter":  {"name": "Starter",  "price_eur": 29, "tier": "silver"},
    "pro":      {"name": "Pro",      "price_eur": 59, "tier": "gold"},
    "business": {"name": "Business", "price_eur": 99, "tier": "diamond"},
}

ADMIN_DEFAULT_PERMISSIONS = {k: True for k in CAP_KEYS}
SELLER_DEFAULT_PERMISSIONS = {k: False for k in CAP_KEYS}
SELLER_DEFAULT_PERMISSIONS["pos_sell"] = True  # sellers can at least sell out of the box


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON;")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS shops (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            logo_filename TEXT,
            plan TEXT NOT NULL DEFAULT 'test',
            status TEXT NOT NULL DEFAULT 'active',   -- active | suspended
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,                       -- superadmin | admin | seller
            shop_id TEXT,                              -- null for superadmin
            active INTEGER NOT NULL DEFAULT 1,
            permissions TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(shop_id) REFERENCES shops(id)
        );
        """
    )
    conn.commit()
    conn.close()


def row_to_dict(row):
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return row_to_dict(get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


@app.context_processor
def inject_user():
    return {"user": current_user()}


def login_required(role=None):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user or not user["active"]:
                return redirect(url_for("login"))
            if role and user["role"] != role:
                flash("You don't have access to that.", "danger")
                return redirect(url_for("home"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        # The superadmin account is defined by env var, not stored in the
        # DB with a guessable seed — but we still give it a session/user
        # row so permission checks stay uniform everywhere else.
        if username == "superadmin":
            if not SUPERADMIN_PASSWORD:
                flash("SUPERADMIN_PASSWORD is not set on this deployment.", "danger")
                return render_template("login.html")
            if password == SUPERADMIN_PASSWORD:
                _ensure_superadmin_row()
                row = get_db().execute("SELECT * FROM users WHERE username = 'superadmin'").fetchone()
                session["user_id"] = row["id"]
                return redirect(url_for("home"))
            flash("Invalid credentials.", "danger")
            return render_template("login.html")

        row = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            if not row["active"]:
                flash("This account has been deactivated. Contact your admin.", "danger")
                return render_template("login.html")
            session["user_id"] = row["id"]
            return redirect(url_for("home"))
        flash("Invalid credentials.", "danger")
    return render_template("login.html")


def _ensure_superadmin_row():
    db = get_db()
    exists = db.execute("SELECT 1 FROM users WHERE username = 'superadmin'").fetchone()
    if not exists:
        db.execute(
            "INSERT INTO users (id, username, password_hash, role, shop_id, active, permissions, created_at) "
            "VALUES (?, 'superadmin', ?, 'superadmin', NULL, 1, '{}', ?)",
            (str(uuid.uuid4()), generate_password_hash(SUPERADMIN_PASSWORD), datetime.utcnow().isoformat()),
        )
        db.commit()


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def home():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    if user["role"] == "superadmin":
        return redirect(url_for("superadmin_dashboard"))
    if user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("seller_dashboard"))


@app.route("/uploads/logos/<path:filename>")
def uploaded_logo(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ---------------------------------------------------------------------------
# Superadmin — create subscriptions instantly, grant/revoke anything
# ---------------------------------------------------------------------------

@app.route("/superadmin")
@login_required(role="superadmin")
def superadmin_dashboard():
    db = get_db()
    shops = [row_to_dict(r) for r in db.execute("SELECT * FROM shops ORDER BY created_at DESC")]
    for shop in shops:
        admin = db.execute(
            "SELECT * FROM users WHERE shop_id = ? AND role = 'admin' LIMIT 1", (shop["id"],)
        ).fetchone()
        shop["admin"] = row_to_dict(admin)
        shop["seller_count"] = db.execute(
            "SELECT COUNT(*) c FROM users WHERE shop_id = ? AND role = 'seller'", (shop["id"],)
        ).fetchone()["c"]
        shop["plan_info"] = PLANS.get(shop["plan"], PLANS["test"])
    return render_template("superadmin_dashboard.html", shops=shops, plans=PLANS)


@app.route("/superadmin/shops/new", methods=["GET", "POST"])
@login_required(role="superadmin")
def new_shop():
    if request.method == "POST":
        shop_name = (request.form.get("shop_name") or "").strip()
        admin_username = (request.form.get("admin_username") or "").strip()
        admin_password = request.form.get("admin_password") or ""
        plan = request.form.get("plan") or "test"

        errors = []
        if not shop_name:
            errors.append("Shop name is required.")
        if not admin_username:
            errors.append("Admin username is required.")
        if len(admin_password) < 6:
            errors.append("Admin password must be at least 6 characters.")
        if plan not in PLANS:
            errors.append("Invalid plan.")
        db = get_db()
        if db.execute("SELECT 1 FROM users WHERE username = ?", (admin_username,)).fetchone():
            errors.append("That username is already taken.")

        logo_filename = None
        logo = request.files.get("logo")
        if logo and logo.filename:
            ext = os.path.splitext(logo.filename)[1].lower()
            if ext not in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
                errors.append("Logo must be png, jpg, webp, or svg.")
            else:
                logo_filename = f"{uuid.uuid4().hex}{ext}"

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("new_shop.html", plans=PLANS, form=request.form)

        if logo_filename:
            logo.save(os.path.join(UPLOAD_DIR, secure_filename(logo_filename)))

        shop_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        # 0-fee test plan (or any plan, here) activates immediately — no
        # Stripe checkout in the loop. Real paid plans can still be wired
        # to Stripe later; this path is for you to spin up a subscriber
        # directly, on the house or otherwise.
        db.execute(
            "INSERT INTO shops (id, name, logo_filename, plan, status, created_at) VALUES (?,?,?,?, 'active', ?)",
            (shop_id, shop_name, logo_filename, plan, now),
        )
        db.execute(
            "INSERT INTO users (id, username, password_hash, role, shop_id, active, permissions, created_at) "
            "VALUES (?,?,?, 'admin', ?, 1, ?, ?)",
            (str(uuid.uuid4()), admin_username, generate_password_hash(admin_password),
             shop_id, json.dumps(ADMIN_DEFAULT_PERMISSIONS), now),
        )
        db.commit()
        flash(f"'{shop_name}' created and activated on the {PLANS[plan]['name']} plan. "
              f"Admin login: {admin_username}", "success")
        return redirect(url_for("superadmin_dashboard"))

    return render_template("new_shop.html", plans=PLANS, form={})


@app.route("/superadmin/shops/<shop_id>/toggle", methods=["POST"])
@login_required(role="superadmin")
def toggle_shop(shop_id):
    db = get_db()
    shop = db.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()
    if not shop:
        flash("Shop not found.", "danger")
        return redirect(url_for("superadmin_dashboard"))
    new_status = "suspended" if shop["status"] == "active" else "active"
    db.execute("UPDATE shops SET status = ? WHERE id = ?", (new_status, shop_id))
    db.commit()
    flash(f"{shop['name']} is now {new_status}.", "success")
    return redirect(url_for("superadmin_dashboard"))


@app.route("/superadmin/shops/<shop_id>")
@login_required(role="superadmin")
def shop_detail(shop_id):
    db = get_db()
    shop = row_to_dict(db.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone())
    if not shop:
        flash("Shop not found.", "danger")
        return redirect(url_for("superadmin_dashboard"))
    users = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM users WHERE shop_id = ? ORDER BY role, username", (shop_id,)
    )]
    for u in users:
        u["perms"] = json.loads(u["permissions"] or "{}")
    return render_template("shop_detail.html", shop=shop, users=users, capabilities=CAPABILITIES)


# ---------------------------------------------------------------------------
# Grant/revoke — usable by superadmin on ANY user, and by admin on their
# OWN sellers only. Shared implementation, scope-checked per caller.
# ---------------------------------------------------------------------------

def _can_manage(actor, target):
    if actor["role"] == "superadmin":
        return True
    if actor["role"] == "admin" and target["role"] == "seller" and target["shop_id"] == actor["shop_id"]:
        return True
    return False


@app.route("/users/<user_id>/toggle", methods=["POST"])
def toggle_user(user_id):
    actor = current_user()
    if not actor:
        return redirect(url_for("login"))
    db = get_db()
    target = row_to_dict(db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
    if not target or not _can_manage(actor, target):
        flash("Not allowed.", "danger")
        return redirect(url_for("home"))
    if target["role"] == "superadmin":
        flash("Can't deactivate the superadmin account.", "danger")
        return redirect(url_for("home"))
    new_active = 0 if target["active"] else 1
    db.execute("UPDATE users SET active = ? WHERE id = ?", (new_active, user_id))
    db.commit()
    flash(f"{target['username']} is now {'active' if new_active else 'revoked'}.", "success")
    return redirect(request.referrer or url_for("home"))


@app.route("/users/<user_id>/permissions", methods=["GET", "POST"])
def edit_permissions(user_id):
    actor = current_user()
    if not actor:
        return redirect(url_for("login"))
    db = get_db()
    target = row_to_dict(db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
    if not target or not _can_manage(actor, target):
        flash("Not allowed.", "danger")
        return redirect(url_for("home"))

    if request.method == "POST":
        new_perms = {k: (request.form.get(k) == "on") for k in CAP_KEYS}
        db.execute("UPDATE users SET permissions = ? WHERE id = ?", (json.dumps(new_perms), user_id))
        db.commit()
        flash(f"Permissions updated for {target['username']}.", "success")
        back = url_for("shop_detail", shop_id=target["shop_id"]) if actor["role"] == "superadmin" else url_for("admin_dashboard")
        return redirect(back)

    perms = json.loads(target["permissions"] or "{}")
    return render_template("edit_permissions.html", target=target, perms=perms, capabilities=CAPABILITIES)


# ---------------------------------------------------------------------------
# Admin — manage own shop's sellers
# ---------------------------------------------------------------------------

@app.route("/admin")
@login_required(role="admin")
def admin_dashboard():
    user = current_user()
    db = get_db()
    shop = row_to_dict(db.execute("SELECT * FROM shops WHERE id = ?", (user["shop_id"],)).fetchone())
    sellers = [row_to_dict(r) for r in db.execute(
        "SELECT * FROM users WHERE shop_id = ? AND role = 'seller' ORDER BY username", (user["shop_id"],)
    )]
    for s in sellers:
        s["perms"] = json.loads(s["permissions"] or "{}")
    return render_template("admin_dashboard.html", shop=shop, sellers=sellers, capabilities=CAPABILITIES,
                            suspended=(shop["status"] != "active"))


@app.route("/admin/sellers/new", methods=["GET", "POST"])
@login_required(role="admin")
def new_seller():
    user = current_user()
    db = get_db()
    shop = row_to_dict(db.execute("SELECT * FROM shops WHERE id = ?", (user["shop_id"],)).fetchone())
    if shop["status"] != "active":
        flash("Your shop is suspended — contact the platform owner.", "danger")
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        errors = []
        if not username:
            errors.append("Username is required.")
        if len(password) < 6:
            errors.append("Password must be at least 6 characters.")
        if db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            errors.append("That username is already taken.")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("new_seller.html", form=request.form)

        db.execute(
            "INSERT INTO users (id, username, password_hash, role, shop_id, active, permissions, created_at) "
            "VALUES (?,?,?, 'seller', ?, 1, ?, ?)",
            (str(uuid.uuid4()), username, generate_password_hash(password), user["shop_id"],
             json.dumps(SELLER_DEFAULT_PERMISSIONS), datetime.utcnow().isoformat()),
        )
        db.commit()
        flash(f"Seller '{username}' created.", "success")
        return redirect(url_for("admin_dashboard"))

    return render_template("new_seller.html", form={})


# ---------------------------------------------------------------------------
# Seller — sees only what they were granted
# ---------------------------------------------------------------------------

@app.route("/seller")
@login_required(role="seller")
def seller_dashboard():
    user = current_user()
    perms = json.loads(user["permissions"] or "{}")
    db = get_db()
    shop = row_to_dict(db.execute("SELECT * FROM shops WHERE id = ?", (user["shop_id"],)).fetchone())
    granted = [label for key, label in CAPABILITIES if perms.get(key)]
    return render_template("seller_dashboard.html", user=user, shop=shop, granted=granted,
                            suspended=(shop["status"] != "active"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5060)
else:
    init_db()
