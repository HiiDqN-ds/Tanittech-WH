"""
gdpr.py — data export & account deletion ("Compliance basics" on the
KassenPilot proposal deck, slide 6).

Two admin-only endpoints:
  GET  /gdpr/export   downloads every table as one JSON file
  POST /gdpr/delete   wipes the shop's data after typed confirmation

This is intentionally simple (whole-database export/wipe) because the
deployment model is one database per shop — see the note at the top of
billing_db.py. If you move to a shared multi-tenant database later, these
need to filter by tenant_id instead of exporting/dropping whole tables.
"""

import io
import json
import logging
from datetime import datetime, date
from decimal import Decimal
from functools import wraps

from flask import Blueprint, jsonify, session, redirect, url_for, flash, send_file, request

from db import get_connection, log_audit
import billing_db as bdb

logger = logging.getLogger(__name__)

gdpr_bp = Blueprint("gdpr", __name__)

# Tables considered "shop data" for export/delete. Kept as an explicit list
# (rather than introspecting information_schema) so a future table has to
# be added here deliberately before it's swept up by a delete.
EXPORTABLE_TABLES = [
    "users", "products", "sales", "sale_items", "orders", "salaries",
    "cash_transactions", "daily_cash_balance", "debts", "debt_payments",
    "factures", "audit_log", "assistant_chat_history", "assistant_memory",
    "assistant_chat_summary", "shop_settings", "subscriptions",
]


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "username" not in session or session.get("role") != "admin":
            flash("Admin access required.", "danger")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def _json_default(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    return str(obj)


@gdpr_bp.route("/gdpr/export")
@admin_required
def export_data():
    conn = get_connection()
    data = {}
    try:
        with conn.cursor() as cur:
            for table in EXPORTABLE_TABLES:
                try:
                    cur.execute(f"SELECT * FROM `{table}`;")
                    rows = cur.fetchall() or []
                    # Never export password hashes in a data-portability dump.
                    if table == "users":
                        for row in rows:
                            row.pop("password", None)
                    data[table] = rows
                except Exception:
                    # Table may not exist on older installs — skip quietly.
                    continue
    finally:
        conn.close()

    log_audit("gdpr_export", "shop", None, "Full data export downloaded",
              actor=session.get("username"), module="compliance")

    payload = json.dumps(
        {"exported_at": datetime.utcnow().isoformat() + "Z", "data": data},
        default=_json_default, indent=2, ensure_ascii=False,
    ).encode("utf-8")

    buf = io.BytesIO(payload)
    buf.seek(0)
    fname = f"kassenpilot-export-{datetime.utcnow().strftime('%Y%m%d')}.json"
    return send_file(buf, mimetype="application/json", as_attachment=True, download_name=fname)


@gdpr_bp.route("/gdpr/delete", methods=["POST"])
@admin_required
def delete_data():
    if (request.form.get("confirm") or "").strip().upper() != "DELETE":
        flash("Type DELETE exactly to confirm account deletion.", "danger")
        return redirect(url_for("billing.status"))

    actor = session.get("username")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for table in EXPORTABLE_TABLES:
                try:
                    cur.execute(f"DELETE FROM `{table}`;")
                except Exception:
                    continue
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("GDPR delete failed")
        flash("Something went wrong deleting your data — nothing was removed. Contact support.", "danger")
        return redirect(url_for("billing.status"))
    finally:
        conn.close()

    logger.info("Shop data deleted by %s", actor)
    session.clear()
    flash("Your account and all shop data have been permanently deleted.", "info")
    return redirect(url_for("billing.signup"))
