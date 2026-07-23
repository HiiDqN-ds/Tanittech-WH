import os

# Load variables from a local .env file (if present) into the process
# environment. This MUST happen before anything else calls os.getenv() —
# in particular before `from db import *` below, since db.py reads
# MYSQL_HOST/MYSQL_USER/etc. at import time. Without this, a .env file
# created from .env.example (as the file itself instructs) was silently
# ignored: only variables actually exported in the shell/host environment
# took effect, which is why ANTHROPIC_API_KEY (and DB settings) could look
# "set" in .env but still be missing at runtime.
try:
    from dotenv import load_dotenv
    # - Path is explicit (next to this file), so it doesn't matter which
    #   directory the process was launched from.
    # - override=True makes values in .env win over any stray environment
    #   variable left behind by an earlier shell session (Windows in
    #   particular persists env vars across terminals more than you'd
    #   expect) — otherwise a real key in .env can be silently shadowed
    #   by a leftover empty/placeholder value.
    _dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    load_dotenv(dotenv_path=_dotenv_path, override=True, encoding='utf-8-sig')
except ImportError:
    # python-dotenv isn't installed — fine in production if real
    # environment variables are set directly (e.g. by the host).
    pass

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash,
    Response, jsonify, send_file, abort, stream_with_context,
)
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import csv
import io
import json
import logging
import random
import re
import uuid
from datetime import datetime, timedelta, date
from decimal import Decimal, InvalidOperation
from io import StringIO

import html

import barcode
from barcode.writer import ImageWriter
from apscheduler.schedulers.background import BackgroundScheduler
from PIL import Image, ImageOps

# pytesseract wraps the Tesseract OCR binary. The Python package alone is not
# enough - Tesseract itself must be installed on the machine (see
# requirements.txt / README for the "invoice photo scan" feature). We import
# it defensively so the whole app doesn't crash if it's missing; the OCR
# route just reports a clear error instead.
try:
    import pytesseract
    TESSERACT_AVAILABLE = True
except ImportError:
    pytesseract = None
    TESSERACT_AVAILABLE = False

# PyMuPDF ("fitz") powers PDF support for the invoice scanner: for a normal
# (non-scanned) PDF it reads the embedded text layer directly — that's exact,
# character-for-character, not a guess from OCR — and only falls back to
# rendering pages as images + Tesseract OCR for scanned/photographed PDFs
# that have no text layer.
try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    fitz = None
    PYMUPDF_AVAILABLE = False

# OpenAI SDK powers the free-text assistant chat (/assistant/api/chat).
# Imported defensively so the rest of the app still runs even if the
# dependency isn't installed (or no API key is configured).
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OpenAI = None
    OPENAI_AVAILABLE = False

# Stripe powers real card payments at the register (Stripe Terminal, i.e. a
# physical card reader). Imported defensively so the rest of the app still
# runs even if the dependency isn't installed (or no key is configured) —
# card sales just aren't available in that case, everything else works.
try:
    import stripe
    STRIPE_SDK_AVAILABLE = True
except ImportError:
    stripe = None
    STRIPE_SDK_AVAILABLE = False


from db import *
from translations import TR, SUPPORTED_LANGS, DEFAULT_LANG

import billing_db as bdb
from billing import billing_bp, subscription_gate
from gdpr import gdpr_bp


app = Flask(__name__)
# Required for sessions/flash messages. Set FLASK_SECRET_KEY in production.
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key-change-me')

logger = logging.getLogger(__name__)

app.register_blueprint(billing_bp)
app.register_blueprint(gdpr_bp)

try:
    bdb.ensure_billing_tables()
except Exception:
    logger.exception('Could not ensure billing/subscription tables at startup')


@app.before_request
def _enforce_subscription():
    """Gate the whole app behind an active subscription/trial. Signup,
    login, the billing pages themselves, and static assets stay reachable
    so a shop can always get in to pay you. See billing.py."""
    return subscription_gate()


def shop_feature_enabled(feature):
    """Is this feature turned on for the current session's shop?
    Superadmin accounts aren't scoped to a shop, so they're never blocked
    by these flags — the flags only ever restrict admin/seller accounts."""
    if session.get('role') == 'superadmin':
        return True
    return get_shop_features(session.get('shop_id')).get(feature, True)


def seller_kasse_enabled(username=None):
    """Can this account deposit/withdraw cash at the register?

    Only ever restricts a seller — admins and superadmins always have
    full Kasse access. Looked up fresh from the DB (not cached in the
    session) so a revoke by the admin takes effect on the seller's very
    next request, the same request-fresh behaviour as shop_feature_enabled().
    """
    return seller_has_permission_category('kasse', username)


# Each checkbox on the Edit Seller page ("KI-Assistent — zusätzliche
# Berechtigungen") grants a seller a group of related write actions —
# both the AI assistant's tools AND, where applicable, the matching page
# in the regular web app (e.g. 'kasse' also gates the /kasse deposit/
# withdraw form, not just the assistant's add_kasse_transaction tool).
# Read-only tools (get_summary, list_items, ...) and quick_sell are never
# gated here — every seller already has those regardless of category.
PERMISSION_CATEGORY_TOOLS = {
    'debts': {'add_debt', 'pay_debt', 'record_debt_payment', 'delete_debt',
              'delete_all_debts', 'update_debt', 'edit_debt_payment', 'delete_debt_payment'},
    'factures': {'add_facture', 'pay_facture', 'edit_facture', 'delete_facture'},
    'kasse': {'add_kasse_transaction', 'edit_kasse_transaction', 'delete_kasse_transaction'},
    'items': {'add_item', 'update_item', 'delete_item'},
    'orders': {'add_order', 'edit_order', 'delete_order'},
    'sales': {'edit_sale_item', 'delete_sale'},
    'clients': {'rename_client', 'delete_client'},
}

# Base tools every seller can already use regardless of granted categories
# — viewing sales/inventory/reports plus checkout, per the Edit Seller
# page's own description ("Jeder Verkäufer kann den Assistenten bereits
# für Verkäufe, Bestand ansehen und Berichte nutzen.").
SELLER_BASE_TOOLS = {
    'get_summary', 'list_items', 'list_low_stock', 'list_recent_sales',
    'list_recent_orders', 'quick_sell', 'print_barcode', 'remember_note',
}


def seller_granted_categories(username=None):
    """The set of extra permission categories granted to this account.
    Admin/superadmin sessions are unrestricted (every category "granted"),
    since these checkboxes only ever limit a seller."""
    if session.get('role') != 'seller':
        return set(PERMISSION_CATEGORY_TOOLS.keys())
    return get_seller_permission_categories(username or session.get('username'))


def seller_has_permission_category(category, username=None):
    """Has this account been granted a specific extra-permission category
    (e.g. 'kasse')? Admins/superadmins always pass."""
    if session.get('role') != 'seller':
        return True
    return category in get_seller_permission_categories(username or session.get('username'))


def seller_allowed_tools(username=None):
    """Full set of assistant tool names usable by this session right now:
    the always-on base tools plus whatever extra categories were granted.
    Admin/superadmin sessions bypass this entirely (see the check at the
    top of _execute_assistant_tool)."""
    granted = seller_granted_categories(username)
    extra = set()
    for category in granted:
        extra |= PERMISSION_CATEGORY_TOOLS.get(category, set())
    return SELLER_BASE_TOOLS | extra


@app.before_request
def _enforce_shop_feature_flags():
    """Superadmin can grant/revoke the assistant, card payments, and
    barcode-label printing per shop. Enforced here at the path level so
    every route under each prefix is covered, not just the main page."""
    if 'username' not in session:
        return  # unauthenticated — login_required on the view will handle it

    path = request.path
    if path.startswith('/assistant') and not shop_feature_enabled('assistant'):
        flash('Der KI-Assistent ist für dieses Konto deaktiviert.', 'warning')
        return redirect(url_for('index'))
    if path.startswith('/stripe') and not shop_feature_enabled('card_payment'):
        return jsonify({'error': 'Kartenzahlung ist für dieses Konto deaktiviert.'}), 403
    if ('/admin/items/barcode_print' in path or '/admin/items/barcode_label' in path) and not shop_feature_enabled('barcode_printing'):
        flash('Barcode-Druck ist für dieses Konto deaktiviert.', 'warning')
        return redirect(url_for('list_items'))

# One-time repair for the "Gewinn is always 0" bug: earlier INSERTs into
# sale_items never wrote the `profit` column, so every historical sale sits
# at the column's default of 0.00. Runs at import time (not inside
# `if __name__ == '__main__'`) so it also fires under gunicorn, which is how
# the Procfile actually starts this app in production. Safe to run every
# time the app boots — it only touches rows that still show profit = 0.
try:
    _repaired = backfill_sale_item_profits()
    if _repaired:
        logger.info(f'Backfilled profit on {_repaired} historical sale_items row(s).')
except Exception:
    logger.exception('Could not backfill sale_items.profit at startup (DB unreachable?)')

# AI-first checkout (see quick_sell / assistant_quick_sell below) now records
# who a sale was for and how it was paid, so make sure those columns exist
# even on a database created before this upgrade.
try:
    ensure_sales_extra_columns()
except Exception:
    logger.exception('Could not ensure sales.customer_name/payment_method columns at startup')

# Inventory search (see search_items() in db.py) can match by SKU, so make
# sure the column exists even on a database created before this feature.
try:
    ensure_products_sku_column()
except Exception:
    logger.exception('Could not ensure products.sku column at startup')

# An admin can grant/revoke individual sellers' extra assistant
# permissions (Kasse, Schulden, Rechnungen, ...), separate from the
# card_payment/assistant shop-wide flags above. Make sure the column
# exists even on an older database.
try:
    ensure_seller_permissions_column()
except Exception:
    logger.exception('Could not ensure users.assistant_permissions column at startup')

# The product's barcode is now its primary key (replacing the old
# surrogate `id`) everywhere in the app — URLs, the chat assistant,
# receipts/labels. Migrate any database still on the old schema.
try:
    ensure_products_barcode_primary_key()
except Exception:
    logger.exception('Could not migrate products to a barcode primary key at startup')

# Products can have a condition (Zustand: neu/gebraucht/defekt) — make sure
# the column exists even on a database created before this feature.
try:
    ensure_products_condition_column()
except Exception:
    logger.exception('Could not ensure products.item_condition column at startup')

# Every debt gets its own short random reference number
# the internal debt_id) for reading out to a client or printing on a
# receipt/contract. Make sure the column exists and every row has one.
try:
    ensure_debts_reference_number_column()
except Exception:
    logger.exception('Could not ensure debts.reference_number column at startup')

try:
    ensure_audit_log_module_column()
except Exception:
    logger.exception('Could not ensure audit_log.module column at startup')

try:
    ensure_cash_transactions_payment_method_column()
except Exception:
    logger.exception('Could not ensure cash_transactions.payment_method column at startup')

# Kasse (the physical cash drawer) must only count cash-paid purchases —
# previously every purchase order reduced it regardless of payment method,
# since orders had no way to record one at all.
try:
    ensure_orders_payment_method_column()
except Exception:
    logger.exception('Could not ensure orders.payment_method column at startup')

try:
    ensure_debt_payment_support()
except Exception:
    logger.exception('Could not ensure debts.original_amount / debt_payments at startup')

# Card sales taken at the register via a physical Stripe Terminal reader
# record the PaymentIntent id on the sale, so make sure the column exists
# even on a database created before this feature.
try:
    ensure_sales_stripe_payment_intent_column()
except Exception:
    logger.exception('Could not ensure sales.stripe_payment_intent_id column at startup')

try:
    ensure_refunds_table()
except Exception:
    logger.exception('Could not ensure refunds table at startup')

# Purchase orders (Einkauf) paid by card go through the same physical Stripe
# Terminal reader as sales/debt payments — make sure the column that stores
# that PaymentIntent id exists even on a database created before this.
try:
    ensure_orders_stripe_payment_intent_column()
except Exception:
    logger.exception('Could not ensure orders.stripe_payment_intent_id column at startup')

# The chat assistant's long-term memory (durable per-user notes) and its
# rolling history summary (compresses conversation turns older than the raw
# window sent to the LLM instead of silently dropping them) live in their
# own tables — make sure they exist even on a database created before this
# feature.
try:
    ensure_assistant_memory_tables()
    ensure_assistant_chat_history_conversation_column()
except Exception:
    logger.exception('Could not ensure assistant_memory/assistant_chat_summary tables at startup')

# --- Stripe Terminal config (physical card reader at the Kasse) ---
# STRIPE_SECRET_KEY: your Stripe secret key (sk_test_... / sk_live_...).
# STRIPE_LOCATION_ID: the Stripe Terminal "Location" (tml_...) the physical
# reader is registered under. Both come from the Stripe Dashboard
# (Terminal -> Locations / Readers). See .env.example for details.
STRIPE_SECRET_KEY = (os.getenv('STRIPE_SECRET_KEY') or '').strip()
STRIPE_LOCATION_ID = (os.getenv('STRIPE_LOCATION_ID') or '').strip()
STRIPE_CONFIGURED = bool(STRIPE_SDK_AVAILABLE and STRIPE_SECRET_KEY)
if STRIPE_CONFIGURED:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    logger.info('Stripe Terminal not configured (missing stripe package or STRIPE_SECRET_KEY) — card payments at the register are disabled until set up.')

_openai_client = None


def _get_llm_provider_config():
    """Resolve which OpenAI-compatible provider is actually configured.

    The .env file in this project sets GROQ_API_KEY / GROQ_MODEL /
    GROQ_AUDIO_MODEL / GROQ_BASE_URL (Groq's API is OpenAI-SDK-compatible),
    but get_openai_client() and the two call sites below only ever read
    OPENAI_API_KEY / OPENAI_MODEL / OPENAI_AUDIO_MODEL / OPENAI_BASE_URL —
    so with a Groq-only .env the assistant always reported "OPENAI_API_KEY
    is not configured on the server." even though a valid key was present.

    This checks OPENAI_* first (so a real OpenAI key still takes priority
    if both happen to be set) and falls back to GROQ_*, defaulting the base
    URL and model names to Groq's own values when it's the one supplying
    the key — since Groq requires api.groq.com's base URL and doesn't
    serve OpenAI's own model names (gpt-4o-mini, whisper-1).
    """
    api_key = os.getenv('OPENAI_API_KEY')
    if api_key:
        return {
            'api_key': api_key,
            'base_url': os.getenv('OPENAI_BASE_URL') or None,
            'chat_model': os.getenv('OPENAI_MODEL') or 'gpt-4o-mini',
            'audio_model': os.getenv('OPENAI_AUDIO_MODEL') or 'whisper-1',
        }
    api_key = os.getenv('GROQ_API_KEY')
    if api_key:
        return {
            'api_key': api_key,
            'base_url': os.getenv('GROQ_BASE_URL') or 'https://api.groq.com/openai/v1',
            'chat_model': os.getenv('GROQ_MODEL') or 'llama-3.3-70b-versatile',
            'audio_model': os.getenv('GROQ_AUDIO_MODEL') or 'whisper-large-v3',
        }
    return None



# Fallback strings the chat loop shows when something goes wrong *before*
# the model itself has produced any reply — these previously were English
# only, so a German- or Arabic-speaking user got an English error the one
# time something actually failed, breaking the "always replies in your
# language" promise exactly when it's most noticeable. Keyed the same way
# the frontend's own I18N/state.lang is: 'de' | 'en' | 'ar'.
_ASSISTANT_FALLBACK_MESSAGES = {
    'too_many_steps': {
        'de': 'Das hat mehr Schritte gebraucht als erwartet — kannst du es anders formulieren oder eine Sache nach der anderen fragen?',
        'en': "That turned into more steps than expected — could you rephrase, or ask me one thing at a time?",
        'ar': 'تطلّب هذا خطوات أكثر من المتوقع — هل يمكنك إعادة الصياغة أو طرح طلب واحد في كل مرة؟',
    },
    'quota_exceeded': {
        'de': 'Das KI-Kontingent ist aufgebraucht. Bitte wende dich an die Verwaltung/Abrechnung, um den Zugriff wiederherzustellen, und versuche es dann erneut.',
        'en': 'LLM quota exceeded. Please contact the administrator/billing to restore access, then try again.',
        'ar': 'تم استنفاد حصة الذكاء الاصطناعي. يرجى التواصل مع الإدارة/الفوترة لاستعادة الوصول ثم المحاولة مرة أخرى.',
    },
    'rate_limited': {
        'de': 'Gerade zu viele Anfragen beim Assistenten. Bitte versuche es in einem Moment erneut.',
        'en': 'Rate limit reached while contacting the assistant. Please try again in a moment.',
        'ar': 'تم الوصول إلى حد الطلبات مع المساعد. يرجى المحاولة مرة أخرى بعد قليل.',
    },
    'unreachable': {
        'de': 'Der Assistent ist gerade nicht erreichbar. Bitte versuche es in einem Moment erneut.',
        'en': 'The assistant is unreachable right now. Please try again in a moment.',
        'ar': 'المساعد غير متاح حاليًا. يرجى المحاولة مرة أخرى بعد قليل.',
    },
}


def _fallback_message(key, lang):
    entry = _ASSISTANT_FALLBACK_MESSAGES.get(key, {})
    return entry.get(lang) or entry.get('en') or ''


def _is_tool_schema_validation_error(exc):
    """True for the specific failure mode where the configured provider
    (Groq's OpenAI-compatible endpoint, in this app's case) rejects an
    entire chat.completions.create() call because the model emitted a tool
    argument that doesn't match the declared JSON-schema type — e.g. a
    quoted "3" where the schema says integer. OpenAI's own API doesn't
    validate tool-call arguments server-side at all, so this only fires
    against stricter OpenAI-compatible providers; against real OpenAI this
    check simply never matches and the retry below is never triggered."""
    text = str(exc).lower()
    return 'tool call validation failed' in text or (
        'did not match schema' in text and 'tool' in text
    )


def _create_completion_with_schema_retry(client, **kwargs):
    """Wraps client.chat.completions.create() with exactly one retry for
    the schema-validation failure above. The retry re-sends the same
    request with a short corrective system message appended, which is
    usually enough to get a well-typed tool call back on the second try —
    the model is not being asked to redo any work, only to reformat one
    argument. If the retry also fails (or the error isn't this specific
    kind), the original exception propagates unchanged so the existing
    completed_write / rate-limit / generic-error handling still applies."""
    try:
        return client.chat.completions.create(**kwargs)
    except Exception as e:
        if not _is_tool_schema_validation_error(e):
            raise
        nudge_messages = kwargs['messages'] + [{
            'role': 'system',
            'content': ('Your previous tool call was rejected: a numeric argument was sent as a '
                        'quoted string instead of a plain JSON number. Retry the same tool call '
                        'with every numeric field (quantity, amount, price, salary, any id) as an '
                        'actual number, not text.'),
        }]
        retry_kwargs = dict(kwargs, messages=nudge_messages)
        return client.chat.completions.create(**retry_kwargs)


def _llm_unavailable_message():
    """get_openai_client() returns None for two very different reasons, but
    every call site used to show the same generic 'set OPENAI_API_KEY or
    GROQ_API_KEY' message for both — which is actively misleading if the
    real cause is the `openai` package not being installed at all: the key
    could be set correctly and this message would still tell you to go set
    it, sending you in circles. This builds the message that actually
    matches what's wrong."""
    if not OPENAI_AVAILABLE:
        return ("The 'openai' Python package isn't installed on this server, so no API key "
                "can be used yet (installed and configured are two separate things). "
                "Run: pip install -r requirements.txt (it includes openai==1.40.0), then restart the server.")
    if not _get_llm_provider_config():
        return 'No AI provider is configured on the server (set OPENAI_API_KEY or GROQ_API_KEY).'
    return 'The AI provider client could not be created — check the server logs for the specific error.'


def get_openai_client():
    """Lazily build the OpenAI-SDK-compatible client from whichever
    provider _get_llm_provider_config() resolves (OpenAI or Groq).

    Returns None if the SDK isn't installed or no key is configured, so the
    assistant route can return a clear 503 instead of crashing.
    """
    global _openai_client
    if not OPENAI_AVAILABLE:
        return None
    if _openai_client is None:
        config = _get_llm_provider_config()
        if not config:
            return None
        # Work around httpx/SDK version mismatch around `proxies` kwarg.
        # If your environment uses a proxy, configure it via env vars (HTTP_PROXY/HTTPS_PROXY).
        # Create client with an explicit httpx client (prevents SDK from passing unsupported kwargs like `proxies`).
        import httpx
        _openai_client = OpenAI(api_key=config['api_key'], base_url=config['base_url'], http_client=httpx.Client())
    return _openai_client


if not OPENAI_AVAILABLE:
    logger.warning("AI assistant disabled: the 'openai' Python package is not installed "
                    "(pip install -r requirements.txt). Any OPENAI_API_KEY/GROQ_API_KEY you've "
                    "set will be ignored until the package is installed.")
elif _get_llm_provider_config() is None:
    logger.warning('AI assistant disabled: no OPENAI_API_KEY or GROQ_API_KEY found in the environment/.env.')
else:
    _provider_name = 'OpenAI' if os.getenv('OPENAI_API_KEY') else 'Groq'
    logger.info(f'AI assistant configured: using {_provider_name} (model: {_get_llm_provider_config()["chat_model"]}).')



def _stripe_terminal_use_simulator():
    """Whether the browser should use Stripe's built-in *software* simulated
    reader instead of discovering your real, registered physical reader.

    This is intentionally NOT tied to sk_test_ vs sk_live_: a real physical
    reader can (and normally should, while developing) be used in test mode
    too — taps/inserts are read normally but no real money moves. Forcing
    the software simulator just because a test key is set would silently
    ignore a real reader that's actually sitting on the counter.

    Controlled explicitly via STRIPE_TERMINAL_USE_SIMULATOR=true in .env —
    only turn that on if you have no physical reader at all yet.
    """
    return (os.getenv('STRIPE_TERMINAL_USE_SIMULATOR') or '').strip().lower() in ('1', 'true', 'yes', 'on')


def create_stripe_terminal_payment_intent(amount_eur, context, reference, description=None):
    """Create a PaymentIntent for a card-present (physical reader) payment.

    `context`/`reference` (e.g. 'sale' / a sale_id, or 'debt_payment' / a
    debt_id) are stored in metadata purely so a payment can be traced back
    to what it was for from the Stripe Dashboard — they don't affect
    processing.
    """
    if not STRIPE_CONFIGURED:
        raise RuntimeError('Stripe is not configured on the server (missing stripe package or STRIPE_SECRET_KEY).')
    amount_cents = int(round(float(amount_eur) * 100))
    if amount_cents <= 0:
        raise ValueError('amount must be greater than 0')
    return stripe.PaymentIntent.create(
        amount=amount_cents,
        currency='eur',
        payment_method_types=['card_present'],
        capture_method='automatic',
        description=description or f'{context}:{reference}',
        metadata={'context': context, 'reference': str(reference), 'app': 'ookk_merged'},
    )


def verify_stripe_terminal_payment(payment_intent_id, expected_amount_eur):
    """Re-fetch a PaymentIntent from Stripe and confirm it actually
    succeeded for the expected amount before a card sale/payment is
    recorded as paid.

    This is the safety check that prevents a tampered/forged client-side
    request from marking something as paid by card without Stripe having
    actually captured the money — the browser is never trusted on its own
    for "it went through".
    """
    if not STRIPE_CONFIGURED:
        raise RuntimeError('Stripe is not configured on the server.')
    if not payment_intent_id:
        raise ValueError('Missing stripe_payment_intent_id for a card payment.')
    intent = stripe.PaymentIntent.retrieve(payment_intent_id)
    if intent.status != 'succeeded':
        raise ValueError(f'Card payment has not succeeded yet (status: {intent.status}).')
    expected_cents = int(round(float(expected_amount_eur) * 100))
    if abs(int(intent.amount) - expected_cents) > 1:  # 1 cent rounding tolerance
        raise ValueError('Card payment amount does not match the sale/payment total.')
    return intent


@app.context_processor
def inject_stripe_terminal_config():
    """Make Stripe Terminal config available in every template (used to
    decide whether to show the 'pay by card' reader flow at all, and
    whether to default the JS SDK to a simulated reader in test mode).
    'configured' is now also gated by the shop's card_payment feature flag
    — a superadmin-revoked shop sees no card option even if Stripe itself
    is configured on the server."""
    return {
        'stripe_terminal': {
            'configured': STRIPE_CONFIGURED and shop_feature_enabled('card_payment'),
            'location_id': STRIPE_LOCATION_ID,
            'simulated': _stripe_terminal_use_simulator(),
        }
    }


@app.context_processor
def inject_shop_features():
    """Expose {{ shop_features.assistant }} / .card_payment / .barcode_printing
    to every template, so nav links and buttons for a revoked feature can
    hide themselves instead of just failing when clicked."""
    if 'username' not in session:
        return {'shop_features': dict(DEFAULT_SHOP_FEATURES)}
    if session.get('role') == 'superadmin':
        return {'shop_features': dict(DEFAULT_SHOP_FEATURES)}
    return {'shop_features': get_shop_features(session.get('shop_id'))}


@app.context_processor
def inject_seller_permissions():
    """Expose {{ can_kasse }} and {{ seller_categories }} to every
    template, same idea as inject_shop_features() but for the per-seller
    (not per-shop) assistant permission grants — so buttons/forms for a
    category the seller doesn't have can hide themselves instead of just
    403ing on click."""
    categories = seller_granted_categories()
    return {'can_kasse': 'kasse' in categories, 'seller_categories': categories}


@app.context_processor
def inject_site_language():
    """Make {{ T.xxx }}, {{ site_lang }} and {{ site_dir }} available everywhere.

    The whole-page language toggle (DE / EN / AR) lives in the topbar and in
    the sidebar footer on the login page; it only stores a language code in
    the session, so it survives navigation just like the rest of the app.
    """
    lang = session.get('site_lang', DEFAULT_LANG)
    if lang not in TR:
        lang = DEFAULT_LANG
    return {
        'site_lang': lang,
        'T': TR[lang],
        'site_dir': 'rtl' if lang == 'ar' else 'ltr',
        'supported_langs': SUPPORTED_LANGS,
    }


@app.route('/set-language/<lang>')
def set_language(lang):
    """Switch the whole site's language and go back to wherever the user was."""
    if lang in TR:
        session['site_lang'] = lang
    next_url = request.referrer or url_for('index')
    return redirect(next_url)


# Login_required
def login_required(roles=None):
    if not isinstance(roles, (list, tuple)):
        roles = [roles] if roles else []

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'username' not in session:
                flash('Please login first', 'warning')
                # Remember where they were headed (e.g. /assistant/app,
                # the installed PWA's start_url) so login() can send them
                # straight back instead of dumping every login onto the
                # dashboard — matters a lot for a phone-installed app that
                # should reopen exactly where it left off.
                return redirect(url_for('login', next=request.path))
            if roles and session.get('role') not in roles:
                flash('Unauthorized access', 'danger')
                return redirect(url_for('login'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# ROUTES

@app.route('/stripe/connection_token', methods=['POST'])
@login_required(['admin', 'seller'])
def stripe_connection_token():
    """The Stripe Terminal JS SDK calls this (via onFetchConnectionToken)
    each time it needs to (re)connect to a reader. The secret it returns is
    short-lived and scoped to Terminal only, so it's safe to hand to the
    browser."""
    if not STRIPE_CONFIGURED:
        return jsonify({'error': 'Stripe is not configured on the server.'}), 503
    try:
        token = stripe.terminal.ConnectionToken.create()
        return jsonify({'secret': token.secret})
    except Exception as e:
        logger.exception('Could not create Stripe Terminal connection token')
        return jsonify({'error': str(e)}), 500


@app.route('/stripe/create_payment_intent', methods=['POST'])
@login_required(['admin', 'seller'])
def stripe_create_payment_intent():
    """Called from the register/debt-payment screen right before the
    cashier taps the reader, to create the PaymentIntent the reader will
    collect payment against."""
    if not STRIPE_CONFIGURED:
        return jsonify({'error': 'Stripe is not configured on the server.'}), 503
    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get('amount'))
    except (TypeError, ValueError):
        return jsonify({'error': 'amount must be a number'}), 400
    context = (data.get('context') or 'sale').strip()[:40]
    reference = (data.get('reference') or '').strip()[:120] or 'n/a'
    try:
        intent = create_stripe_terminal_payment_intent(amount, context, reference)
        return jsonify({'client_secret': intent.client_secret, 'id': intent.id})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception('Could not create Stripe PaymentIntent')
        return jsonify({'error': str(e)}), 500


@app.route('/')
def index():
    role = session.get('role')
    if role == 'admin':
        # The dashboard is now the app's home surface. The AI assistant
        # is still fully available — see /assistant — just no longer
        # what you land on when you open the app.
        return admin_dashboard()
    elif role == 'seller':
        return seller_dashboard()
    elif role == 'superadmin':
        return redirect(url_for('superadmin_dashboard'))
    else:
        return redirect(url_for('login'))


@app.route('/assistant')
@login_required(['admin', 'seller'])
def assistant_home():
    return render_template('home.html')


# Login
@app.route('/login', methods=['GET', 'POST'])
def login():
    # Only ever redirect to a same-site relative path (never a full URL) —
    # otherwise ?next=https://evil.example.com would be an open redirect.
    next_url = request.values.get('next', '')
    safe_next = next_url if next_url.startswith('/') and not next_url.startswith('//') else None

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = find_user(username)
        if user and check_password_hash(user['password'], password):
            if not user['activated']:
                flash('Your account is not activated yet.', 'warning')
                return redirect(url_for('login'))
            # Shop-scoped accounts (admin/seller) must belong to an active
            # shop. Superadmin accounts have shop_id = NULL and skip this.
            shop_id = user.get('shop_id')
            if user['role'] != 'superadmin' and shop_id is not None:
                shop = get_shop(shop_id)
                if not shop or shop['status'] != 'active':
                    flash('This shop is suspended. Contact the platform owner.', 'danger')
                    return redirect(url_for('login'))
            session['username'] = user['username']
            session['role'] = user['role']
            session['shop_id'] = shop_id
            log_audit('login', 'user', username, f'{username} logged in ({user["role"]})',
                       actor=username, module='auth')
            flash(f'Welcome {username}!', 'success')
            # Send a seller/admin back to the installed app (or whatever
            # page) they were trying to reach, e.g. /assistant/app, rather
            # than always landing on the dashboard.
            safe_next_post = request.form.get('next', '')
            if safe_next_post.startswith('/') and not safe_next_post.startswith('//'):
                return redirect(safe_next_post)
            return redirect(url_for('index'))
        else:
            flash('Invalid credentials', 'danger')
    return render_template('login.html', next=safe_next)


# Logout
@app.route('/logout')
def logout():
    username = session.get('username')
    if username:
        log_audit('logout', 'user', username, f'{username} logged out', actor=username, module='auth')
    session.clear()
    #flash('Logged out', 'success')
    return redirect(url_for('login'))


# ---------------------------------------------------------------------------
# Superadmin — manage shops. One deployment, one MySQL database, one login;
# no separate process/database for this anymore.
# ---------------------------------------------------------------------------

SHOP_PLANS = {
    "test":     {"name": "Test",     "price_eur": 0},
    "starter":  {"name": "Starter",  "price_eur": 29},
    "pro":      {"name": "Pro",      "price_eur": 59},
    "business": {"name": "Business", "price_eur": 99},
}


@app.route('/superadmin')
@login_required('superadmin')
def superadmin_dashboard():
    shops = list_shops()
    for shop in shops:
        shop_users = list_shop_users(shop['id'])
        shop['admin'] = next((u for u in shop_users if u['role'] == 'admin'), None)
        shop['seller_count'] = sum(1 for u in shop_users if u['role'] == 'seller')
        shop['plan_info'] = SHOP_PLANS.get(shop['plan'], SHOP_PLANS['test'])
    return render_template('superadmin/dashboard.html', shops=shops, plans=SHOP_PLANS)


@app.route('/superadmin/shops/new', methods=['GET', 'POST'])
@login_required('superadmin')
def new_shop():
    if request.method == 'POST':
        shop_name = (request.form.get('shop_name') or '').strip()
        admin_username = (request.form.get('admin_username') or '').strip()
        admin_password = request.form.get('admin_password') or ''
        plan = request.form.get('plan') or 'test'

        errors = []
        if not shop_name:
            errors.append('Shop name is required.')
        if not admin_username:
            errors.append('Admin username is required.')
        if len(admin_password) < 6:
            errors.append('Admin password must be at least 6 characters.')
        if plan not in SHOP_PLANS:
            errors.append('Invalid plan.')
        if find_user(admin_username):
            errors.append('That username is already taken.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('superadmin/new_shop.html', plans=SHOP_PLANS, form=request.form)

        shop_id = create_shop(shop_name, plan=plan, status='active')
        insert_user({
            'username': admin_username,
            'password': generate_password_hash(admin_password),
            'role': 'admin',
            'activated': True,
            'shop_id': shop_id,
        })
        log_audit('create', 'shop', shop_name, f'Shop "{shop_name}" created with admin {admin_username}',
                   actor=session.get('username'), module='superadmin')
        flash(f"'{shop_name}' created and activated on the {SHOP_PLANS[plan]['name']} plan. "
              f"Admin login: {admin_username}", 'success')
        return redirect(url_for('superadmin_dashboard'))

    return render_template('superadmin/new_shop.html', plans=SHOP_PLANS, form={})


@app.route('/superadmin/shops/<int:shop_id>/toggle', methods=['POST'])
@login_required('superadmin')
def toggle_shop(shop_id):
    shop = get_shop(shop_id)
    if not shop:
        flash('Shop not found', 'danger')
        return redirect(url_for('superadmin_dashboard'))
    new_status = toggle_shop_status(shop_id)
    log_audit('update', 'shop', shop['name'], f'Shop "{shop["name"]}" set to {new_status}',
               actor=session.get('username'), module='superadmin')
    flash(f"{shop['name']} is now {new_status}.", 'success')
    return redirect(url_for('superadmin_dashboard'))


@app.route('/superadmin/shops/<int:shop_id>')
@login_required('superadmin')
def shop_detail(shop_id):
    shop = get_shop(shop_id)
    if not shop:
        flash('Shop not found', 'danger')
        return redirect(url_for('superadmin_dashboard'))
    users = list_shop_users(shop_id)
    admins = [u for u in users if u.get('role') == 'admin']
    features = get_shop_features(shop_id)
    return render_template('superadmin/shop_detail.html', shop=shop, users=users, admins=admins, features=features)


@app.route('/superadmin/shops/<int:shop_id>/features/<feature>/toggle', methods=['POST'])
@login_required('superadmin')
def toggle_shop_feature(shop_id, feature):
    shop = get_shop(shop_id)
    if not shop:
        flash('Shop not found', 'danger')
        return redirect(url_for('superadmin_dashboard'))
    if feature not in DEFAULT_SHOP_FEATURES:
        flash('Unknown feature.', 'danger')
        return redirect(url_for('shop_detail', shop_id=shop_id))

    current = get_shop_features(shop_id)
    new_value = not current.get(feature, True)
    set_shop_feature(shop_id, feature, new_value)
    log_audit('update', 'shop', shop['name'],
              f'Feature "{feature}" {"granted" if new_value else "revoked"} for shop "{shop["name"]}"',
              actor=session.get('username'), module='superadmin')
    flash(f'{feature} is now {"enabled" if new_value else "disabled"} for {shop["name"]}.', 'success')
    return redirect(url_for('shop_detail', shop_id=shop_id))


@app.route('/superadmin/shops/<int:shop_id>/logo', methods=['POST'])
@login_required('superadmin')
def set_shop_logo_route(shop_id):
    shop = get_shop(shop_id)
    if not shop:
        flash('Shop not found', 'danger')
        return redirect(url_for('superadmin_dashboard'))
    logo_url = (request.form.get('logo_url') or '').strip()
    set_shop_logo(shop_id, logo_url)
    log_audit('update', 'shop', shop['name'], f'Logo updated for shop "{shop["name"]}"',
              actor=session.get('username'), module='superadmin')
    flash('Logo updated.', 'success')
    return redirect(url_for('shop_detail', shop_id=shop_id))


@app.route('/superadmin/shops/<int:shop_id>/admin/add', methods=['POST'])
@login_required('superadmin')
def add_shop_admin(shop_id):
    """Add an admin login to a shop — e.g. its original admin account was
    deleted, or it needs a second one."""
    shop = get_shop(shop_id)
    if not shop:
        flash('Shop not found', 'danger')
        return redirect(url_for('superadmin_dashboard'))

    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    if not username or len(password) < 6:
        flash('Username is required and password must be at least 6 characters.', 'danger')
        return redirect(url_for('shop_detail', shop_id=shop_id))
    if find_user(username):
        flash('That username is already taken.', 'danger')
        return redirect(url_for('shop_detail', shop_id=shop_id))

    insert_user({
        'username': username,
        'password': generate_password_hash(password),
        'role': 'admin',
        'activated': True,
        'shop_id': shop_id,
    })
    log_audit('create', 'account', username, f'Admin login "{username}" added to shop "{shop["name"]}"',
              actor=session.get('username'), module='superadmin')
    flash(f'Admin login "{username}" added.', 'success')
    return redirect(url_for('shop_detail', shop_id=shop_id))


@app.route('/superadmin/shops/<int:shop_id>/admin/<username>/rename', methods=['POST'])
@login_required('superadmin')
def rename_shop_admin(shop_id, username):
    shop = get_shop(shop_id)
    user = find_user(username)
    if not shop or not user or user.get('shop_id') != shop_id:
        flash('Account not found in this shop.', 'danger')
        return redirect(url_for('superadmin_dashboard'))

    new_username = (request.form.get('new_username') or '').strip()
    if not new_username:
        flash('New username cannot be empty.', 'danger')
        return redirect(url_for('shop_detail', shop_id=shop_id))

    try:
        rename_username(username, new_username)
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(url_for('shop_detail', shop_id=shop_id))
    except Exception:
        logger.exception('Failed to rename %s to %s', username, new_username)
        flash('Could not rename this account — nothing was changed.', 'danger')
        return redirect(url_for('shop_detail', shop_id=shop_id))

    log_audit('update', 'account', new_username, f'"{username}" renamed to "{new_username}"',
              actor=session.get('username'), module='superadmin')
    flash(f'"{username}" renamed to "{new_username}".', 'success')
    return redirect(url_for('shop_detail', shop_id=shop_id))


@app.route('/superadmin/shops/<int:shop_id>/admin/<username>/reset-password', methods=['POST'])
@login_required('superadmin')
def reset_shop_admin_password(shop_id, username):
    shop = get_shop(shop_id)
    user = find_user(username)
    if not shop or not user or user.get('shop_id') != shop_id:
        flash('Account not found in this shop.', 'danger')
        return redirect(url_for('superadmin_dashboard'))

    new_password = request.form.get('new_password') or ''
    if len(new_password) < 6:
        flash('New password must be at least 6 characters.', 'danger')
        return redirect(url_for('shop_detail', shop_id=shop_id))

    set_user_password(username, generate_password_hash(new_password))
    log_audit('update', 'account', username, f'Password reset for "{username}" by superadmin',
              actor=session.get('username'), module='superadmin')
    flash(f'Password updated for "{username}".', 'success')
    return redirect(url_for('shop_detail', shop_id=shop_id))


@app.route('/superadmin/shops/<int:shop_id>/admin/<username>/delete', methods=['POST'])
@login_required('superadmin')
def delete_shop_admin(shop_id, username):
    shop = get_shop(shop_id)
    user = find_user(username)
    if not shop or not user or user.get('shop_id') != shop_id:
        flash('Account not found in this shop.', 'danger')
        return redirect(url_for('superadmin_dashboard'))
    if user['role'] == 'superadmin':
        flash('Cannot delete a superadmin account.', 'danger')
        return redirect(url_for('shop_detail', shop_id=shop_id))

    delete_user(username)
    log_audit('delete', 'account', username, f'"{username}" deleted from shop "{shop["name"]}" by superadmin',
              actor=session.get('username'), module='superadmin')
    flash(f'"{username}" deleted.', 'success')
    return redirect(url_for('shop_detail', shop_id=shop_id))


@app.route('/superadmin/accounts')
@login_required('superadmin')
def superadmin_accounts():
    accounts = list_all_accounts()
    return render_template('superadmin/accounts.html', accounts=accounts)


@app.route('/superadmin/accounts/<username>/toggle', methods=['POST'])
@login_required('superadmin')
def toggle_account(username):
    user = find_user(username)
    if not user:
        flash('Account not found', 'danger')
        return redirect(url_for('superadmin_accounts'))
    if user['role'] == 'superadmin':
        flash('Cannot deactivate a superadmin account.', 'danger')
        return redirect(url_for('superadmin_accounts'))
    new_state = not user['activated']
    set_user_activated(username, new_state)
    log_audit('update', 'account', username,
               f'{username} {"activated" if new_state else "deactivated"} by superadmin',
               actor=session.get('username'), module='superadmin')
    flash(f'{username} is now {"activated" if new_state else "deactivated"}.', 'success')
    return redirect(url_for('superadmin_accounts'))


@app.route('/superadmin/accounts/<username>/delete', methods=['POST'])
@login_required('superadmin')
def delete_account(username):
    user = find_user(username)
    if not user:
        flash('Account not found', 'danger')
        return redirect(url_for('superadmin_accounts'))
    if user['role'] == 'superadmin':
        flash('Cannot delete a superadmin account.', 'danger')
        return redirect(url_for('superadmin_accounts'))
    delete_user(username)
    log_audit('delete', 'account', username, f'{username} deleted by superadmin',
               actor=session.get('username'), module='superadmin')
    flash(f'{username} deleted.', 'success')
    return redirect(url_for('superadmin_accounts'))


# Date Time Format 
@app.template_filter('datetimeformat')
def datetimeformat(value, format='%d.%m.%Y %H:%M'):
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime(format)
    except Exception:
        return value

# calculate_all_time_profit
def calculate_all_time_profit(sales, items):
    barcode_map = {item['barcode']: item.get('purchase_price', 0) for item in items}
    profit = 0.0
    for s in sales:
        barcode = s.get('barcode')
        purchase_price = barcode_map.get(barcode, 0)
        sale_price = s.get('sale_price', 0)
        quantity = s.get('quantity', 0)
        profit += (sale_price - purchase_price) * quantity
    return round(profit, 2)




@app.route('/admin/save_closing_balance')
@login_required('admin')
def admin_save_closing_balance():
    
    flash("Today's closing balance saved successfully.")
    return redirect(url_for('admin_dashboard'))


@app.route('/admin')
@login_required('admin')
def admin_dashboard():
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    month_start = datetime(today.year, today.month, 1).date()

    calculate_and_save_today_closing_balance()

    def safe_get_date(p):
        date_str = p.get('order_date') or p.get('date')
        if not isinstance(date_str, str):
            return None
        try:
            return datetime.fromisoformat(date_str).date()
        except Exception:
            return None

    sales = load_sales()
    purchases = load_orders()

    items = load_items()
    debts = load_debts()

    dismissed = set(session.get('dismissed_notifications', []))

    closing_balance_yesterday = get_kasse_balance_for_date(yesterday) or 0


    taegliche_einnahmen = calculate_sales_for_date(today) or 0
    daily_purchases_total = calculate_purchases_for_date(today) or 0
    daily_cash_debt_payments = calculate_cash_debt_payments_for_date(today) or 0
    daily_cash_salary_payments = calculate_cash_salary_payments_for_date(today) or 0

    monthly_purchases_total = round(sum(
        float(p.get('total_price', 0)) for p in purchases
        if (purchase_date := safe_get_date(p)) is not None and purchase_date >= month_start
    ), 2)

    monatliche_einnahmen = calculate_monthly_sales()
    heutiger_gewinn = calculate_today_profit()

    # Kasse is the physical cash drawer: only cash-tagged manual entries
    # count (a "card" manual transaction doesn't move physical cash).
    einzahlungen_result = fetch_one("""
        SELECT SUM(amount) AS total FROM cash_transactions 
        WHERE type = 'einzahlung' AND date >= %s
          AND COALESCE(payment_method, 'cash') = 'cash';
    """, (month_start,))
    monatliche_einzahlungen_kasse = float(einzahlungen_result['total'] or 0)

    auszahlungen_result = fetch_one("""
        SELECT SUM(amount) AS total FROM cash_transactions 
        WHERE type = 'auszahlung' AND date >= %s
          AND COALESCE(payment_method, 'cash') = 'cash';
    """, (month_start,))
    monatliche_auszahlungen_kasse = float(auszahlungen_result['total'] or 0)
    # NOTE: monatliche_einzahlungen_kasse/monatliche_auszahlungen_kasse above
    # are "this month so far" totals used purely for the stat cards/chart
    # further down this page — they are NOT used in the running cash
    # balance below anymore. They used to be added into that balance as a
    # patch, but calculate_and_save_today_closing_balance() now folds each
    # day's net cash deposits/withdrawals into the rolling balance itself,
    # so closing_balance_yesterday already correctly includes every prior
    # month's manual transactions. Re-adding "this month's" total on top
    # double-counted the current month AND, worse, silently dropped every
    # PRIOR month's deposits/withdrawals from the total the moment the
    # month rolled over. Only today's net transactions need adding below.
    todays_cash_deposits = calculate_cash_deposits_for_date(today)
    todays_cash_withdrawals = calculate_cash_withdrawals_for_date(today)

    # Cash in the box today: yesterday's closing balance (already includes
    # every prior day's cash movements), plus today's cash sales and cash
    # debt collections, minus today's cash purchases and any salary paid
    # out of the drawer, plus/minus today's manual cash deposits/
    # withdrawals. Card sales/purchases and "Privat"-sourced salaries never
    # touch this number — same rule as Kasse everywhere else in the app.
    current_cash_in_box = round(
        closing_balance_yesterday + taegliche_einnahmen - daily_purchases_total
        + daily_cash_debt_payments - daily_cash_salary_payments
        + todays_cash_deposits - todays_cash_withdrawals,
        2
    )

    unpaid_sum = round(sum(float(d['amount']) for d in debts if not d.get('paid', False)), 2)

    low_stock_notes = get_low_stock_notifications(items, threshold=5)
    old_stock_notes = get_old_stock_notifications(items, days_old=21)
    warehouse_notifications = [

        note for note in (low_stock_notes + old_stock_notes)
        if note.get('barcode') not in dismissed
    ]
    mailbox_notifications = [
        {
            'date': today.isoformat(),
            'message': note.get('message', 'Keine Nachricht'),
            'type': note.get('type', 'info'),
            'barcode': note.get('barcode')
        }
        for note in warehouse_notifications
    ]

    sales_sorted = sorted(sales, key=lambda x: x.get('date', datetime.min), reverse=True)
    purchases_sorted = sorted(purchases, key=lambda x: x.get('order_date', datetime.min), reverse=True)
    total_order_sum = round(sum(float(p.get('total_price', 0)) for p in purchases), 2)

    # New live dashboard cards (Today's Sales/Purchases/Profit, Inventory
    # Value, Outstanding/Paid Debt, Cash/Card Debt Payments, Cash/Card
    # Withdrawals, Low Stock Products) — one consolidated, efficient call;
    # see get_dashboard_stats() in db.py for why this is only 6 queries
    # total instead of one query (or a full Python loop) per card.
    live_stats = get_dashboard_stats()

    # Render the template with all data
    return render_template(
        "admin_dashboard.html",
        heutiger_gewinn=heutiger_gewinn,
        taegliche_einnahmen=taegliche_einnahmen,
        monatliche_einnahmen=monatliche_einnahmen,
        wallet_balance=current_cash_in_box,
        daily_purchases_total=daily_purchases_total,
        monthly_purchases_total=monthly_purchases_total,
        berechneter_kassenstand=current_cash_in_box,
        sales=sales_sorted,
        purchases=purchases_sorted,
        mailbox_notifications=mailbox_notifications,
        warehouse_notifications=warehouse_notifications,
        total_order_sum=total_order_sum,
        unpaid_sum=unpaid_sum,
        live_stats=live_stats,
    )


@app.route('/admin/dashboard/stats')
@login_required('admin')
def admin_dashboard_stats():
    """JSON endpoint the dashboard polls to keep the live cards current
    without a full page reload. Always computed fresh (see
    get_dashboard_stats() — no caching), so it reflects any create/update/
    delete made anywhere in the app, including via the chat assistant,
    immediately on the next poll."""
    return jsonify(get_dashboard_stats())




@app.template_filter('to_float')
def to_float_filter(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# Format_currency_de
def format_currency_de(amount):
    return f"€{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

#Saving Everyday History
def save_dashboard_snapshot(date, daily_profit, monthly_profit, wallet_balance, all_time_profit):
    history_file = os.path.join('data', 'dashboard_history.json')
    
    if os.path.exists(history_file):
        with open(history_file, 'r', encoding='utf-8') as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = []
    else:
        history = []

    # Avoid duplicate entry for the same day
    if any(entry.get("date") == date.isoformat() for entry in history):
        return

    history.append({
        "date": date.isoformat(),
        "daily_profit": round(daily_profit, 2),
        "monthly_profit": round(monthly_profit, 2),
        "wallet_balance": round(wallet_balance, 2),
        "all_time_profit": round(all_time_profit, 2)
    })

    with open(history_file, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

#log_wallet_change
def log_wallet_change(amount, change_type="manual"):
    wallet_file = os.path.join('data', 'wallet_log.json')

    # Load old log
    if os.path.exists(wallet_file):
        with open(wallet_file, 'r', encoding='utf-8') as f:
            try:
                log = json.load(f)
            except json.JSONDecodeError:
                log = []
    else:
        log = []

    # Get the username from session
    username = session.get('username', 'unknown')

    # Append new entry
    log.append({
        "date": datetime.now().isoformat(),
        "change_type": change_type,
        "amount": round(amount, 2),
        "user": username
    })

    # Save updated log
    with open(wallet_file, 'w', encoding='utf-8') as f:
        json.dump(log, f, indent=2, ensure_ascii=False)




 # Generate CSV
#Log generate_csv
def generate_csv(data, fieldnames):
    """Generate CSV response from list of dicts."""
    def generate():
        yield ",".join(fieldnames) + "\n"
        for row in data:
            yield ",".join(str(row.get(f, "")) for f in fieldnames) + "\n"
    return Response(generate(), mimetype='text/csv')

@app.route('/download/sales.csv')
@login_required(['admin', 'seller'])
def download_sales_csv():
    current_user = session.get('username')
    sales = load_sales()

    # Filter sales for current user
    user_sales = [sale for sale in sales if sale.get('user') == current_user]

    rows = []
    for sale in user_sales:
        sale_date = sale.get('date')
        for item in sale.get('items', []):
            rows.append({
                'date': sale_date,
                'product_name': item.get('product_name'),
                'quantity': item.get('quantity'),
                'price': item.get('sale_price'),
                'total_price': item.get('total_price'),
            })

    fieldnames = ['date', 'product_name', 'quantity', 'price', 'total_price']
    return generate_csv(rows, fieldnames)


@app.route('/download/purchases.csv')
@login_required(['admin', 'seller'])
def download_purchases_csv():
    current_user = session.get('username')

    # Use the same function you use in the dashboard
    user_purchases = get_purchases_for_user(current_user)

    rows = []
    for purchase in user_purchases:
        purchase_date = purchase.get('date') or purchase.get('order_date')
        rows.append({
            'date': purchase_date,
            'product_name': purchase.get('product_name', 'Unbekannt'),
            'quantity': purchase.get('quantity', 0),
            'price': purchase.get('purchase_price') or purchase.get('price', 0),
            'total_price': (purchase.get('purchase_price') or purchase.get('price', 0)) * purchase.get('quantity', 0),
        })

    fieldnames = ['date', 'product_name', 'quantity', 'price', 'total_price']
    return generate_csv(rows, fieldnames)


# Admin: List Sellers
@app.route('/admin/sellers')
@login_required('admin')
def list_sellers():
    # Only ever list role == 'seller' accounts here. Admin accounts (even
    # the current one) must never appear in this table — seeing or managing
    # other admins is reserved for superadmin via /superadmin/accounts.
    sellers = [s for s in load_users() if s.get('role') == 'seller']
    current_username = session.get('username')
    for seller in sellers:
        seller.setdefault('salary', 0.0)
        seller.setdefault('profile_img', '')
        seller.setdefault('activated', False)
        # Kept as a defense-in-depth flag even though an admin's own row
        # can no longer appear here (role filter above already excludes it).
        seller['is_self'] = (seller['username'] == current_username)
    return render_template('sellers.html', sellers=sellers)

# Add  user
from werkzeug.security import generate_password_hash, check_password_hash


def _read_permission_categories_from_form(form):
    """Which of the 7 'KI-Assistent — zusätzliche Berechtigungen' checkboxes
    were ticked on the Add/Edit Seller form. Checkbox names are
    perm_<category>, matching the keys of PERMISSION_CATEGORY_TOOLS."""
    return {cat for cat in PERMISSION_CATEGORY_TOOLS if f'perm_{cat}' in form}


@app.route('/admin/sellers/add', methods=['GET', 'POST'])
@login_required('admin')
def add_seller():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        profile_img = request.form.get('profile_img', '').strip()
        salary_str = request.form.get('salary', '').strip()
        activated = 'activated' in request.form
        granted_categories = _read_permission_categories_from_form(request.form)

        if not username:
            flash('Benutzername darf nicht leer sein.', 'danger')
            return redirect(url_for('add_seller'))

        if not password:
            flash('Passwort darf nicht leer sein.', 'danger')
            return redirect(url_for('add_seller'))

        try:
            salary = float(salary_str) if salary_str else 0.0
            if salary < 0:
                raise ValueError("Gehalt darf nicht negativ sein.")
        except ValueError as e:
            flash(f'Ungültiges Gehalt: {e}', 'danger')
            return redirect(url_for('add_seller'))

        if find_user(username):
            flash('Benutzername existiert bereits.', 'danger')
            return redirect(url_for('add_seller'))

        # Hash the password before storing
        hashed_password = generate_password_hash(password)

        new_seller = {
            'username': username,
            'password': hashed_password,  # store hashed password here
            'role': 'seller',
            'profile_img': profile_img,
            'salary': salary,
            'activated': activated
        }

        insert_user(new_seller)
        set_seller_permission_categories(username, granted_categories)
        log_audit('seller_created', 'seller', username,
                   format_seller_details(username, salary),
                   actor=session.get('username'), module='sellers')

        flash('Verkäufer erfolgreich hinzugefügt!', 'success')
        return redirect(url_for('list_sellers'))

    return render_template('add_seller.html')



# Admin: Edit Seller
@app.route('/admin/sellers/edit/<username>', methods=['GET', 'POST'])
@login_required('admin')
def edit_seller(username):
    sellers = load_users()
    seller = next((s for s in sellers if s['username'] == username), None)
    if not seller:
        flash('Seller not found', 'danger')
        return redirect(url_for('list_sellers'))

    # This endpoint is for managing sellers only. Admin/superadmin accounts
    # can't be viewed or edited here, even by guessing the URL directly —
    # that's reserved for superadmin via /superadmin/accounts.
    if seller.get('role') != 'seller':
        flash('Unauthorized access', 'danger')
        return redirect(url_for('list_sellers'))

    # An admin editing their own account can't touch their own salary —
    # only someone else's admin/edit action should ever change it.
    is_self = (username == session.get('username'))

    if request.method == 'POST':
        seller['profile_img'] = request.form.get('profile_img', seller.get('profile_img', ''))
        seller['activated'] = 'activated' in request.form

        if is_self:
            # Ignore whatever was submitted for salary; keep the existing value.
            new_salary = seller.get('salary', 0.0)
        else:
            new_salary = float(request.form.get('salary', seller.get('salary', 0.0)))
        seller['salary'] = new_salary

        update_user(username, {
            'profile_img': request.form.get('profile_img', ''),
            'salary': new_salary,
            'activated': 'activated' in request.form
        })
        set_seller_permission_categories(username, _read_permission_categories_from_form(request.form))
        log_audit('seller_updated', 'seller', username,
                   format_seller_details(username, seller['salary']),
                   actor=session.get('username'), module='sellers')

        if is_self:
            flash('Profil aktualisiert. Ihr eigenes Gehalt kann nicht über dieses Formular geändert werden.', 'info')
        else:
            flash('Seller updated successfully', 'success')
        return redirect(url_for('list_sellers'))

    return render_template('edit_seller.html', seller=seller, is_self=is_self,
                           granted_categories=get_seller_permission_categories(username))

# Admin: Delete Seller
@app.route('/admin/sellers/delete/<username>', methods=['POST'])
@login_required('admin')
def delete_seller(username):
    target = find_user(username)
    if not target or target.get('role') != 'seller':
        flash('Unauthorized access', 'danger')
        return redirect(url_for('list_sellers'))
    delete_user(username)
    log_audit('seller_deleted', 'seller', username, username, actor=session.get('username'), module='sellers')
    flash('Seller deleted successfully', 'success')
    return redirect(url_for('list_sellers'))


@app.route('/admin/items')
@login_required('admin')
def list_items():
    query = (request.args.get('q') or '').strip()

    if query:
        # Server-side search — barcode/SKU exact matches first, then
        # substring matches on name/barcode/SKU (see search_items() in
        # db.py, the single search implementation shared with the chat
        # assistant and the external API). Ordered by relevance, so it is
        # NOT reversed like the plain "browse everything" list below.
        items = search_items(query, limit=200)
    else:
        items = load_items()
        items = items[::-1]

    total_purchase_value = 0  # Initialize total

    for item in items:
        product_name = item.get('product_name', '').strip()
        if not product_name:
            product_name = "Unnamed product"
        item['product_name'] = product_name

        item['barcode'] = item.get('barcode', '')
        item['sku'] = item.get('sku') or ''

        purchase_price = float(item.get('purchase_price', 0) or 0)
        selling_price = float(item.get('selling_price', 0) or 0)
        min_selling_price = float(item.get('min_selling_price', 0) or 0)

        quantity = int(item.get('quantity', 0) or 0)

        item['purchase_price'] = purchase_price
        item['selling_price'] = selling_price
        item['min_selling_price'] = min_selling_price
        item['quantity'] = quantity

        item['description'] = item.get('description', '')
        item['photo_link'] = item.get('photo_link', '')
        item['item_condition'] = (item.get('item_condition') or 'neu').strip().lower()

        # Add to total purchase value
        total_purchase_value += purchase_price * quantity

    return render_template('items.html', items=items, total_purchase_value=total_purchase_value, search_query=query)







@app.route('/static/barcodes/code_barres_<barcode_value>.png')
@login_required(['admin', 'seller'])
def barcode_print_legacy_static(barcode_value):
    """Fallback for a filename pattern (`code_barres_<value>.png` served as
    a literal static file) that a stale build of the frontend may still be
    requesting directly instead of calling /admin/items/barcode_print/...
    — the actual purchase-order flow (add_order) writes real files at this
    exact path via barcode.save(), but item barcodes were never written
    there, only ever generated on the fly. Rather than chase down which
    old template/JS is still constructing this URL, just make the path
    work either way: serve the real file if the order flow already wrote
    one, otherwise generate the same CODE128 image the dynamic route
    would. Registered as a literal path (not a wildcard), so Werkzeug
    routes requests here in preference to the generic /static/<path:...>
    handler — this does not shadow any other file under /static/barcodes/.
    """
    static_path = os.path.join(app.static_folder, 'barcodes', f'code_barres_{barcode_value}.png')
    if os.path.isfile(static_path):
        return send_file(static_path, mimetype='image/png')
    return barcode_print(barcode_value)


@app.route('/admin/items/barcode_print/<barcode_value>')
@login_required(['admin', 'seller'])
def barcode_print(barcode_value):
    try:
        CODE128 = barcode.get_barcode_class('code128')
        img_io = io.BytesIO()

        code = CODE128(barcode_value, writer=ImageWriter())
        code.write(img_io)
        img_io.seek(0)

        return send_file(
            img_io,
            mimetype='image/png',
            as_attachment=False
        )
    except Exception:
        logger.exception("Failed to generate barcode image for %s", barcode_value)
        abort(404, description="Invalid barcode")


@app.route('/admin/items/barcode_label/<barcode_value>')
@login_required(['admin', 'seller'])
def barcode_label(barcode_value):
    """Serve a print-ready HTML page with the barcode image and product name,
    auto-triggering the browser's print dialog on load.

    This avoids the popup-blocker issues that plague `window.open()`-based
    approaches (see items.html/list_orders.html) because the user clicks a
    plain `<a target="_blank">` link  — browsers never block those.
    """
    # Look up the product name from the database (gracefully fall back to
    # the barcode value itself if the product was deleted or not found).
    product_name = barcode_value
    try:
        item = find_item(barcode_value)
        if item:
            product_name = (item.get('product_name') or '').strip() or barcode_value
    except Exception:
        pass

    barcode_url = url_for('barcode_print', barcode_value=barcode_value, _external=True)
    title = _('Barcode Label') if '_' in dir() else 'Barcode Label'

    page_html = f'''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<title>{html.escape(product_name)} — Barcode</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 100vh;
    font-family: Arial, sans-serif;
  }}
  .label {{
    width: 60mm;
    padding: 5mm;
    text-align: center;
  }}
  .label img {{
    width: 50mm;
    height: auto;
    display: block;
    margin: 0 auto 3mm;
  }}
  .label .product-name {{
    font-size: 10pt;
    font-weight: bold;
    text-align: center;
    word-wrap: break-word;
  }}
  .label .barcode-text {{
    font-size: 7pt;
    color: #555;
    text-align: center;
    margin-top: 1mm;
    font-family: monospace;
  }}
  @media print {{
    body {{ margin: 0; padding: 0; }}
    .label {{ width: 60mm; padding: 5mm; }}
    .label img {{ width: 50mm; }}
  }}
</style>
</head>
<body onload="window.print()">
<div class="label">
  <img src="{html.escape(barcode_url)}" alt="Barcode {html.escape(barcode_value)}"
       onerror="this.outerHTML='<div style=\\'color:red;padding:10px;\\'>Barcode: {html.escape(barcode_value)}</div>'">
  <div class="product-name">{html.escape(product_name)}</div>
  <div class="barcode-text">{html.escape(barcode_value)}</div>
</div>
</body>
</html>'''
    return Response(page_html, mimetype='text/html')




# Add Item
@app.route('/admin/items/add', methods=['GET', 'POST'])
@login_required('admin')
def add_item():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        barcode_value = (request.form.get('barcode') or '').strip()
        sku = (request.form.get('sku') or '').strip()
        description = (request.form.get('description') or '').strip()

        errors = []
        if not name:
            errors.append('Produktname ist erforderlich.')

        def parse_float(field_name, label, required=True, allow_negative=False):
            raw = (request.form.get(field_name) or '').strip()
            if not raw:
                if required:
                    errors.append(f'{label} ist erforderlich.')
                return None
            try:
                value = float(raw)
            except ValueError:
                errors.append(f'{label} muss eine Zahl sein.')
                return None
            if not allow_negative and value < 0:
                errors.append(f'{label} darf nicht negativ sein.')
                return None
            return value

        def parse_int(field_name, label, required=True):
            raw = (request.form.get(field_name) or '').strip()
            if not raw:
                if required:
                    errors.append(f'{label} ist erforderlich.')
                return None
            try:
                value = int(raw)
            except ValueError:
                errors.append(f'{label} muss eine ganze Zahl sein.')
                return None
            if value < 0:
                errors.append(f'{label} darf nicht negativ sein.')
                return None
            return value

        purchase_price = parse_float('purchase_price', 'Einkaufspreis')
        selling_price = parse_float('selling_price', 'Verkaufspreis')
        min_selling_price = parse_float('min_selling_price', 'Mindestverkaufspreis', required=False)
        quantity = parse_int('quantity', 'Menge')

        if not errors and min_selling_price is None:
            min_selling_price = selling_price

        # Empty barcode = auto-generate one, same behaviour as the
        # assistant's add-item tool, so both entry points stay consistent.
        if not errors and not barcode_value:
            barcode_value = _generate_item_barcode()

        # Check for existing barcode in DB (only once the barcode itself
        # is known, whether typed or auto-generated).
        if not errors and query_one("SELECT barcode FROM products WHERE barcode = %s", (barcode_value,)):
            errors.append('Dieser Barcode existiert bereits.')

        # SKU is optional, but if given it must be unique too — otherwise
        # "search by SKU" could return more than one product for one code.
        if not errors and sku and query_one("SELECT barcode FROM products WHERE sku = %s", (sku,)):
            errors.append('Diese SKU existiert bereits.')

        if errors:
            for message in errors:
                flash(message, 'danger')
            return render_template('add_item.html', form=request.form)

        item_condition = (request.form.get('item_condition') or 'neu').strip().lower()
        if item_condition not in ('neu', 'gebraucht', 'defekt'):
            item_condition = 'neu'

        username = session.get('username', 'anonymous')  # get from session
        insert_query = """
            INSERT INTO products 
            (product_name, description, quantity, barcode, sku, purchase_price, selling_price, min_selling_price, date_added, item_condition)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        params = (
            name,
            description,
            quantity,
            barcode_value,
            sku or None,
            purchase_price,
            selling_price,
            min_selling_price,
            datetime.now().strftime('%Y-%m-%d'),  # date_added
            item_condition,
        )
        try:
            execute_query(insert_query, params)
        except Exception:
            logger.exception('Failed to add item %r', name)
            flash('Datenbankfehler beim Hinzufügen des Artikels. Bitte erneut versuchen.', 'danger')
            return render_template('add_item.html', form=request.form)

        log_audit('product_created', 'item', barcode_value,
                   format_product_details(name, barcode_value, sku, quantity, selling_price),
                   actor=username, module='inventory')

        flash(f'Artikel "{name}" wurde erfolgreich hinzugefügt (Barcode {barcode_value}).', 'success')
        return redirect(url_for('list_items'))
    return render_template('add_item.html')


# Update Item
@app.route('/edit_item/<path:barcode_value>', methods=['GET', 'POST'])
@login_required('admin')
def edit_item(barcode_value):
    # Fetch the item from DB by its barcode (now the primary key)
    item = find_item(barcode_value)
    if not item:
        flash('Artikel wurde nicht gefunden.', 'danger')
        return redirect(url_for('list_items'))

    if request.method == 'POST':
        # Determine if barcode is being edited or not
        edit_barcode = request.form.get('edit_barcode') == 'on'

        # If not editing barcode, keep old barcode
        new_barcode_value = (request.form.get('barcode') if edit_barcode else request.form.get('old_barcode')) or ''
        new_barcode_value = new_barcode_value.strip()

        product_name = (request.form.get('product_name') or '').strip()
        sku = (request.form.get('sku') or '').strip()
        description = (request.form.get('description') or '').strip()
        photo_link = (request.form.get('photo_link') or '').strip()

        errors = []

        def parse_float(field_name, label, required=True, allow_negative=False):
            raw = (request.form.get(field_name) or '').strip()
            if not raw:
                if required:
                    errors.append(f'{label} ist erforderlich.')
                return None
            try:
                value = float(raw)
            except ValueError:
                errors.append(f'{label} muss eine Zahl sein.')
                return None
            if not allow_negative and value < 0:
                errors.append(f'{label} darf nicht negativ sein.')
                return None
            return value

        def parse_int(field_name, label, required=True):
            raw = (request.form.get(field_name) or '').strip()
            if not raw:
                if required:
                    errors.append(f'{label} ist erforderlich.')
                return None
            try:
                value = int(raw)
            except ValueError:
                errors.append(f'{label} muss eine ganze Zahl sein.')
                return None
            if value < 0:
                errors.append(f'{label} darf nicht negativ sein.')
                return None
            return value

        purchase_price = parse_float('purchase_price', 'Einkaufspreis')
        selling_price = parse_float('selling_price', 'Verkaufspreis')
        min_selling_price = parse_float('min_selling_price', 'Mindestverkaufspreis', required=False)
        quantity = parse_int('quantity', 'Menge')

        if not errors and min_selling_price is None:
            min_selling_price = selling_price

        if not product_name:
            errors.append('Produktname ist erforderlich.')
        if not new_barcode_value:
            errors.append('Barcode ist erforderlich.')

        # Barcode must stay unique across products, excluding this item.
        if not errors:
            clash = query_one(
                "SELECT barcode FROM products WHERE barcode = %s AND barcode != %s",
                (new_barcode_value, barcode_value),
            )
            if clash:
                errors.append('Dieser Barcode wird bereits von einem anderen Artikel verwendet.')

        # SKU is optional, but if given it must stay unique too, excluding
        # this item (so re-saving the item's own unchanged SKU is fine).
        if not errors and sku:
            sku_clash = query_one(
                "SELECT barcode FROM products WHERE sku = %s AND barcode != %s",
                (sku, barcode_value),
            )
            if sku_clash:
                errors.append('Diese SKU wird bereits von einem anderen Artikel verwendet.')

        if errors:
            for message in errors:
                flash(message, 'danger')
            # Re-render with the submitted (unsaved) values so nothing the
            # user typed gets lost on a validation error.
            item_view = dict(item)
            item_view.update({
                'product_name': product_name,
                'barcode': new_barcode_value,
                'sku': sku,
                'description': description,
                'photo_link': photo_link,
            })
            return render_template('edit_item.html', item=item_view)

        item_condition = (request.form.get('item_condition') or 'neu').strip().lower()
        if item_condition not in ('neu', 'gebraucht', 'defekt'):
            item_condition = 'neu'

        updates = {
            'product_name': product_name,
            'barcode': new_barcode_value,
            'sku': sku or None,
            'purchase_price': purchase_price,
            'selling_price': selling_price,
            'min_selling_price': min_selling_price,
            'quantity': quantity,
            'description': description,
            'photo_link': photo_link,
            'item_condition': item_condition,
        }

        try:
            update_item(barcode_value, updates)
        except Exception:
            logger.exception('Failed to update item %s', barcode_value)
            flash('Datenbankfehler beim Aktualisieren des Artikels. Bitte erneut versuchen.', 'danger')
            return render_template('edit_item.html', item=item)

        log_audit('product_updated', 'item', new_barcode_value,
                   format_product_details(product_name, new_barcode_value, sku, quantity, selling_price),
                   actor=session.get('username'), module='inventory')

        flash('Artikel erfolgreich aktualisiert.', 'success')
        return redirect(url_for('list_items'))

    return render_template('edit_item.html', item=item)


 # Delete Item
# Route handler
@app.route('/admin/items/delete/<path:barcode_value>', methods=['POST'])
@login_required('admin')
def delete_item_route(barcode_value):
    item = find_item(barcode_value)
    if not item:
        flash('Artikel wurde nicht gefunden (eventuell bereits gelöscht).', 'danger')
        return redirect(url_for('list_items'))

    try:
        db_delete_item(barcode_value)
    except Exception:
        logger.exception('Failed to delete item %s', barcode_value)
        flash('Datenbankfehler beim Löschen des Artikels. Bitte erneut versuchen.', 'danger')
        return redirect(url_for('list_items'))

    log_audit('product_deleted', 'item', barcode_value,
              format_product_details(item.get('product_name'), item.get('barcode'), item.get('sku')),
              actor=session.get('username'), module='inventory')
    flash(f"Artikel \"{item.get('product_name') or barcode_value}\" wurde erfolgreich gelöscht.", "success")

    items = load_items()
    
    # Calculate total_purchase_value (sum of purchase_price * quantity)
    total_purchase_value = 0
    for item in items:
        purchase_price = float(item.get('purchase_price', 0) or 0)
        quantity = int(item.get('quantity', 0) or 0)
        total_purchase_value += purchase_price * quantity

    return render_template('items.html', items=items, total_purchase_value=total_purchase_value)



# Helper function to find product by barcode (if needed)
def find_item_by_barcode(barcode):
    return fetch_one("SELECT * FROM products WHERE barcode = %s;", (barcode,))


@app.route('/sell', methods=['GET', 'POST'])
def sell_item():

    # Access control
    if 'username' not in session or session.get('role') not in ('admin', 'seller'):
        flash("❌ Zugriff verweigert. Bitte einloggen.", 'danger')
        return redirect(url_for('login'))

    items = load_items()

    if request.method == 'POST':

        indices = sorted({
            key.split('[')[1].split(']')[0]
            for key in request.form
            if key.startswith('items[')
        }, key=int)

        sale_items = []
        updated_quantities = {}
        total_sale_price = 0.0

        # -------------------------
        # VALIDATION PHASE
        # -------------------------
        for idx in indices:

            barcode = request.form.get(f'items[{idx}][barcode]', '').strip()
            quantity_raw = request.form.get(f'items[{idx}][quantity]', '').strip()
            discount_active = request.form.get(f'items[{idx}][discount_active]')
            price_input = request.form.get(f'items[{idx}][price]', '').strip()

            if not barcode:
                flash(f"❌ Produkt {int(idx)+1}: Kein Barcode ausgewählt.", 'danger')
                return redirect(url_for('sell_item'))

            item = next((i for i in items if i['barcode'] == barcode), None)
            if not item:
                flash(f"❌ Produkt mit Barcode {barcode} nicht gefunden.", 'danger')
                return redirect(url_for('sell_item'))

            try:
                quantity = int(quantity_raw)
                if quantity <= 0:
                    raise ValueError()
            except ValueError:
                flash(f"❌ Ungültige Menge für {item.get('product_name', 'Produkt')}.", 'danger')
                return redirect(url_for('sell_item'))

            if quantity > item['quantity']:
                flash(f"❌ Nicht genug Bestand für {item.get('product_name')}.", 'danger')
                return redirect(url_for('sell_item'))

            # Price logic
            if discount_active:
                try:
                    sale_price = float(price_input)
                    if sale_price <= 0:
                        raise ValueError()
                except ValueError:
                    flash(f"❌ Ungültiger Preis für {item.get('product_name')}.", 'danger')
                    return redirect(url_for('sell_item'))
            else:
                sale_price = float(item.get('selling_price') or 0)

            if sale_price <= 0:
                flash(f"❌ Ungültiger Preis für {item.get('product_name')}.", 'danger')
                return redirect(url_for('sell_item'))

            total_price = round(sale_price * quantity, 2)
            total_sale_price += total_price

            sale_items.append({
                'barcode': barcode,
                'product_name': item.get('product_name') or 'Unbenannt',
                'quantity': quantity,
                'sale_price': sale_price,
                'total_price': total_price,
                'purchase_price': item.get('purchase_price', 0)
            })

            updated_quantities[barcode] = item['quantity'] - quantity

        # -------------------------
        # DB TRANSACTION
        # -------------------------
        sale_id = str(uuid.uuid4())
        sale_date = datetime.now()
        sale_payment_method = (request.form.get('payment_method') or 'cash').strip().lower()
        if sale_payment_method not in PAYMENT_METHODS:
            sale_payment_method = 'cash'

        # A "card" sale must have actually been captured by the physical
        # Stripe Terminal reader before we record it as paid — the browser
        # sends back the PaymentIntent id it got from the reader, and we
        # re-check that PaymentIntent with Stripe directly (never trust the
        # client's word alone that a card payment "went through").
        stripe_payment_intent_id = (request.form.get('stripe_payment_intent_id') or '').strip() or None
        if sale_payment_method == 'card':
            try:
                verify_stripe_terminal_payment(stripe_payment_intent_id, total_sale_price)
            except Exception as e:
                flash(f"❌ Kartenzahlung konnte nicht bestätigt werden: {e}", 'danger')
                return redirect(url_for('sell_item'))

        try:
            conn = get_connection()
            cur = conn.cursor()

            # Insert sale
            cur.execute("""
                INSERT INTO sales (sale_id, username, sale_date, total_sale_price, payment_method, stripe_payment_intent_id)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (sale_id, session['username'], sale_date, total_sale_price, sale_payment_method, stripe_payment_intent_id))

            # Insert sale items
            for item in sale_items:
                # profit = revenue - cost for this line. This was previously
                # left out of the INSERT entirely, so the `profit` column
                # silently kept its DB default of 0.00 for every sale ever
                # made — which is why "Gewinn" (profit) always showed 0
                # everywhere (dashboards, reports, the chat assistant), even
                # though sale_price/purchase_price were recorded correctly.
                item_profit = round(
                    item['total_price'] - float(item['purchase_price'] or 0) * item['quantity'], 2
                )
                cur.execute("""
                    INSERT INTO sale_items 
                    (sale_id, barcode, product_name, quantity, sale_price, total_price, purchase_price, profit)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    sale_id,
                    item['barcode'],
                    item['product_name'],
                    item['quantity'],
                    item['sale_price'],
                    item['total_price'],
                    item['purchase_price'],
                    item_profit
                ))

            # Update stock
            for barcode, new_qty in updated_quantities.items():
                cur.execute("""
                    UPDATE products SET quantity = %s WHERE barcode = %s
                """, (new_qty, barcode))

            conn.commit()

        except Exception as e:
            conn.rollback()
            flash(f"❌ Fehler beim Speichern des Verkaufs: {e}", 'danger')
            return redirect(url_for('sell_item'))

        finally:
            conn.close()

        sale_details_lines = [f'Sold {len(sale_items)} item(s), total {format_money(total_sale_price)}']
        for si in sale_items:
            sale_details_lines.append(
                f"- {si['quantity']} × {si['product_name']} (Barcode {si['barcode']}): {format_money(si['total_price'])}"
            )
        log_audit('sale_created', 'sale', sale_id,
                   '\n'.join(sale_details_lines),
                   actor=session.get('username'), module='sales')

        # -------------------------
        # SUCCESS MESSAGE (FIXED)
        # -------------------------
        success_msg = "✅ Verkauf erfolgreich:\n"
        warnings = []

        for item in sale_items:
            success_msg += f"- {item['quantity']} × {item['product_name']}\n"

            if updated_quantities[item['barcode']] <= 5:
                warnings.append(
                    f"⚠️ Nur noch {updated_quantities[item['barcode']]} Stück von {item['product_name']}!"
                )

        flash(success_msg, 'success')

        for w in warnings:
            flash(w, 'warning')

        # Redirect
        return redirect(
            url_for('admin_dashboard') if session.get('role') == 'admin'
            else url_for('seller_dashboard')
        )

    # GET request
    return render_template('sell_item.html', items=items)



#from flask import request

@app.route('/admin/sales')
@login_required('admin')
def admin_sales():
    selected_date = request.args.get('date')
    # Use load_sales_with_refunds() so each sale card includes refund
    # data (total_refunded, refunded_qty per item) without extra queries
    # client-side.
    all_orders = load_sales_with_refunds()

    if selected_date:
        try:
            selected_date_obj = datetime.strptime(selected_date, "%Y-%m-%d").date()
            filtered_orders = []
            for order in all_orders:
                order_date_value = order.get('date')

                if isinstance(order_date_value, datetime):
                    order_datetime = order_date_value
                elif isinstance(order_date_value, str):
                    try:
                        order_datetime = datetime.fromisoformat(order_date_value)
                    except ValueError:
                        continue  # Skip invalid date string
                else:
                    continue  # Skip if not string or datetime

                if order_datetime.date() == selected_date_obj:
                    filtered_orders.append(order)

            all_orders = filtered_orders
        except ValueError:
            pass  # Invalid filter date, do nothing

    return render_template('admin_sales.html', sales=all_orders[::-1])

@app.route('/sale_contract/<sale_id>')
@login_required('admin')
def sale_contract(sale_id):
    all_orders = load_sales()

    order = None
    for item in all_orders:
        if item.get('order_id') == sale_id:
            order = item
            break

    if not order:
        return "Sale not found", 404

    return render_template(
        'sale_contract.html',
        order=order
    )

@app.route('/admin/sales/delete_sales_order/<order_id>', methods=['POST'])
@login_required('admin')
def delete_sales_order_route(order_id):
    # Delegates to db.delete_sales_order rather than duplicating the same
    # DELETE statements here (this route used to have its own copy that
    # could silently drift out of sync with the one in db.py).
    ok, message = delete_sales_order(order_id)
    if ok:
        log_audit('sale_deleted', 'sale', order_id, f'Deleted sale #{order_id}', actor=session.get('username'), module='sales')
        flash(f"✅ {message}", "success")
    else:
        flash(f"❌ {message}", "danger")
    return redirect(url_for('admin_sales'))


@app.route('/admin/sales/refund/<sale_id>', methods=['POST'])
@login_required('admin')
def process_refund(sale_id):
    """Process a refund for a sale (full or partial).

    Expects JSON body:
    {
        \"items\": [
            {\"sale_item_id\": 123, \"qty\": 1},
            {\"sale_item_id\": 124, \"qty\": 2}
        ],
        \"refund_method\": \"cash\" | \"card\",
        \"reason\": \"Customer returned damaged item\",
        \"stripe_payment_intent_id\": null   // required only for card refunds
    }

    Validates that:
    - Each item belongs to the given sale
    - The quantity being refunded does not exceed the quantity bought
      (minus any already-refunded quantity for that item)
    - For card refunds, processes the Stripe refund first
    - Then records the refund, restores stock, logs to Kasse if cash,
      and creates an audit log entry
    """
    if not request.is_json:
        return jsonify({'success': False, 'message': 'JSON body expected.'}), 400

    data = request.get_json(silent=True) or {}
    items_data = data.get('items', [])
    refund_method = (data.get('refund_method') or 'cash').strip().lower()
    reason = (data.get('reason') or '').strip()
    stripe_payment_intent_id = data.get('stripe_payment_intent_id') or None

    if not items_data:
        return jsonify({'success': False, 'message': 'Mindestens ein Artikel muss ausgewählt werden.'}), 400
    if refund_method not in ('cash', 'card'):
        return jsonify({'success': False, 'message': 'R\u00fcckerstattungsmethode muss \u201ecash\u201c oder \u201ecard\u201c sein.'}), 400

    # Verify the sale exists
    sale = fetch_one("SELECT * FROM sales WHERE sale_id = %s;", (sale_id,))
    if not sale:
        return jsonify({'success': False, 'message': 'Verkauf nicht gefunden.'}), 404

    # Calculate total refund amount and validate each item
    refund_items = []
    total_refund_amount = 0.0

    for entry in items_data:
        sale_item_id = entry.get('sale_item_id')
        qty = entry.get('qty', 0)

        if not sale_item_id or qty < 1:
            return jsonify({'success': False, 'message': 'Ungültige Artikelauswahl.'}), 400

        item = fetch_one(
            "SELECT * FROM sale_items WHERE id = %s AND sale_id = %s;",
            (sale_item_id, sale_id),
        )
        if not item:
            return jsonify({'success': False, 'message': f'Artikel-ID {sale_item_id} nicht in diesem Verkauf gefunden.'}), 400

        already_refunded = get_total_refunded_qty_for_item(sale_item_id)
        refundable_qty = int(item['quantity']) - already_refunded

        if qty > refundable_qty:
            pname = item.get('product_name', '')
            return jsonify({
                'success': False,
                'message': f'Maximal {refundable_qty} von \u201e{pname}\u201c k\u00f6nnen zur\u00fcckerstattet werden (bereits {already_refunded} erstattet).'
            }), 400

        line_total = round(float(item['sale_price']) * qty, 2)
        total_refund_amount = round(total_refund_amount + line_total, 2)

        refund_items.append({
            'sale_item_id': sale_item_id,
            'barcode': item['barcode'],
            'product_name': item['product_name'],
            'quantity': qty,
            'unit_price': float(item['sale_price']),
            'total_refund_amount': line_total,
        })

    if total_refund_amount <= 0:
        return jsonify({'success': False, 'message': 'Der Rückerstattungsbetrag muss größer als 0 sein.'}), 400

    # For card refunds: process Stripe refund first
    stripe_refund_id = None
    if refund_method == 'card':
        # Look up the original PaymentIntent from the sale
        original_pi_id = sale.get('stripe_payment_intent_id')
        if not original_pi_id:
            return jsonify({'success': False, 'message': 'Dieser Verkauf wurde nicht per Karte bezahlt — Bar-Rückerstattung verwenden.'}), 400
        try:
            refund_obj = stripe_refund_payment(original_pi_id, total_refund_amount)
            stripe_refund_id = refund_obj.id
        except Exception as e:
            logger.exception('Stripe refund failed for sale %s', sale_id)
            return jsonify({'success': False, 'message': f'Karten-Rückerstattung fehlgeschlagen: {e}'}), 500

    # Record each refund item in the DB
    username = session.get('username', 'unknown')
    for ri in refund_items:
        record_refund(
            sale_id=sale_id,
            sale_item_id=ri['sale_item_id'],
            barcode=ri['barcode'],
            product_name=ri['product_name'],
            quantity=ri['quantity'],
            unit_price=ri['unit_price'],
            total_refund_amount=ri['total_refund_amount'],
            refund_method=refund_method,
            reason=reason,
            stripe_refund_id=stripe_refund_id,
            refunded_by=username,
        )

    # Single audit log entry for the full refund batch
    items_summary = '; '.join(
        f"{ri['quantity']}×{ri['product_name']} (€{ri['total_refund_amount']:.2f})"
        for ri in refund_items
    )
    log_audit(
        'refund_processed', 'sale', sale_id,
        f'Rückerstattung von €{total_refund_amount:.2f} ({refund_method}) für Verkauf #{sale_id}: {items_summary}'
        + (f' — Grund: {reason}' if reason else ''),
        actor=username, module='sales',
    )

    return jsonify({
        'success': True,
        'message': f'✅ Rückerstattung von €{total_refund_amount:.2f} erfolgreich ({refund_method}).',
        'total_refunded': total_refund_amount,
        'refund_method': refund_method,
        'stripe_refund_id': stripe_refund_id,
        'items': refund_items,
    })


@app.route('/admin/sales/<sale_id>/refunds', methods=['GET'])
@login_required('admin')
def get_sale_refunds_route(sale_id):
    """Return the full refund history for a sale, as JSON."""
    refunds = get_sale_refunds(sale_id)
    total_refunded = get_total_refunded_for_sale(sale_id)
    return jsonify({
        'refunds': [{
            'id': r['id'],
            'sale_item_id': r['sale_item_id'],
            'product_name': r['product_name'],
            'barcode': r['barcode'],
            'quantity': int(r['quantity']),
            'unit_price': float(r['unit_price']),
            'total_refund_amount': float(r['total_refund_amount']),
            'refund_method': r['refund_method'],
            'reason': r.get('reason'),
            'refunded_by': r['refunded_by'],
            'refunded_at': r['refunded_at'].strftime('%d.%m.%Y %H:%M') if r.get('refunded_at') else None,
            'stripe_refund_id': r.get('stripe_refund_id'),
        } for r in refunds],
        'total_refunded': total_refunded,
    })


@app.route('/assistant/api/sales/refund', methods=['POST'])
@login_required('admin')
def assistant_process_refund():
    """Assistant tool endpoint for processing refunds.

    JSON body: same as POST /admin/sales/refund/<sale_id> but includes
    the sale_id in the body.

    Returns a user-facing message the assistant can show, not just raw JSON.
    """
    if not request.is_json:
        return jsonify({'success': False, 'message': 'JSON body expected.'}), 400

    data = request.get_json(silent=True) or {}
    sale_id = (data.get('sale_id') or '').strip()
    if not sale_id:
        return jsonify({'success': False, 'message': 'Verkaufs-ID (sale_id) ist erforderlich.'}), 400

    # Validate items structure
    items_data = data.get('items', [])
    refund_method = (data.get('refund_method') or 'cash').strip().lower()
    reason = (data.get('reason') or '').strip()
    stripe_payment_intent_id = data.get('stripe_payment_intent_id') or None

    if not items_data:
        return jsonify({'success': False, 'message': 'Mindestens ein Artikel muss für die Rückerstattung ausgewählt werden.'}), 400
    if refund_method not in ('cash', 'card'):
        return jsonify({'success': False, 'message': 'R\u00fcckerstattungsmethode muss \u201ecash\u201c oder \u201ecard\u201c sein.'}), 400

    sale = fetch_one("SELECT * FROM sales WHERE sale_id = %s;", (sale_id,))
    if not sale:
        return jsonify({'success': False, 'message': 'Verkauf nicht gefunden.'}), 404

    total_refund_amount = 0.0
    refund_items = []

    for entry in items_data:
        sale_item_id = entry.get('sale_item_id') or entry.get('id')
        qty = entry.get('qty') or entry.get('quantity', 1)
        if not sale_item_id or qty < 1:
            return jsonify({'success': False, 'message': 'Ungültige Artikelauswahl.'}), 400

        item = fetch_one(
            "SELECT * FROM sale_items WHERE id = %s AND sale_id = %s;",
            (sale_item_id, sale_id),
        )
        if not item:
            return jsonify({'success': False, 'message': f'Artikel-ID {sale_item_id} nicht in diesem Verkauf gefunden.'}), 400

        already_refunded = get_total_refunded_qty_for_item(sale_item_id)
        refundable_qty = int(item['quantity']) - already_refunded
        if qty > refundable_qty:
            pname = item.get('product_name', '')
            return jsonify({
                'success': False,
                'message': f'Nur {refundable_qty} von \u201e{pname}\u201c k\u00f6nnen erstattet werden ({already_refunded} bereits erstattet).'
            }), 400

        line_total = round(float(item['sale_price']) * qty, 2)
        total_refund_amount = round(total_refund_amount + line_total, 2)
        refund_items.append({
            'sale_item_id': sale_item_id,
            'barcode': item['barcode'],
            'product_name': item['product_name'],
            'quantity': qty,
            'unit_price': float(item['sale_price']),
            'total_refund_amount': line_total,
        })

    if total_refund_amount <= 0:
        return jsonify({'success': False, 'message': 'Der Rückerstattungsbetrag muss größer als 0 sein.'}), 400

    stripe_refund_id = None
    if refund_method == 'card':
        original_pi_id = sale.get('stripe_payment_intent_id')
        if not original_pi_id:
            return jsonify({'success': False, 'message': 'Bar-Rückerstattung verwenden — dieser Verkauf wurde nicht per Karte bezahlt.'}), 400
        try:
            refund_obj = stripe_refund_payment(original_pi_id, total_refund_amount)
            stripe_refund_id = refund_obj.id
        except Exception as e:
            logger.exception('Stripe refund failed (assistant) for sale %s', sale_id)
            return jsonify({'success': False, 'message': f'Karten-Rückerstattung fehlgeschlagen: {e}'}), 500

    username = session.get('username', 'unknown')
    for ri in refund_items:
        record_refund(
            sale_id=sale_id, sale_item_id=ri['sale_item_id'],
            barcode=ri['barcode'], product_name=ri['product_name'],
            quantity=ri['quantity'], unit_price=ri['unit_price'],
            total_refund_amount=ri['total_refund_amount'],
            refund_method=refund_method, reason=reason,
            stripe_refund_id=stripe_refund_id, refunded_by=username,
        )

    items_summary = '; '.join(f"{ri['quantity']}×{ri['product_name']}" for ri in refund_items)
    log_audit('refund_processed', 'sale', sale_id,
              f'Assistant: Rückerstattung €{total_refund_amount:.2f} ({refund_method}) für #{sale_id}: {items_summary}'
              + (f' — {reason}' if reason else ''),
              actor=username, source='assistant', module='sales')

    return jsonify({
        'success': True,
        'message': f'✅ Rückerstattung von €{total_refund_amount:.2f} ({refund_method}) für Verkauf #{sale_id} erfolgreich. {items_summary}',
        'total_refunded': total_refund_amount,
    })


@app.route('/admin/sales/edit/<int:sale_item_id>', methods=['GET', 'POST'])
@login_required('admin')
def edit_sale(sale_item_id):
    sale = get_sale_item(sale_item_id)
    if not sale:
        flash('❌ Verkaufsposition nicht gefunden.', 'danger')
        return redirect(url_for('admin_sales'))

    if request.method == 'POST':
        quantity_raw = request.form.get('quantity', '').strip()
        sale_price_raw = request.form.get('sale_price', '').strip()
        try:
            quantity = int(quantity_raw)
            sale_price = float(sale_price_raw)
        except ValueError:
            flash('❌ Bitte gültige Zahlen für Menge und Preis eingeben.', 'danger')
            return render_template('edit_sale.html', sale=sale)

        ok, message = update_sale_item(sale_item_id, quantity, sale_price)
        if ok:
            log_audit('sale_updated', 'sale_item', sale_item_id,
                       f"Updated sale line #{sale_item_id}: quantity {quantity}, price {format_money(sale_price)}",
                       actor=session.get('username'), module='sales')
            flash(f'✅ {message}', 'success')
            return redirect(url_for('admin_sales'))
        else:
            flash(f'❌ {message}', 'danger')
            return render_template('edit_sale.html', sale=sale)

    return render_template('edit_sale.html', sale=sale)


@app.route('/admin/pay-salary', methods=['GET', 'POST'])
@login_required('admin')
def pay_salary():
    # "the option to choose" used to list every user in the system —
    # including the admin account itself, and inactive/deactivated sellers
    # who shouldn't be paid a salary. Scope it down to real, active
    # employees so the dropdown only ever shows people you can actually pay.
    users = [u for u in load_users() if u.get('role') == 'seller' and u.get('activated')]
    employee_salaries = {u['username']: float(u.get('salary') or 0) for u in users}
    valid_usernames = set(employee_salaries.keys())

    if request.method == 'POST':
        try:
            employee = request.form['employee_name']
            if employee not in valid_usernames:
                flash('❌ Bitte einen gültigen, aktiven Mitarbeiter auswählen.', 'danger')
                return redirect(url_for('pay_salary'))

            amount_raw = request.form.get('salary_amount', '').strip()
            try:
                amount = float(amount_raw)
            except ValueError:
                amount = -1
            if amount <= 0:
                flash('❌ Bitte einen gültigen Betrag über 0 € eingeben.', 'danger')
                return redirect(url_for('pay_salary'))

            source = request.form.get('payment_source', 'Kasse')
            note = request.form.get('note', '')

            record = {
                'employee': employee,
                'amount': amount,
                'source': source,
                'note': note,
                'payment_date': datetime.now()  # direct datetime object
            }

            insert_salary_payment(record)
            log_audit('salary_paid', 'salary_payment', None, format_salary_details(employee, amount),
                       actor=session.get('username'), module='salaries')

            flash(f'✅ {amount:.2f} € an {employee} aus {source} bezahlt.', 'success')
            return redirect(url_for('pay_salary'))

        except Exception as e:
            flash(f'Fehler bei der Zahlung: {str(e)}', 'danger')

    return render_template('pay_salary.html', users=users, employee_salaries=employee_salaries)




@app.route('/order', methods=['GET', 'POST'])
def order():
    # Check user role
    if session.get('role') not in ['admin', 'seller']:
        flash('Zugriff verweigert.', 'danger')
        return redirect(url_for('index'))

    if request.method == 'POST':
        try:
            product_name = request.form['product_name'].strip()
            ref_number = request.form.get('ref_number', '').strip()
            description = request.form.get('description', '').strip()
            price = float(request.form['price'])
            selling_price = float(request.form['selling_price'])
            min_selling_price = float(request.form['min_selling_price'])
            quantity = int(request.form['quantity'])

            if price < 0 or selling_price < 0 or min_selling_price < 0 or quantity < 1:
                raise ValueError("Preise und Menge müssen positiv sein.")
            
            # Barcode validation/generation
            if ref_number:
                if not ref_number.isdigit() or len(ref_number) > 16:
                    flash("❌ Der manuell eingegebene Barcode darf höchstens 16 Ziffern lang sein.", "danger")
                    return redirect(url_for('order'))
                barcode_number = ref_number
            else:
                # Generate unique 16-digit barcode (simple example)
                barcode_number = ''.join(str(random.randint(0, 9)) for _ in range(16))


            # Save barcode image
            barcode_dir = os.path.join(app.static_folder, 'barcodes')
            os.makedirs(barcode_dir, exist_ok=True)
            barcode_filename_no_ext = f'code_barres_{barcode_number}'
            barcode_path = os.path.join(barcode_dir, barcode_filename_no_ext)

            ean = barcode.get_barcode_class('ean13')
            code = ean(barcode_number, writer=ImageWriter())
            code.save(barcode_path)

            total_price = round(price * quantity, 2)
            today = datetime.now().strftime('%Y-%m-%d')
            username = session.get('username', 'unbekannt')
            payment_method = (request.form.get('payment_method') or 'cash').strip().lower()
            if payment_method not in PAYMENT_METHODS:
                payment_method = 'cash'

            # A "card" purchase order must have actually been captured by the
            # physical Stripe Terminal reader first — same rule as sales and
            # debt payments — never just trust the client's word that a card
            # payment "went through".
            stripe_payment_intent_id = (request.form.get('stripe_payment_intent_id') or '').strip() or None
            if payment_method == 'card':
                try:
                    verify_stripe_terminal_payment(stripe_payment_intent_id, total_price)
                except Exception as e:
                    flash(f"❌ Kartenzahlung konnte nicht bestätigt werden: {e}", 'danger')
                    return redirect(url_for('order'))

            new_order = {
                "order_number": barcode_number,
                "product_name": product_name,
                "ref_number": ref_number if ref_number else None,
                "description": description,
                "price": price,
                "selling_price": selling_price,
                "min_selling_price": min_selling_price,
                "quantity": quantity,
                "total_price": total_price,
                "date": today,
                "user": username,
                "barcode": f"barcodes/{barcode_filename_no_ext}.png",
                "payment_method": payment_method,
                "stripe_payment_intent_id": stripe_payment_intent_id,
            }

            add_order(new_order)  # Your DB insert function
            log_audit('purchase_created', 'order', barcode_number,
                       format_purchase_details(product_name, quantity, price),
                       actor=username, module='purchases')

            flash('✅ Bestellung erfolgreich gespeichert!', 'success')
            return redirect(url_for('list_orders'))

        except (ValueError, KeyError) as e:
            flash(f'Ungültige Eingabe: {e}', 'danger')
            return redirect(url_for('order'))

        except Exception as e:
            flash(f'Fehler beim Speichern der Bestellung: {e}', 'danger')
            return redirect(url_for('order'))

    # GET request - render form
    return render_template('order_item.html')








# Update_Item_quantity
@app.route('/update_quantity', methods=['POST'])
@login_required('admin')
def update_quantity():
    product_identifier = request.form.get('product_identifier', '').strip()
    add_quantity_str = request.form.get('add_quantity', '0').strip()

    # Validate quantity
    try:
        add_quantity = int(add_quantity_str)
        if add_quantity < 1:
            flash("Menge muss mindestens 1 sein.", "danger")
            return redirect(url_for('list_items'))
    except ValueError:
        flash("Ungültige Menge angegeben.", "danger")
        return redirect(url_for('list_items'))

    if not product_identifier:
        flash("Bitte Produktname oder Barcode eingeben.", "warning")
        return redirect(url_for('list_items'))

    # First try to find by barcode (exact match) — what a barcode scanner produces
    item = fetch_one("SELECT * FROM products WHERE barcode = %s;", (product_identifier,))

    # Then try by SKU (exact match)
    if not item:
        item = fetch_one("SELECT * FROM products WHERE sku = %s;", (product_identifier,))

    # If not found, try by product name (case insensitive)
    if not item:
        item = fetch_one("SELECT * FROM products WHERE LOWER(product_name) = LOWER(%s);", (product_identifier,))

    if not item:
        flash("Produkt nicht gefunden. Bitte Produktname oder Barcode prüfen.", "warning")
        return redirect(url_for('list_items'))

    new_quantity = item['quantity'] + add_quantity
    update_query = "UPDATE products SET quantity = %s WHERE barcode = %s;"
    execute_query(update_query, (new_quantity, item['barcode']))

    flash(f"Menge von '{item['product_name']}' von {item['quantity']} auf {new_quantity} erhöht.", "success")
    return redirect(url_for('list_items'))








# Load normalize_items
def normalize_items(items):
    for item in items:
        item['name'] = item.get('name') or item.get('product_name') or 'Unbenannt'
        item['product_name'] = item.get('product_name') or item.get('name') or 'Unbenannt'
        item['barcode'] = item.get('barcode', '')
        item['quantity'] = int(item.get('quantity', 0))
        item['purchase_price'] = float(item.get('purchase_price', 0))
        item['selling_price'] = float(item.get('selling_price', 0))
        item['min_selling_price'] = float(item.get('min_selling_price', 0))
        item['price'] = float(item.get('price', item.get('selling_price', 0)))
        item['description'] = item.get('description', '')
        item['photo_link'] = item.get('photo_link') or item.get('image_url', '')
        item['item_condition'] = (item.get('item_condition') or 'neu').strip().lower()
    return items


@app.route('/orders')
@login_required(['admin', 'seller'])
def list_orders():
    role = session.get('role')
    username = session.get('username')
    
    # Get filters from query params
    filter_user = request.args.get('user', '').strip()
    filter_date = request.args.get('date', '').strip()
    # Search text: the template uses `name="q"` for this input.
    q = request.args.get('q', '').strip()

    # Fetch orders with filter and access control
    # (db.get_orders currently supports user/date, not text search).
    orders = get_orders(role=role, username=username, filter_user=filter_user, filter_date=filter_date)

    # If q is provided, filter in Python against product_name/ref_number.
    # Keeps the UI working even before/without adding a SQL search to get_orders.
    if q:
        q_lower = q.lower()
        orders = [o for o in orders if (q_lower in (o.get('product_name') or '').lower() or q_lower in (o.get('ref_number') or '').lower() or q_lower in (o.get('order_number') or '').lower())]

    # Fetch distinct users for filtering dropdown
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT `user` FROM orders WHERE `user` IS NOT NULL ORDER BY `user`;")
            users = [row['user'] for row in cur.fetchall()]
    finally:
        conn.close()

    return render_template(
        "list_orders.html",
        orders=orders,
        users=users,
        filter_user=filter_user,
        filter_date=filter_date,
        search_query=q,
    )


@app.route('/orders/<order_number>/edit', methods=['GET', 'POST'])
@login_required(['admin'])
def edit_order(order_number):
    conn = get_connection()
    cur = conn.cursor()

    if request.method == 'POST':
        try:
            selling_price = float(request.form['selling_price'])
            quantity = int(request.form['quantity'])
            total_price = selling_price * quantity

            order_data = {
                'product_name': request.form['product_name'],
                'ref_number': request.form.get('ref_number'),
                'description': request.form.get('description'),
                'price': float(request.form.get('price')),
                'selling_price': selling_price,
                'min_selling_price': float(request.form['min_selling_price']),
                'quantity': quantity,
                'total_price': total_price,
                'date': datetime.now().strftime('%Y-%m-%d'),  # system date in 'YYYY-MM-DD' format
                'user': session.get('username'),  # get logged-in username from session
                'barcode': request.form.get('barcode')
            }

            update_order(order_number, order_data)
            log_audit('purchase_updated', 'order', order_number,
                       format_purchase_details(order_data['product_name'], order_data['quantity'], order_data['price']),
                       actor=session.get('username'), module='purchases')
            flash('Bestellung erfolgreich aktualisiert.', 'success')
            return redirect(url_for('list_orders'))

        except KeyError as e:
            flash(f'Missing form field: {e.args[0]}', 'danger')
        except ValueError:
            flash('Please enter valid numeric values.', 'danger')

    # GET request: load existing order for editing
    cur.execute("SELECT * FROM orders WHERE order_number = %s;", (order_number,))
    order = cur.fetchone()
    cur.close()
    conn.close()

    if not order:
        flash('Bestellung nicht gefunden.', 'danger')
        return redirect(url_for('list_orders'))

    return render_template('edit_order.html', order=order)


@app.route('/orders/<order_number>/delete', methods=['POST'])
@login_required(['admin', 'seller'])
def delete_order_route(order_number):
    order = query_one("SELECT product_name, quantity, price FROM orders WHERE order_number = %s;", (order_number,))
    delete_order(order_number)
    if order:
        details = format_purchase_details(order.get('product_name'), order.get('quantity'), order.get('price'))
    else:
        details = f'Purchase order #{order_number}'
    log_audit('purchase_deleted', 'order', order_number, details, actor=session.get('username'), module='purchases')
    flash('Bestellung wurde gelöscht.', 'success')
    return redirect(url_for('list_orders'))



@app.route('/list_salary_payments')
@login_required('admin')  # optional but secure
def list_salary_payments():
    payments = load_salary_payments()
    return render_template('list_salary_payments.html', payments=payments)


@app.route('/admin/salary/edit/<int:payment_id>', methods=['GET', 'POST'])
@login_required('admin')
def edit_salary_payment(payment_id):
    payment = get_salary_payment(payment_id)
    if not payment:
        flash('❌ Zahlung nicht gefunden.', 'danger')
        return redirect(url_for('list_salary_payments'))

    if request.method == 'POST':
        amount_raw = request.form.get('amount', '').strip()
        source = request.form.get('source', '').strip() or 'Kasse'
        note = request.form.get('note', '').strip()
        try:
            amount = float(amount_raw)
        except ValueError:
            amount = -1
        if amount <= 0:
            flash('❌ Bitte einen gültigen Betrag über 0 € eingeben.', 'danger')
            return render_template('edit_salary_payment.html', payment=payment)

        update_salary_payment(payment_id, amount, source, note)
        log_audit('salary_payment_updated', 'salary_payment', payment_id,
                   f"Updated salary payment #{payment_id}: {format_money(amount)} ({source})" + (f' — {note}' if note else ''),
                   actor=session.get('username'), module='salaries')
        flash('✅ Zahlung aktualisiert.', 'success')
        return redirect(url_for('list_salary_payments'))

    return render_template('edit_salary_payment.html', payment=payment)


@app.route('/admin/salary/delete/<int:payment_id>', methods=['POST'])
@login_required('admin')
def delete_salary_payment_route(payment_id):
    delete_salary_payment(payment_id)
    log_audit('salary_payment_deleted', 'salary_payment', payment_id,
               f'Deleted salary payment #{payment_id}', actor=session.get('username'), module='salaries')
    flash('✅ Zahlung gelöscht.', 'success')
    return redirect(url_for('list_salary_payments'))


@app.route('/kasse', methods=['GET', 'POST'])
@login_required()
def kasse():
    role = session.get('role', 'admin')

    if request.method == 'POST':
        if not seller_kasse_enabled():
            flash('Kasse-Zugriff wurde für Ihr Konto deaktiviert.', 'danger')
            return redirect(url_for('kasse'))

        typ = request.form.get('typ')
        betrag = request.form.get('betrag')
        beschreibung = request.form.get('beschreibung')
        payment_method = (request.form.get('payment_method') or 'cash').strip().lower()
        if payment_method not in PAYMENT_METHODS:
            payment_method = 'cash'
        username = session.get('username', 'anonymous')

        if typ not in ('einzahlung', 'auszahlung'):
            flash('Ungültiger Typ')
            return redirect(url_for('kasse'))

        try:
            # Accept both comma and dot as decimal separator (e.g. 10,50 or 10.50)
            normalized = betrag.replace(',', '.').strip()
            amount = float(normalized)
            if amount <= 0:
                flash('Betrag muss größer als 0 sein')
                return redirect(url_for('kasse'))
        except Exception:
            flash('Ungültiger Betrag')
            return redirect(url_for('kasse'))

        if typ == 'auszahlung':
            amount = -amount

        # 🔥 IMPORTANT FIX: escape % to prevent PyMySQL crash
        beschreibung = beschreibung.replace('%', '%%')
        username = username.replace('%', '%%')

        query = """
            INSERT INTO cash_transactions (date, amount, type, description, username, payment_method)
            VALUES (%s, %s, %s, %s, %s, %s)
        """

        params = (datetime.now(), abs(amount), typ, beschreibung, username, payment_method)

        execute_query(query, params)
        action_name = 'deposit_created' if typ == 'einzahlung' else 'withdrawal_created'
        log_audit(action_name, 'cash_transaction', None,
                   format_kasse_details(typ, abs(amount), beschreibung),
                   actor=session.get('username'), module='cash')

        flash('Transaktion erfolgreich gespeichert', 'success')
        return redirect(url_for('kasse'))

    # CSV download
    if request.args.get('download') == 'csv':
        transactions = fetch_all("SELECT * FROM cash_transactions ORDER BY date DESC;")

        si = StringIO()
        cw = csv.writer(si)
        cw.writerow(['Datum', 'Typ', 'Betrag', 'Beschreibung', 'Benutzer'])

        for t in transactions:
            cw.writerow([
                t['date'].strftime('%Y-%m-%d %H:%M'),
                t['type'],
                f"{t['amount']:.2f}",
                t['description'],
                t['username']
            ])

        output = si.getvalue()
        return Response(
            output,
            mimetype="text/csv",
            headers={"Content-disposition": "attachment; filename=transactions.csv"}
        )

    # Fetch data
    transactions = fetch_all("SELECT * FROM cash_transactions ORDER BY date DESC;")

# Kasse is the physical cash drawer, so only cash-tagged manual entries
    # count toward its balance — a manual transaction logged as "card"
    # (e.g. correcting a card-terminal reconciliation) shouldn't move the
    # physical cash total. Rows predating the payment_method column are
    # treated as cash (that was the only option before it existed).
    current_balance = float(sum(
        (t['amount'] if t['type'] == 'einzahlung' else -t['amount'])
        for t in transactions
        if (t.get('payment_method') or 'cash') == 'cash'
    ))

    today = date.today()

    # Cash-only sales and purchases (card sales/purchases don't touch Kasse).
    total_sold_today = calculate_sales_for_date(today)
    total_orders_today = calculate_purchases_for_date(today)

    # The two flows that used to be completely invisible to Kasse: cash
    # collected against open debts (money physically handed over) and
    # salaries actually paid out of the drawer (source = "Kasse", as
    # opposed to "Privat" which never touches it).
    cash_debt_payments_today = calculate_cash_debt_payments_for_date(today)
    cash_salary_payments_today = calculate_cash_salary_payments_for_date(today)

    total_balance = (
        current_balance
        + total_sold_today
        - total_orders_today
        + cash_debt_payments_today
        - cash_salary_payments_today
    )

    # 🔥 FIXED MONTHLY QUERY (%% escape)
    monthly_summary = fetch_all("""
        SELECT
            DATE_FORMAT(date, '%%Y-%%m-01') AS month,
            SUM(CASE WHEN type = 'einzahlung' THEN amount ELSE -amount END) AS total
        FROM cash_transactions
        WHERE COALESCE(payment_method, 'cash') = 'cash'
        GROUP BY month
        ORDER BY month DESC;
    """)

    return render_template(
        'kasse.html',
        transactions=transactions,
        current_balance=current_balance,
        total_sold_today=total_sold_today,
        total_orders_today=total_orders_today,
        cash_debt_payments_today=cash_debt_payments_today,
        cash_salary_payments_today=cash_salary_payments_today,
        total_balance=total_balance,
        monthly_summary=monthly_summary,
        role=role
    )

# Delete_cash_transaction
@app.route('/admin/kasse/delete/<int:transaction_id>', methods=['POST'])
@login_required('admin')
def delete_cash_transaction(transaction_id):
    try:
        transaction = get_cash_transaction(transaction_id)
        execute_query("DELETE FROM cash_transactions WHERE id = %s", (transaction_id,))
        if transaction:
            action_name = 'deposit_deleted' if transaction.get('type') == 'einzahlung' else 'withdrawal_deleted'
            details = format_kasse_details(transaction.get('type'), transaction.get('amount'), transaction.get('description'))
        else:
            action_name = 'withdrawal_deleted'
            details = f'Deleted cash transaction #{transaction_id}'
        log_audit(action_name, 'cash_transaction', transaction_id, details, actor=session.get('username'), module='cash')
        flash('Transaktion gelöscht.', 'success')
    except Exception as e:
        flash('Fehler beim Löschen: ' + str(e), 'danger')
    return redirect(url_for('kasse'))


@app.route('/admin/kasse/edit/<int:transaction_id>', methods=['GET', 'POST'])
@login_required('admin')
def edit_kasse_transaction(transaction_id):
    transaction = get_cash_transaction(transaction_id)
    if not transaction:
        flash('❌ Transaktion nicht gefunden.', 'danger')
        return redirect(url_for('kasse'))

    if request.method == 'POST':
        typ = request.form.get('typ')
        amount_raw = request.form.get('betrag', '').strip()
        description = (request.form.get('beschreibung', '') or '').replace('%', '%%')
        payment_method = (request.form.get('payment_method') or 'cash').strip().lower()
        if payment_method not in PAYMENT_METHODS:
            payment_method = 'cash'

        if typ not in ('einzahlung', 'auszahlung'):
            flash('❌ Ungültiger Typ.', 'danger')
            return render_template('edit_kasse_transaction.html', transaction=transaction)
        try:
            amount = float(amount_raw)
            if amount <= 0:
                raise ValueError
        except ValueError:
            flash('❌ Betrag muss größer als 0 sein.', 'danger')
            return render_template('edit_kasse_transaction.html', transaction=transaction)

        update_cash_transaction(transaction_id, amount, typ, description, payment_method)
        action_name = 'deposit_updated' if typ == 'einzahlung' else 'withdrawal_updated'
        log_audit(action_name, 'cash_transaction', transaction_id,
                   format_kasse_details(typ, amount, description),
                   actor=session.get('username'), module='cash')
        flash('✅ Transaktion aktualisiert.', 'success')
        return redirect(url_for('kasse'))

    return render_template('edit_kasse_transaction.html', transaction=transaction)




@app.route("/generate_barcode/<order_number>")
def generate_barcode(order_number):
    try:
        # Create directory if not exists
        barcode_dir = os.path.join("static", "barcodes")
        os.makedirs(barcode_dir, exist_ok=True)

        # Define the filename
        filename = f"code_barres_{order_number}.png"
        filepath = os.path.join(barcode_dir, filename)

        # Generate barcode if it doesn't already exist
        if not os.path.exists(filepath):
            EAN = barcode.get_barcode_class('code128')
            ean = EAN(order_number, writer=ImageWriter())
            ean.save(filepath[:-4])  # Remove ".png" as `.save` adds it automatically

        return jsonify({"status": "ok", "filename": f"/static/barcodes/{filename}"})
    
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})




# Helper to parse date safely
def parse_date(date_val):
    if isinstance(date_val, datetime):
        return date_val
    if isinstance(date_val, str):
        try:
            return datetime.fromisoformat(date_val)
        except ValueError:
            return None
    return None

def to_decimal(value, default=Decimal('0')):
    """Convert value to Decimal safely."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default

@app.template_filter('format_date_str')
def format_date_str(value):
    # Assume value is a string like "2023-07-24T15:32:10.123Z" or similar
    if not isinstance(value, str):
        return value
    return value.replace("T", " ")[:19]

# Seller Dashboard Route



@app.route('/seller')
@login_required('seller')
def seller_dashboard():
    username = session['username']

    # Existing sales and purchases fetching logic (if you still need those)
    user_sales = get_sales_for_user(username)
    user_purchases = get_purchases_for_user(username)

    # Calculate today's sales and purchases using your helper functions
    daily_sales_total = calculate_today_sales(username)
    daily_purchases_total = calculate_today_purchases(username)

    # Calculate daily net difference
    daily_net_difference = daily_sales_total - daily_purchases_total

    # You can keep your existing profit, monthly totals, etc. calculations here or adjust as needed
    # For example, let's just keep the previous monthly calculations based on user_sales
    flat_sales = []
    for sale in user_sales:
        sale_date = sale.get('sale_date')
        if isinstance(sale_date, str):
            sale_date = datetime.fromisoformat(sale_date)
        elif not sale_date:
            sale_date = datetime.now()

        quantity = Decimal(str(sale.get('quantity', 0)))
        sale_price = Decimal(str(sale.get('sale_price', '0')))
        purchase_price = Decimal(str(sale.get('purchase_price', '0')))
        total_price = sale_price * quantity
        profit = (sale_price - purchase_price) * quantity

        flat_sales.append({
            'date': sale_date,
            'product_name': sale.get('product_name', 'Unbekannt'),
            'quantity': quantity,
            'sale_price': sale_price,
            'purchase_price': purchase_price,
            'total_price': total_price,
            'profit': profit,
        })

    today = datetime.now().date()
    daily_profit = sum(sale['profit'] for sale in flat_sales if sale['date'].date() == today)
    monthly_profit = sum(sale['profit'] for sale in flat_sales if sale['date'].year == today.year and sale['date'].month == today.month)
    total_profit = sum(sale['profit'] for sale in flat_sales)
    monthly_total_order_price = sum(sale['total_price'] for sale in flat_sales if sale['date'].year == today.year and sale['date'].month == today.month)
    total_purchase_cost = sum(
        Decimal(str(p.get('purchase_price', '0'))) * Decimal(str(p.get('quantity', 0)))
        for p in user_purchases
    )
    total_balance = daily_sales_total - daily_purchases_total

    return render_template(
        'seller_dashboard.html',
        sales=flat_sales,
        purchases=user_purchases,
        daily_profit=float(daily_profit),
        monthly_profit=float(monthly_profit),
        total_profit=float(total_profit),
        total_purchase_cost=float(total_purchase_cost),
        total_balance=float(total_balance),
        monthly_total_order_price=float(monthly_total_order_price),
        daily_sales_total=float(daily_sales_total),
        daily_purchases_total=float(daily_purchases_total),
        daily_net_difference=float(daily_net_difference),
    )



# Seller: Seller History
@app.route('/seller/sales')
@login_required('seller')
def seller_sales():
    username = session.get('username', '').lower()
    
    # Load all sales (replace with your actual function)
    sales = load_sales()  # returns a list of sale dicts
    
    # Filter sales by matching user/seller - using .get() to avoid KeyError
    user_sales = [s for s in sales if s.get('user', '').lower() == username]
    
    # Optional: sort by date descending (if your sales have a 'date' field)
    user_sales.sort(key=lambda s: s.get('date', ''), reverse=True)
    
    return render_template('seller_sales.html', sales=user_sales)


# Load Items for User/Seller
def load_items_for_seller(username):
    all_items = load_items()
    filtered_items = []
    for item in all_items:
        seller = item.get('seller', 'admin')  # Default to admin if missing
        if seller in ('admin', username):
            filtered_items.append(item)
    return filtered_items

# List all the items for the seller
@app.route('/seller/items')
@login_required('seller')
def seller_items():
    username = session['username']
    items = load_items_for_seller(username)
    items = normalize_items(items)  # Ensure all items have 'name'
    items = items[::-1]
    return render_template('seller_items.html', items=items)


# Schulden
# List Schulden
@app.route('/schulden')
@login_required(['admin', 'seller'])
def schulden():
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT client_name, 
                COALESCE(SUM(CASE WHEN paid = FALSE THEN amount ELSE 0 END), 0) AS total_unpaid,
                MIN(created_at) AS first_created
            FROM debts
            GROUP BY client_name
            ORDER BY client_name
        """)
        clients = cur.fetchall()

        # Also calculate total unpaid (optional)
        cur.execute("SELECT COALESCE(SUM(amount), 0) AS total_unpaid FROM debts WHERE paid = FALSE")
        result = cur.fetchone()
        unpaid_total = float(result['total_unpaid']) if result else 0.0

    except Exception as e:
        clients = []
        unpaid_total = 0.0
        logger.exception("Error fetching clients")
    finally:
        cur.close()
        conn.close()

    return render_template('schulden.html', clients=clients, unpaid_total=unpaid_total)

@app.route('/schulden/add', methods=['POST'])
@login_required(['admin', 'seller'])
def add_debt_route():
    name = request.form.get('client_name')  # must match your form field name
    description = request.form.get('description')
    amount = request.form.get('amount')
    phone = request.form.get('phone')

    # Basic validation
    if not name or not description or not phone or not amount:
        flash("Alle Felder sind erforderlich.", "danger")
        return redirect(url_for('schulden'))

    try:
        amount = float(amount)
    except ValueError:
        flash("Der Betrag muss eine Zahl sein.", "danger")
        return redirect(url_for('schulden'))

    debt_id = str(uuid.uuid4())[:8].upper()
    reference_number = generate_debt_reference_number()

    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO debts (debt_id, reference_number, client_name, description, amount, original_amount, phone_number)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (debt_id, reference_number, name, description, amount, amount, phone))
        conn.commit()
    except Exception as e:
        flash(f"Datenbankfehler: {e}", "danger")
        return redirect(url_for('schulden'))
    finally:
        cur.close()
        conn.close()

    is_new_customer = not query_one("SELECT 1 FROM debts WHERE client_name = %s AND debt_id != %s;", (name, debt_id))

    log_audit('debt_created', 'debt', debt_id,
               format_debt_details(name, amount, description, phone),
               actor=session.get('username'), module='debts')
    if is_new_customer:
        log_audit('customer_created', 'client', name, f'New customer: {name}',
                   actor=session.get('username'), module='customers')
    flash("Schuld erfolgreich hinzugefügt!", "success")
    return redirect(url_for('schulden'))




@app.route('/clients/delete', methods=['POST'])
@login_required(['admin', 'seller'])
def delete_client():
    client_name = request.form.get('client_name')

    try:
        conn = get_connection()
        cur = conn.cursor()

        # Ensure no unpaid debts
        cur.execute("SELECT COUNT(*) AS cnt FROM debts WHERE client_name = %s AND paid = FALSE", (client_name,))
        count = cur.fetchone()['cnt']
        if count > 0:
            flash("Kunde hat noch offene Schulden und kann nicht gelöscht werden.", "danger")
        else:
            cur.execute("DELETE FROM debts WHERE client_name = %s", (client_name,))
            conn.commit()
            log_audit('customer_deleted', 'client', client_name, f'Deleted customer: {client_name}', actor=session.get('username'), module='customers')
            flash("Kunde erfolgreich gelöscht.", "success")
    except Exception as e:
        flash(f"Fehler beim Löschen: {e}", "danger")
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('schulden'))


@app.route('/clients/rename', methods=['POST'])
@login_required(['admin', 'seller'])
def rename_client():
    """Clients have no separate table — a \"client\" is just the distinct
    client_name column on debts — so \"updating\" a client means renaming
    that name across all of their debt rows in one transaction."""
    old_name = (request.form.get('old_client_name') or '').strip()
    new_name = (request.form.get('new_client_name') or '').strip()

    if not old_name or not new_name:
        flash('Alter und neuer Kundenname sind erforderlich.', 'danger')
        return redirect(url_for('schulden'))
    if old_name == new_name:
        flash('Der neue Name ist identisch mit dem alten.', 'warning')
        return redirect(url_for('schulden'))

    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM debts WHERE client_name = %s", (old_name,))
        if not cur.fetchone()['cnt']:
            flash('Kunde wurde nicht gefunden.', 'danger')
        else:
            cur.execute("UPDATE debts SET client_name = %s WHERE client_name = %s", (new_name, old_name))
            conn.commit()
            log_audit('customer_renamed', 'client', new_name,
                       f'Renamed customer "{old_name}" to "{new_name}"',
                       actor=session.get('username'), module='customers')
            flash(f'Kunde "{old_name}" wurde in "{new_name}" umbenannt.', 'success')
    except Exception as e:
        flash(f'Fehler beim Umbenennen: {e}', 'danger')
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('schulden'))


@app.route('/schulden/delete/<debt_id>', methods=['POST'])
@login_required('admin')
def delete_debt_route(debt_id):
    """Delete a single debt record. clients.html already posts here (its
    trash-can button is only enabled once a debt is marked paid), but the
    matching route never existed, so every click 404'd."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT client_name, amount FROM debts WHERE debt_id = %s", (debt_id,))
        row = cur.fetchone()
        cur.execute("DELETE FROM debts WHERE debt_id = %s", (debt_id,))
        deleted = cur.rowcount
        conn.commit()
        if deleted:
            details = format_debt_details(row['client_name'], row['amount']) if row else f'Debt #{debt_id}'
            log_audit('debt_deleted', 'debt', debt_id, details, actor=session.get('username'), module='debts')
            flash("Schuld erfolgreich gelöscht.", "success")
        else:
            flash("Diese Schuld wurde nicht gefunden.", "warning")
    except Exception as e:
        conn.rollback()
        flash(f"Fehler beim Löschen: {e}", "danger")
        logger.exception("Error deleting debt")
    finally:
        cur.close()
        conn.close()

    return redirect(request.referrer or url_for('schulden'))


@app.route('/schulden/delete_all', methods=['POST'])
@login_required('admin')
def delete_all_debts_route():
    """Wipe every debt record for every client — paid and unpaid alike.
    This is intentionally a hard reset (not just 'delete paid debts'),
    so it's admin-only and always asks for confirmation client-side."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM debts")
        deleted = cur.rowcount
        conn.commit()
        log_audit('debt_deleted_all', 'debt', None, f'Deleted all {deleted} debt record(s)', actor=session.get('username'), module='debts')
        flash(f"{deleted} Schuld(en) wurden gelöscht.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Fehler beim Löschen: {e}", "danger")
        logger.exception("Error deleting all debts")
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('schulden'))






@app.route('/schulden/edit/<debt_id>', methods=['POST'])
@login_required(['admin', 'seller'])
def edit_debt_route(debt_id):
    name = request.form.get('name')
    description = request.form.get('description')
    amount = request.form.get('amount')
    phone = request.form.get('phone')

    if not name or not description or not amount or not phone:
        flash("Alle Felder sind erforderlich.", "danger")
        return redirect(url_for('schulden'))

    try:
        amount = float(amount)
    except ValueError:
        flash("Der Betrag muss eine Zahl sein.", "danger")
        return redirect(url_for('schulden'))

    try:
        conn = get_connection()
        cur = conn.cursor()
        has_payments = bool(fetch_one("SELECT 1 FROM debt_payments WHERE debt_id = %s LIMIT 1;", (debt_id,)))
        if has_payments:
            # A partially-paid debt's principal isn't something this quick
            # edit form should silently redefine — only the remaining
            # balance is updated, exactly as before this feature existed.
            cur.execute("""
                UPDATE debts SET client_name=%s, description=%s, amount=%s, phone_number=%s
                WHERE debt_id=%s
            """, (name, description, amount, phone, debt_id))
        else:
            # No payments yet, so "remaining" and "original" are still the
            # same number — keep original_amount in sync too.
            cur.execute("""
                UPDATE debts SET client_name=%s, description=%s, amount=%s, original_amount=%s, phone_number=%s
                WHERE debt_id=%s
            """, (name, description, amount, amount, phone, debt_id))
        conn.commit()
        log_audit('debt_updated', 'debt', debt_id,
                   format_debt_details(name, amount, description, phone),
                   actor=session.get('username'), module='debts')
        flash("Schuld erfolgreich aktualisiert!", "success")
    except Exception as e:
        flash(f"Datenbankfehler: {e}", "danger")
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('schulden'))




@app.route('/schulden/redeem/<debt_id>', methods=['POST'])
@login_required(['admin', 'seller'])
def redeem_debt_route(debt_id):
    """Mark a debt as fully paid in one click. Kept working exactly as
    before for existing callers that send no body (clients.html's
    checkmark button) — those now default to a 'cash' payment, recorded
    in the new payment history the same as every other payment. Callers
    that do send a payment_method (schulden.html's newer UI) can pick
    cash/card explicitly.
    """
    payment_method = (
        (request.form.get('payment_method') if request.form else None)
        or (request.get_json(silent=True) or {}).get('payment_method')
        or 'cash'
    )
    stripe_payment_intent_id = (
        (request.form.get('stripe_payment_intent_id') if request.form else None)
        or (request.get_json(silent=True) or {}).get('stripe_payment_intent_id')
        or None
    )
    debt = query_one("SELECT client_name, amount, original_amount FROM debts WHERE debt_id = %s", (debt_id,))
    if not debt:
        return jsonify({'error': 'Debt not found'}), 404

    if (payment_method or '').strip().lower() == 'card':
        try:
            verify_stripe_terminal_payment(stripe_payment_intent_id, debt['amount'])
        except Exception as e:
            return jsonify({'error': f'Kartenzahlung konnte nicht bestätigt werden: {e}'}), 400

    try:
        updated_debt = record_debt_payment(debt_id, debt['amount'], payment_method, recorded_by=session.get('username'), stripe_payment_intent_id=stripe_payment_intent_id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception:
        logger.exception("Error redeeming debt")
        return jsonify({'error': 'Internal server error'}), 500

    log_audit('debt_payment', 'debt', debt_id,
               format_debt_payment_details(debt['client_name'], debt['amount'], payment_method, remaining=updated_debt['amount'], original_amount=debt.get('original_amount')),
               actor=session.get('username'), module='debts')
    return jsonify({'success': True}), 200


@app.route('/schulden/<debt_id>/pay', methods=['POST'])
@login_required(['admin', 'seller'])
def pay_debt_route(debt_id):
    """Record a cash/card payment against a debt — supports partial
    payments (e.g. debt of €50, client pays €30 now, €20 remains owed).
    Recalculates the debt's remaining balance/paid flag automatically via
    record_debt_payment(), so every statistic that reads debts.amount is
    correct on its very next query — no separate stats to refresh.
    """
    data = request.get_json(silent=True) if request.is_json else None
    amount_raw = (data or {}).get('amount') if data else request.form.get('amount')
    payment_method = ((data or {}).get('payment_method') if data else request.form.get('payment_method')) or 'cash'
    stripe_payment_intent_id = ((data or {}).get('stripe_payment_intent_id') if data else request.form.get('stripe_payment_intent_id')) or None

    debt = query_one("SELECT client_name, amount, original_amount FROM debts WHERE debt_id = %s", (debt_id,))
    if not debt:
        return jsonify({'success': False, 'message': 'Schuld wurde nicht gefunden.'}), 404

    try:
        amount = float(amount_raw)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Der Betrag muss eine Zahl sein.'}), 400

    # Same rule as sales: a "card" debt payment must have actually been
    # captured by the physical Stripe Terminal reader first — never take
    # the browser's word alone that a card payment went through.
    if (payment_method or '').strip().lower() == 'card':
        try:
            verify_stripe_terminal_payment(stripe_payment_intent_id, amount)
        except Exception as e:
            return jsonify({'success': False, 'message': f'Kartenzahlung konnte nicht bestätigt werden: {e}'}), 400

    try:
        updated_debt = record_debt_payment(debt_id, amount, payment_method, recorded_by=session.get('username'), stripe_payment_intent_id=stripe_payment_intent_id)
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except Exception:
        logger.exception('Error recording debt payment for %s', debt_id)
        return jsonify({'success': False, 'message': 'Datenbankfehler beim Speichern der Zahlung.'}), 500

    log_audit('debt_payment', 'debt', debt_id,
               format_debt_payment_details(debt['client_name'], amount, payment_method, remaining=updated_debt['amount'], original_amount=debt.get('original_amount')),
               actor=session.get('username'), module='debts')
    return jsonify({
        'success': True,
        'remaining': float(updated_debt['amount']),
        'paid': bool(updated_debt['paid']),
        'message': f"Zahlung von €{amount:.2f} ({payment_method}) gespeichert." + (' Schuld vollständig bezahlt.' if updated_debt['paid'] else f" Restbetrag: €{float(updated_debt['amount']):.2f}"),
    })


@app.route('/schulden/<debt_id>/payments', methods=['GET'])
@login_required(['admin', 'seller'])
def list_debt_payments_route(debt_id):
    """Full payment history for one debt, for the UI's history panel."""
    payments = list_debt_payments(debt_id)
    return jsonify({'payments': [{
        'id': p['id'],
        'amount': float(p['amount']),
        'payment_method': p['payment_method'],
        'paid_at': p['paid_at'].strftime('%Y-%m-%d %H:%M') if p.get('paid_at') else None,
        'recorded_by': p.get('recorded_by'),
    } for p in payments]})


@app.route('/schulden/payments/<int:payment_id>/edit', methods=['POST'])
@login_required(['admin', 'seller'])
def edit_debt_payment_route(payment_id):
    """Correct a previously recorded payment's amount/method. The parent
    debt's remaining balance and paid flag are recalculated automatically
    (old amount added back, new amount re-applied) — see edit_debt_payment()
    in db.py.
    """
    data = request.get_json(silent=True) if request.is_json else None
    amount_raw = (data or {}).get('amount') if data else request.form.get('amount')
    payment_method = (data or {}).get('payment_method') if data else request.form.get('payment_method')

    try:
        amount = float(amount_raw) if amount_raw not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Der Betrag muss eine Zahl sein.'}), 400

    try:
        payment, updated_debt = edit_debt_payment(payment_id, amount=amount, payment_method=payment_method)
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except Exception:
        logger.exception('Error editing debt payment %s', payment_id)
        return jsonify({'success': False, 'message': 'Datenbankfehler beim Bearbeiten der Zahlung.'}), 500

    log_audit('debt_payment', 'debt', payment['debt_id'],
               f"Payment #{payment_id} updated to {format_money(payment['amount'])} via {payment['payment_method'].title()}"
               f" (remaining: {format_money(updated_debt['amount'])})",
               actor=session.get('username'), module='debts')
    return jsonify({'success': True, 'remaining': float(updated_debt['amount']), 'paid': bool(updated_debt['paid'])})


@app.route('/schulden/payments/<int:payment_id>/delete', methods=['POST'])
@login_required('admin')
def delete_debt_payment_route(payment_id):
    """Remove a payment entirely. The amount is added back onto the
    debt's remaining balance (un-marking it paid if needed) — recalculated
    automatically via delete_debt_payment() in db.py.
    """
    payment = get_debt_payment(payment_id)
    if not payment:
        return jsonify({'success': False, 'message': 'Zahlung wurde nicht gefunden.'}), 404
    try:
        updated_debt = delete_debt_payment(payment_id)
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except Exception:
        logger.exception('Error deleting debt payment %s', payment_id)
        return jsonify({'success': False, 'message': 'Datenbankfehler beim Löschen der Zahlung.'}), 500

    log_audit('debt_payment', 'debt', payment['debt_id'],
               f"Payment #{payment_id} of {format_money(payment['amount'])} ({payment['payment_method']}) deleted"
               f" (remaining: {format_money(updated_debt['amount'])})",
               actor=session.get('username'), module='debts')
    return jsonify({'success': True, 'remaining': float(updated_debt['amount']), 'paid': bool(updated_debt['paid'])})


@app.route('/clients')
def clients():
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # Einzigartige Kunden mit Gesamtstatus
        cur.execute("""
            SELECT client_name,
                MAX(paid = FALSE) AS has_unpaid,
                COALESCE(SUM(CASE WHEN paid = FALSE THEN amount ELSE 0 END), 0) AS total_unpaid
            FROM debts
            GROUP BY client_name
            ORDER BY client_name
        """)
        clients = cur.fetchall()

    except Exception as e:
        clients = []
    finally:
        cur.close()
        conn.close()

    return render_template('clients.html', clients=clients)


@app.route('/clients/<client_name>/debts')
def client_debts(client_name):
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT debt_id, client_name, description, amount, original_amount, phone_number, paid, created_at
            FROM debts
            WHERE client_name = %s
            ORDER BY created_at DESC
        """, (client_name,))
        debts = cur.fetchall()

        for debt in debts:
            if debt['created_at']:
                debt['created_at'] = debt['created_at'].strftime('%Y-%m-%d %H:%M:%S')

    except Exception:
        debts = []
    finally:
        cur.close()
        conn.close()

    return jsonify(debts)


@app.route('/save_kasse_balance', methods=['POST'])
@login_required('admin')
def save_kasse_balance():
    try:
        balance = float(request.form.get('balance', 0))
        today = datetime.now().date()

        query = """
            INSERT INTO daily_cash_balance (date, closing_balance)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE closing_balance = VALUES(closing_balance);
        """
        execute_query(query, (today, balance))
        log_audit('kasse_balance_updated', 'kasse_balance', str(today), f'Closing balance for {today}: {format_money(balance)}', actor=session.get('username'), module='cash')
        flash("✅ Tagesabschluss gespeichert!", "success")
    except Exception as e:
        flash(f"❌ Fehler beim Speichern des Kassenstands: {e}", "danger")

    return redirect(url_for('admin_dashboard'))


# ---------------------------------------------------------------------------
# Factures / Invoices (eBay, Strom, Internet, Lieferanten, ...)
# ---------------------------------------------------------------------------

@app.route('/factures')
@login_required('admin')
def list_factures():
    facture_type = request.args.get('type') or None
    status = request.args.get('status') or None

    factures = get_factures(facture_type=facture_type, status=status)
    summary = get_facture_summary()

    today = datetime.now().date()
    for f in factures:
        due = f.get('due_date')
        f['is_overdue'] = bool(f.get('status') == 'unpaid' and due and due < today)

    return render_template(
        'factures.html',
        factures=factures,
        summary=summary,
        facture_types=FACTURE_TYPES,
        selected_type=facture_type,
        selected_status=status,
    )


@app.route('/factures/add', methods=['GET', 'POST'])
@login_required('admin')
def add_facture_route():
    if request.method == 'POST':
        try:
            data = {
                'facture_type': request.form.get('facture_type', 'other'),
                'reference': request.form.get('reference') or None,
                'issuer': request.form['issuer'].strip(),
                'amount': float(request.form['amount']),
                'currency': request.form.get('currency', 'EUR'),
                'issue_date': request.form['issue_date'],
                'due_date': request.form.get('due_date') or None,
                'status': request.form.get('status', 'unpaid'),
                'notes': request.form.get('notes') or None,
                'created_by': session.get('username'),
            }
            new_id = add_facture(data)
            log_audit('invoice_created', 'facture', new_id,
                       format_invoice_details(data['issuer'], data['amount'], data['facture_type'], data['status']),
                       actor=session.get('username'), module='invoices')
            flash('Rechnung erfolgreich gespeichert.', 'success')
            return redirect(url_for('list_factures'))
        except (KeyError, ValueError) as e:
            flash(f'Ungültige Eingabe: {e}', 'danger')
        except Exception:
            logger.exception('Failed to add facture')
            flash('Fehler beim Speichern der Rechnung.', 'danger')

    return render_template('facture_form.html', facture=None, facture_types=FACTURE_TYPES, today=datetime.now().date())


@app.route('/factures/edit/<int:facture_id>', methods=['GET', 'POST'])
@login_required('admin')
def edit_facture_route(facture_id):
    facture = get_facture(facture_id)
    if not facture:
        flash('Rechnung nicht gefunden.', 'warning')
        return redirect(url_for('list_factures'))

    if request.method == 'POST':
        try:
            data = {
                'facture_type': request.form.get('facture_type', 'other'),
                'reference': request.form.get('reference') or None,
                'issuer': request.form['issuer'].strip(),
                'amount': float(request.form['amount']),
                'currency': request.form.get('currency', 'EUR'),
                'issue_date': request.form['issue_date'],
                'due_date': request.form.get('due_date') or None,
                'status': request.form.get('status', 'unpaid'),
                'notes': request.form.get('notes') or None,
            }
            update_facture(facture_id, data)
            log_audit('invoice_updated', 'facture', facture_id,
                       format_invoice_details(data['issuer'], data['amount'], data['facture_type'], data['status']),
                       actor=session.get('username'), module='invoices')
            flash('Rechnung erfolgreich aktualisiert.', 'success')
            return redirect(url_for('list_factures'))
        except (KeyError, ValueError) as e:
            flash(f'Ungültige Eingabe: {e}', 'danger')
        except Exception:
            logger.exception('Failed to update facture %s', facture_id)
            flash('Fehler beim Aktualisieren der Rechnung.', 'danger')

    return render_template('facture_form.html', facture=facture, facture_types=FACTURE_TYPES, today=datetime.now().date())


@app.route('/factures/delete/<int:facture_id>', methods=['POST'])
@login_required('admin')
def delete_facture_route(facture_id):
    try:
        facture = get_facture(facture_id)
        delete_facture(facture_id)
        details = format_invoice_details(facture.get('issuer'), facture.get('amount'), facture.get('facture_type')) if facture else f'Invoice #{facture_id}'
        log_audit('invoice_deleted', 'facture', facture_id, details, actor=session.get('username'), module='invoices')
        flash('Rechnung gelöscht.', 'success')
    except Exception:
        logger.exception('Failed to delete facture %s', facture_id)
        flash('Fehler beim Löschen der Rechnung.', 'danger')
    return redirect(url_for('list_factures'))


@app.route('/factures/<int:facture_id>/mark-paid', methods=['POST'])
@login_required('admin')
def mark_facture_paid(facture_id):
    facture = get_facture(facture_id)
    if not facture:
        flash('Rechnung nicht gefunden.', 'warning')
        return redirect(url_for('list_factures'))
    try:
        new_status = 'paid' if facture.get('status') != 'paid' else 'unpaid'
        execute_query("UPDATE factures SET status = %s WHERE id = %s;", (new_status, facture_id))
        action_name = 'invoice_payment' if new_status == 'paid' else 'invoice_status_updated'
        log_audit(action_name, 'facture', facture_id,
                   format_invoice_details(facture.get('issuer'), facture.get('amount'), facture.get('facture_type'), new_status),
                   actor=session.get('username'), module='invoices')
        flash('Status aktualisiert.', 'success')
    except Exception:
        logger.exception('Failed to toggle facture status %s', facture_id)
        flash('Fehler beim Aktualisieren des Status.', 'danger')
    return redirect(url_for('list_factures'))


# ---------------------------------------------------------------------------
# Assistant (button-driven chatbot) — JSON API
# Everything here reuses the same underlying data/logic as the regular pages;
# it just exposes it as small JSON actions the chat widget can call.
# ---------------------------------------------------------------------------

@app.route('/assistant/api/summary')
@login_required(['admin', 'seller'])
def assistant_summary():
    today = datetime.now().date()
    items = load_items()

    debts_open = fetch_one("SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS cnt FROM debts WHERE paid = FALSE;")
    factures_summary = get_facture_summary()
    low_stock = get_low_stock_notifications(items, threshold=5)

    return jsonify({
        'sales_today': calculate_today_sales(),
        'profit_today': calculate_today_profit(),
        'sales_month': calculate_monthly_sales(),
        'kasse_balance': get_kasse_balance_for_date(today),
        'debts_open_total': round(float(debts_open['total'] or 0), 2),
        'debts_open_count': int(debts_open['cnt'] or 0),
        'factures_unpaid_total': factures_summary['unpaid_amount'],
        'low_stock_count': len(low_stock),
    })


@app.route('/assistant/api/debts/open')
@login_required('admin')
def assistant_debts_open():
    rows = fetch_all(
        "SELECT debt_id, reference_number, client_name, description, amount, phone_number "
        "FROM debts WHERE paid = FALSE ORDER BY created_at DESC LIMIT 20;"
    )
    return jsonify({'debts': rows})


@app.route('/assistant/api/debts', methods=['POST'])
@login_required('admin')
def assistant_add_debt():
    data = request.get_json(silent=True) or {}
    name = (data.get('client_name') or '').strip()
    amount = data.get('amount')
    phone = (data.get('phone_number') or '').strip()
    description = (data.get('description') or '').strip()

    if not name or not amount:
        return jsonify({'success': False, 'message': 'Name und Betrag sind erforderlich.'}), 400
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Der Betrag muss eine Zahl sein.'}), 400

    debt_id = str(uuid.uuid4())[:8].upper()
    reference_number = generate_debt_reference_number()
    try:
        execute_query(
            "INSERT INTO debts (debt_id, reference_number, client_name, description, amount, original_amount, phone_number) VALUES (%s, %s, %s, %s, %s, %s, %s);",
            (debt_id, reference_number, name, description or None, amount, amount, phone or None),
        )
    except Exception:
        logger.exception('Assistant failed to add debt')
        return jsonify({'success': False, 'message': 'Datenbankfehler beim Hinzufügen der Schuld.'}), 500

    return jsonify({
        'success': True,
        'message': f'Schuld über €{amount:,.2f} für {name} wurde hinzugefügt.',
        'debt_id': debt_id,
        'reference_number': reference_number,
    })


@app.route('/assistant/api/debts/<debt_id>/edit', methods=['POST'])
@login_required('admin')
def assistant_edit_debt(debt_id):
    """Correct a debt's own details (client name, remaining amount, phone,
    description) — distinct from recording a payment against it. Only
    fields actually present in the request body are changed."""
    debt = query_one("SELECT * FROM debts WHERE debt_id = %s;", (debt_id,))
    if not debt:
        return jsonify({'success': False, 'message': 'Schuld wurde nicht gefunden.'}), 404

    data = request.get_json(silent=True) or {}
    new_name = data.get('client_name')
    new_amount = data.get('amount')
    new_phone = data.get('phone_number')
    new_description = data.get('description')

    client_name = (new_name.strip() if new_name is not None else debt['client_name']) or ''
    if not client_name:
        return jsonify({'success': False, 'message': 'Der Kundenname darf nicht leer sein.'}), 400

    if new_amount is not None:
        try:
            amount = float(new_amount)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': 'Der Betrag muss eine Zahl sein.'}), 400
        if amount < 0:
            return jsonify({'success': False, 'message': 'Der Betrag darf nicht negativ sein.'}), 400
    else:
        amount = float(debt['amount'])

    phone_number = (new_phone.strip() if new_phone is not None else debt.get('phone_number')) or None
    description = (new_description.strip() if new_description is not None else debt.get('description')) or None

    try:
        execute_query(
            "UPDATE debts SET client_name = %s, amount = %s, phone_number = %s, description = %s WHERE debt_id = %s;",
            (client_name, amount, phone_number, description, debt_id),
        )
    except Exception:
        logger.exception('Assistant failed to edit debt %s', debt_id)
        return jsonify({'success': False, 'message': 'Datenbankfehler beim Bearbeiten der Schuld.'}), 500

    log_audit('debt_updated', 'debt', debt_id,
              format_debt_details(client_name, amount, description, phone_number),
              actor=session.get('username'), module='debts')

    updated = query_one("SELECT debt_id, reference_number, client_name, description, amount, phone_number FROM debts WHERE debt_id = %s;", (debt_id,))
    return jsonify({'success': True, 'message': f'Schuld von {client_name} wurde aktualisiert.', 'debt': updated})


@app.route('/assistant/api/debts/<debt_id>/pay', methods=['POST'])
@login_required('admin')
def assistant_pay_debt(debt_id):
    """Settle a debt IN FULL. For a partial payment, use
    /assistant/api/debts/<debt_id>/record_payment instead."""
    data = request.get_json(silent=True) or {}
    debt = query_one("SELECT client_name, amount, original_amount FROM debts WHERE debt_id = %s;", (debt_id,))
    if not debt:
        return jsonify({'success': False, 'message': 'Schuld wurde nicht gefunden.'}), 404

    payment_method = (data.get('payment_method') or 'cash').strip().lower()
    amount = float(debt['amount'])  # always the full remaining balance

    # A "card" debt payment from the chat assistant must go through the same
    # physical Stripe Terminal (TPE) reader as the schulden.html UI — never
    # just trust the chat's word that a card payment "went through".
    stripe_payment_intent_id = (data.get('stripe_payment_intent_id') or '').strip() or None
    if payment_method == 'card':
        try:
            verify_stripe_terminal_payment(stripe_payment_intent_id, amount)
        except Exception as e:
            return jsonify({'success': False, 'message': f'Kartenzahlung konnte nicht bestätigt werden: {e}'}), 400

    try:
        updated_debt = record_debt_payment(debt_id, amount, payment_method, recorded_by=session.get('username'), stripe_payment_intent_id=stripe_payment_intent_id)
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except Exception as e:
        logger.exception('Assistant failed to record debt payment')
        return jsonify({'success': False, 'message': f'Fehler beim Speichern der Zahlung. Grund: {e}'}), 500

    log_audit('debt_payment', 'debt', debt_id,
               format_debt_payment_details(debt['client_name'], amount, payment_method, remaining=updated_debt['amount'], original_amount=debt.get('original_amount')),
               actor=session.get('username'), source='assistant', module='debts')
    return jsonify({'success': True, 'message': 'Schuld wurde vollständig bezahlt.',
                     'remaining': float(updated_debt['amount']), 'paid': bool(updated_debt['paid'])})


@app.route('/assistant/api/debts/<debt_id>/record_payment', methods=['POST'])
@login_required('admin')
def assistant_record_debt_payment(debt_id):
    """Record a partial (or full) cash/card payment against a debt without
    assuming it's fully settled. Requires an explicit amount, unlike
    /assistant/api/debts/<debt_id>/pay which always pays the full balance."""
    data = request.get_json(silent=True) or {}
    debt = query_one("SELECT client_name, amount, original_amount FROM debts WHERE debt_id = %s;", (debt_id,))
    if not debt:
        return jsonify({'success': False, 'message': 'Schuld wurde nicht gefunden.'}), 404

    amount_raw = data.get('amount')
    payment_method = (data.get('payment_method') or 'cash').strip().lower()
    try:
        if amount_raw in (None, ''):
            return jsonify({'success': False, 'message': 'Der Betrag ist erforderlich.'}), 400
        amount = float(amount_raw)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Der Betrag muss eine Zahl sein.'}), 400

    stripe_payment_intent_id = (data.get('stripe_payment_intent_id') or '').strip() or None
    if payment_method == 'card':
        try:
            verify_stripe_terminal_payment(stripe_payment_intent_id, amount)
        except Exception as e:
            return jsonify({'success': False, 'message': f'Kartenzahlung konnte nicht bestätigt werden: {e}'}), 400

    try:
        updated_debt = record_debt_payment(debt_id, amount, payment_method, recorded_by=session.get('username'), stripe_payment_intent_id=stripe_payment_intent_id)
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except Exception as e:
        logger.exception('Assistant failed to record debt payment')
        return jsonify({'success': False, 'message': f'Fehler beim Speichern der Zahlung. Grund: {e}'}), 500

    log_audit('debt_payment', 'debt', debt_id,
               format_debt_payment_details(debt['client_name'], amount, payment_method, remaining=updated_debt['amount'], original_amount=debt.get('original_amount')),
               actor=session.get('username'), source='assistant', module='debts')
    message = 'Schuld wurde vollständig bezahlt.' if updated_debt['paid'] else f"Teilzahlung gespeichert. Restbetrag: €{float(updated_debt['amount']):.2f}"
    return jsonify({'success': True, 'message': message, 'remaining': float(updated_debt['amount']), 'paid': bool(updated_debt['paid'])})


@app.route('/assistant/api/debts/delete_all', methods=['POST'])
@login_required('admin')
def assistant_delete_all_debts():
    """Used by the button-driven chat's 'delete all debts' action. Wipes
    every debt record for every client — paid and unpaid alike."""
    try:
        count_row = fetch_one("SELECT COUNT(*) AS cnt FROM debts;")
        execute_query("DELETE FROM debts;")
    except Exception:
        logger.exception('Assistant failed to delete all debts')
        return jsonify({'success': False, 'message': 'Fehler beim Löschen der Schulden.'}), 500
    return jsonify({'success': True, 'deleted_count': int(count_row['cnt'] or 0)})


@app.route('/assistant/api/clients')
@login_required('admin')
def assistant_list_clients():
    """List the people (clients) who have a debt record, distinct from the
    debts themselves — one client can have several debts (paid or open)."""
    rows = fetch_all(
        """
        SELECT client_name,
               MAX(paid = FALSE) AS has_unpaid,
               COUNT(*) AS debt_count,
               COALESCE(SUM(CASE WHEN paid = FALSE THEN amount ELSE 0 END), 0) AS total_unpaid
        FROM debts
        GROUP BY client_name
        ORDER BY client_name;
        """
    )
    out = [{
        'client_name': r['client_name'],
        'has_unpaid': bool(r['has_unpaid']),
        'debt_count': int(r['debt_count'] or 0),
        'total_unpaid': round(float(r['total_unpaid'] or 0), 2),
    } for r in rows]
    return jsonify({'clients': out})


@app.route('/assistant/api/clients/<client_name>/debts')
@login_required('admin')
def assistant_client_debts(client_name):
    """Full debt history (paid and unpaid) for a single client — used when
    the chat needs to look at a *person* rather than the generic open-debts
    list, so it doesn't confuse 'a debt' with 'a client'."""
    rows = fetch_all(
        """
        SELECT debt_id, client_name, description, amount, phone_number, paid, created_at
        FROM debts
        WHERE client_name = %s
        ORDER BY created_at DESC;
        """,
        (client_name,),
    )
    out = []
    for r in rows:
        out.append({
            'debt_id': r['debt_id'],
            'description': r.get('description'),
            'amount': round(float(r['amount'] or 0), 2),
            'phone_number': r.get('phone_number'),
            'paid': bool(r['paid']),
            'created_at': r['created_at'].strftime('%Y-%m-%d') if r.get('created_at') else None,
        })
    return jsonify({'client_name': client_name, 'debts': out})


@app.route('/assistant/api/clients/<client_name>', methods=['PUT'])
@login_required('admin')
def assistant_rename_client(client_name):
    """See rename_client() above: clients are derived from debts.client_name,
    so \"updating\" one means renaming it across every debt row it has."""
    row = fetch_one("SELECT COUNT(*) AS cnt FROM debts WHERE client_name = %s;", (client_name,))
    if not row or not row['cnt']:
        return jsonify({'success': False, 'message': f'Kein Kunde mit dem Namen "{client_name}" gefunden.'}), 404

    data = request.get_json(silent=True) or {}
    new_name = (data.get('client_name') or data.get('new_client_name') or '').strip()
    if not new_name:
        return jsonify({'success': False, 'message': 'Ein neuer Kundenname ist erforderlich.'}), 400
    if new_name == client_name:
        return jsonify({'success': False, 'message': 'Der neue Name ist identisch mit dem alten.'}), 400

    try:
        execute_query("UPDATE debts SET client_name = %s WHERE client_name = %s;", (new_name, client_name))
        log_audit('customer_renamed', 'client', new_name,
                   f'Renamed customer "{client_name}" to "{new_name}"',
                   actor=session.get('username'), module='customers')
    except Exception:
        logger.exception('Assistant failed to rename client %s', client_name)
        return jsonify({'success': False, 'message': 'Datenbankfehler beim Umbenennen des Kunden.'}), 500

    return jsonify({'success': True, 'message': f'Kunde "{client_name}" wurde in "{new_name}" umbenannt.',
                     'client_name': new_name})


@app.route('/assistant/api/factures/unpaid')
@login_required('admin')
def assistant_factures_unpaid():
    q = (request.args.get('q') or '').strip() or None
    try:
        limit = min(max(int(request.args.get('limit', 5)), 1), 50)
    except (TypeError, ValueError):
        limit = 5
    try:
        offset = max(int(request.args.get('offset', 0)), 0)
    except (TypeError, ValueError):
        offset = 0

    # count_factures + a LIMIT/OFFSET page keeps this cheap regardless of how
    # many unpaid invoices exist — no more silently capping at 20 rows.
    total = count_factures(status='unpaid', search=q)
    rows = get_factures(status='unpaid', search=q, limit=limit, offset=offset)
    out = []
    for f in rows:
        out.append({
            'id': f['id'],
            'issuer': f['issuer'],
            'amount': float(f['amount']),
            'currency': f.get('currency', 'EUR'),
            'facture_type': f.get('facture_type'),
            'facture_type_label': FACTURE_TYPES.get(f.get('facture_type'), f.get('facture_type')),
            'due_date': f['due_date'].isoformat() if f.get('due_date') else None,
            'due_date_display': f['due_date'].strftime('%d.%m.%Y') if f.get('due_date') else None,
        })
    return jsonify({
        'factures': out, 'total': total, 'offset': offset, 'limit': limit,
        'has_more': offset + len(out) < total,
    })


@app.route('/assistant/api/factures/<int:facture_id>')
@login_required('admin')
def assistant_get_facture(facture_id):
    f = get_facture(facture_id)
    if not f:
        return jsonify({'success': False, 'message': 'Rechnung nicht gefunden.'}), 404
    return jsonify({'success': True, 'facture': {
        'id': f['id'],
        'facture_type': f.get('facture_type', 'other'),
        'issuer': f['issuer'],
        'amount': float(f['amount']),
        'currency': f.get('currency', 'EUR'),
        'issue_date': f['issue_date'].isoformat() if f.get('issue_date') else None,
        'due_date': f['due_date'].isoformat() if f.get('due_date') else None,
        'status': f.get('status'),
        'notes': f.get('notes'),
    }})


@app.route('/assistant/api/factures/<int:facture_id>', methods=['PUT'])
@login_required('admin')
def assistant_update_facture(facture_id):
    facture = get_facture(facture_id)
    if not facture:
        return jsonify({'success': False, 'message': 'Rechnung nicht gefunden.'}), 404
    data = request.get_json(silent=True) or {}
    try:
        payload = {
            'facture_type': data.get('facture_type') or facture.get('facture_type', 'other'),
            'issuer': (data.get('issuer') or facture['issuer']).strip(),
            'amount': float(data.get('amount')) if data.get('amount') not in (None, '') else float(facture['amount']),
            'currency': data.get('currency') or facture.get('currency', 'EUR'),
            'issue_date': data.get('issue_date') or (facture['issue_date'].isoformat() if facture.get('issue_date') else datetime.now().date().isoformat()),
            'due_date': data['due_date'] if 'due_date' in data else (facture['due_date'].isoformat() if facture.get('due_date') else None),
            'status': data.get('status') or facture.get('status', 'unpaid'),
            'notes': data['notes'] if 'notes' in data else facture.get('notes'),
        }
        if not payload['issuer']:
            raise ValueError('issuer required')
        update_facture(facture_id, payload)
        log_audit('invoice_updated', 'facture', facture_id,
                   format_invoice_details(payload['issuer'], payload['amount'], payload['facture_type'], payload['status']),
                   actor=session.get('username'), module='invoices')
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Bitte Aussteller und einen gültigen Betrag angeben.'}), 400
    except Exception:
        logger.exception('Assistant failed to update facture %s', facture_id)
        return jsonify({'success': False, 'message': 'Datenbankfehler beim Aktualisieren der Rechnung.'}), 500

    return jsonify({'success': True, 'message': f"Rechnung von {payload['issuer']} wurde aktualisiert.", 'facture': payload})


@app.route('/assistant/api/factures/<int:facture_id>', methods=['DELETE'])
@login_required('admin')
def assistant_delete_facture(facture_id):
    facture = get_facture(facture_id)
    if not facture:
        return jsonify({'success': False, 'message': 'Rechnung nicht gefunden.'}), 404
    try:
        details = format_invoice_details(facture.get('issuer'), facture.get('amount'), facture.get('facture_type'))
        delete_facture(facture_id)
        log_audit('invoice_deleted', 'facture', facture_id, details, actor=session.get('username'), module='invoices')
    except Exception:
        logger.exception('Assistant failed to delete facture %s', facture_id)
        return jsonify({'success': False, 'message': 'Fehler beim Löschen der Rechnung.'}), 500
    return jsonify({'success': True, 'message': 'Rechnung wurde gelöscht.'})


@app.route('/assistant/api/factures', methods=['POST'])
@login_required('admin')
def assistant_add_facture():
    data = request.get_json(silent=True) or {}
    try:
        payload = {
            'facture_type': data.get('facture_type', 'other'),
            'issuer': (data.get('issuer') or '').strip(),
            'amount': float(data.get('amount')),
            'currency': data.get('currency', 'EUR'),
            'issue_date': data.get('issue_date') or datetime.now().date().isoformat(),
            'due_date': data.get('due_date') or None,
            'status': 'unpaid',
            'notes': data.get('notes') or None,
            'created_by': session.get('username'),
        }
        if not payload['issuer']:
            raise ValueError('issuer required')
        add_facture(payload)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Bitte Aussteller und einen gültigen Betrag angeben.'}), 400
    except Exception:
        logger.exception('Assistant failed to add facture')
        return jsonify({'success': False, 'message': 'Datenbankfehler beim Hinzufügen der Rechnung.'}), 500

    return jsonify({'success': True, 'message': f"Rechnung von {payload['issuer']} über €{payload['amount']:,.2f} wurde gespeichert."})


@app.route('/assistant/api/factures/<int:facture_id>/pay', methods=['POST'])
@login_required('admin')
def assistant_pay_facture(facture_id):
    facture = get_facture(facture_id)
    if not facture:
        return jsonify({'success': False, 'message': 'Rechnung nicht gefunden.'}), 404
    try:
        execute_query("UPDATE factures SET status = 'paid' WHERE id = %s;", (facture_id,))
    except Exception:
        logger.exception('Assistant failed to mark facture paid')
        return jsonify({'success': False, 'message': 'Fehler beim Aktualisieren.'}), 500
    return jsonify({'success': True, 'message': 'Rechnung wurde als bezahlt markiert.'})


@app.route('/assistant/api/stock/low')
@login_required(['admin', 'seller'])
def assistant_low_stock():
    items = load_items()
    notes = get_low_stock_notifications(items, threshold=5)
    out = [{'barcode': n.get('barcode'), 'message': n.get('message')} for n in notes[:20]]
    return jsonify({'items': out})


@app.route('/assistant/api/kasse/today')
@login_required(['admin', 'seller'])
def assistant_kasse_today():
    if not seller_kasse_enabled():
        return jsonify({'success': False, 'message': 'Kasse-Zugriff wurde für Ihr Konto deaktiviert.'}), 403
    # Same fix as the dashboard route: without this call, get_kasse_balance_
    # for_date(today) can come back empty if nobody has opened the dashboard
    # yet today (that's what actually fills in daily_cash_balance for today).
    calculate_and_save_today_closing_balance()
    today = datetime.now().date()
    return jsonify({
        'balance': get_kasse_balance_for_date(today) or 0,
        'sales_today': calculate_sales_for_date(today),
        'purchases_today': calculate_purchases_for_date(today),
        'cash_debt_payments_today': calculate_cash_debt_payments_for_date(today),
        'cash_salary_payments_today': calculate_cash_salary_payments_for_date(today),
        'cash_deposits_today': calculate_cash_deposits_for_date(today),
        'cash_withdrawals_today': calculate_cash_withdrawals_for_date(today),
    })


@app.route('/assistant/api/kasse/transactions')
@login_required(['admin', 'seller'])
def assistant_kasse_transactions():
    """Recent manual cash register (Kasse) deposit/withdrawal entries, with
    their ids, for the button-driven chat's "show history" action — same
    query the AI's list_kasse_transactions tool uses."""
    if not seller_kasse_enabled():
        return jsonify({'success': False, 'message': 'Kasse-Zugriff wurde für Ihr Konto deaktiviert.'}), 403
    rows = fetch_all(
        "SELECT id, date, amount, type, description, username, payment_method "
        "FROM cash_transactions ORDER BY date DESC LIMIT 20;"
    )
    out = []
    for r in rows:
        raw_date = r.get('date')
        date_str = raw_date.strftime('%d.%m.%Y %H:%M') if hasattr(raw_date, 'strftime') else (str(raw_date) if raw_date else None)
        out.append({
            'transaction_id': r['id'],
            'date': date_str,
            'amount': float(r['amount'] or 0),
            'type': r.get('type'),
            'description': r.get('description'),
            'username': r.get('username'),
            'payment_method': r.get('payment_method'),
        })
    return jsonify({'transactions': out})


# ---------------------------------------------------------------------------
# Invoice photo scan (offline OCR via Tesseract) — "take a picture, auto-fill
# the invoice form". No external AI service is used: a photo is run through
# Tesseract OCR, then a handful of regexes guess the issuer / amount / dates.
# The result is always shown to the user to confirm/correct before saving.
# ---------------------------------------------------------------------------

FACTURE_TYPE_HINTS = {
    'ebay': ['ebay'],
    'electricity': ['strom', 'électricité', 'electricity', 'stromrechnung', 'energie', 'eon', 'vattenfall', 'edf'],
    'water': ['wasser', 'eau', 'water', 'stadtwerke'],
    'internet': ['internet', 'dsl', 'glasfaser', 'vodafone', 'telekom', 'o2', '1&1'],
    'phone': ['telefon', 'mobilfunk', 'phone', 'téléphone'],
    'supplier': ['lieferant', 'supplier', 'fournisseur', 'großhandel'],
    'rent': ['miete', 'rent', 'loyer', 'nebenkosten'],
}


def _guess_facture_type(text_lower):
    for facture_type, hints in FACTURE_TYPE_HINTS.items():
        if any(h in text_lower for h in hints):
            return facture_type
    return 'other'


# Keywords that sit right next to the number that actually matters on an
# invoice, in German/English/French (this app's three OCR languages). Order
# matters less than location: we prefer the amount found on the SAME line as
# one of these labels over "the biggest number on the page" (unreliable —
# invoices are full of bigger numbers: IBANs, phone numbers, tax IDs,
# per-unit prices in a line-item table).
_TOTAL_AMOUNT_KEYWORDS = [
    'gesamtbetrag', 'rechnungsbetrag', 'endbetrag', 'zu zahlen', 'zahlbetrag',
    'gesamtsumme', 'summe brutto', 'bruttobetrag', 'total ttc', 'montant total',
    'a payer', 'amount due', 'total due', 'grand total', 'balance due',
    'total amount', 'gesamt', 'summe', 'total',
]
_ISSUE_DATE_KEYWORDS = ['rechnungsdatum', 'invoice date', 'date de facture', 'ausgestellt', 'datum', 'date']
_DUE_DATE_KEYWORDS = ['zahlbar bis', 'zahlungsziel', 'payment due', 'due date', 'date limite', 'echeance', 'fallig']

_AMOUNT_RE = re.compile(r'(?<!\d)(\d{1,3}(?:[.,]\d{3})*[.,]\d{2}|\d+[.,]\d{2})(?!\d)')
_DATE_RE = re.compile(r'(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})')


def _normalise_amount(raw):
    if raw.count(',') == 1 and raw.count('.') == 0:
        normalised = raw.replace(',', '.')
    elif raw.count('.') == 1 and raw.count(',') == 0:
        normalised = raw
    else:
        normalised = raw.replace('.', '').replace(',', '.')
    try:
        return float(normalised)
    except ValueError:
        return None


def _parse_amount_candidates(text):
    """Find money-looking numbers (e.g. 1.234,56 / 1,234.56 / 45.00) and
    return them as floats. Kept as the last-resort fallback."""
    candidates = []
    for m in _AMOUNT_RE.finditer(text):
        val = _normalise_amount(m.group(1))
        if val is not None:
            candidates.append(val)
    return candidates


def _find_amount_near_keywords(lines):
    """Precise pass: look line-by-line for a total/amount-due keyword and
    grab the money-looking number on THAT line (or, failing that, the very
    next line -- totals are sometimes on their own line under the label).
    Returns None if no keyword line had a usable number, so the caller can
    fall back to the coarser heuristic."""
    for idx, line in enumerate(lines):
        low = line.lower()
        if any(kw in low for kw in _TOTAL_AMOUNT_KEYWORDS):
            for candidate_line in (line, lines[idx + 1] if idx + 1 < len(lines) else ''):
                found = _parse_amount_candidates(candidate_line)
                if found:
                    return max(found)
    return None


def _find_date_near_keywords(lines, keywords):
    for idx, line in enumerate(lines):
        low = line.lower()
        if any(kw in low for kw in keywords):
            for candidate_line in (line, lines[idx + 1] if idx + 1 < len(lines) else ''):
                for m in _DATE_RE.finditer(candidate_line):
                    d, mo, y = m.groups()
                    try:
                        y = int(y)
                        if y < 100:
                            y += 2000
                        return date(y, int(mo), int(d))
                    except ValueError:
                        continue
    return None


def _parse_date_candidates(text):
    found = []
    for m in _DATE_RE.finditer(text):
        d, mo, y = m.groups()
        try:
            y = int(y)
            if y < 100:
                y += 2000
            found.append(date(y, int(mo), int(d)))
        except ValueError:
            continue
    return found


def _guess_issuer_from_image(img):
    """Layout-aware issuer detection: the company name is almost always the
    single largest piece of text near the top of the page (the logo/letter-
    head), not necessarily the FIRST line of OCR text (that can be a
    reference number or address line above a small logo). image_to_data
    gives per-word bounding boxes, so we pick the tallest text block among
    the top third of the page instead of guessing from line order."""
    try:
        data = pytesseract.image_to_data(img, lang='deu+eng+fra', output_type=pytesseract.Output.DICT)
    except Exception:
        return None

    top_cutoff = img.height / 3
    lines = {}
    n = len(data.get('text', []))
    for i in range(n):
        word = (data['text'][i] or '').strip()
        if not word or len(re.sub(r'[^A-Za-z\u00C0-\u00FF]', '', word)) < 2:
            continue
        if data['top'][i] > top_cutoff:
            continue
        key = (data['block_num'][i], data['par_num'][i], data['line_num'][i])
        entry = lines.setdefault(key, {'words': [], 'height': 0, 'top': data['top'][i]})
        entry['words'].append(word)
        entry['height'] = max(entry['height'], data['height'][i])
        entry['top'] = min(entry['top'], data['top'][i])

    if not lines:
        return None
    best = max(lines.values(), key=lambda e: (e['height'], -e['top']))
    text = ' '.join(best['words']).strip()
    return text[:80] if text else None


def _extract_fields_from_text_and_image(text, img=None):
    """Shared precision extraction used by both the image-OCR path and the
    PDF-text-layer path (img is None for a native PDF text layer -- there's
    no bounding-box data to run the layout-aware issuer pass on)."""
    text_lower = text.lower()
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    issuer = _guess_issuer_from_image(img) if img is not None else None
    if not issuer:
        for line in lines[:8]:
            letters = re.sub(r'[^A-Za-z\u00C0-\u00FF]', '', line)
            if len(letters) >= 3:
                issuer = line[:80]
                break

    # Precise pass first (keyword-anchored); only fall back to "largest
    # number on the page" if no total/amount-due label was found at all.
    amount = _find_amount_near_keywords(lines)
    amount_source = 'keyword'
    if amount is None:
        amounts = _parse_amount_candidates(text)
        amount = max(amounts) if amounts else None
        amount_source = 'fallback' if amount is not None else None

    issue_dt = _find_date_near_keywords(lines, _ISSUE_DATE_KEYWORDS)
    due_dt = _find_date_near_keywords(lines, _DUE_DATE_KEYWORDS)
    if issue_dt is None or due_dt is None:
        dates_found = _parse_date_candidates(text)
        if dates_found:
            if issue_dt is None:
                issue_dt = min(dates_found)
            if due_dt is None and len(dates_found) > 1:
                others = [d for d in dates_found if d != issue_dt]
                due_dt = max(others) if others else None

    return {
        'ok': True,
        'issuer': issuer or '',
        'amount': amount,
        'amount_source': amount_source,  # 'keyword' = high confidence, 'fallback' = double-check this
        'facture_type': _guess_facture_type(text_lower),
        'issue_date': issue_dt.isoformat() if issue_dt else None,
        'due_date': due_dt.isoformat() if due_dt else None,
        'raw_text_preview': text.strip()[:500],
    }


def _prepare_image_for_ocr(img):
    img = ImageOps.exif_transpose(img)  # respect phone camera rotation
    img = img.convert('L')  # grayscale improves OCR accuracy
    if max(img.size) < 1500:
        ratio = 1500 / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)))
    return img


def extract_facture_fields_from_image(file_stream):
    """Run OCR on an uploaded invoice photo and best-effort guess the fields
    of the facture form. Returns a dict; never raises to the caller."""
    if not TESSERACT_AVAILABLE:
        return {
            'ok': False,
            'error': 'ocr_unavailable',
            'message': (
                'OCR ist auf diesem Server nicht installiert. Der Administrator muss '
                '"tesseract-ocr" auf dem Server installieren (siehe README).'
            ),
        }
    try:
        img = Image.open(file_stream)
        img = _prepare_image_for_ocr(img)
        text = pytesseract.image_to_string(img, lang='deu+eng+fra')
    except Exception:
        logger.exception('OCR failed while reading invoice photo')
        return {'ok': False, 'error': 'ocr_failed', 'message': 'Das Bild konnte nicht gelesen werden. Bitte ein scharferes Foto versuchen.'}

    return _extract_fields_from_text_and_image(text, img)


def extract_facture_fields_from_pdf(file_stream):
    """PDF version of the scanner. Two paths, tried in order of precision:
    1. Text layer: if the PDF was generated digitally (an emailed invoice,
       an exported bill, ...) it already contains exact, character-perfect
       text -- no OCR guessing needed at all, this is the most accurate path.
    2. Scanned fallback: if there's no text layer (a photographed/scanned
       PDF), each page is rendered to a high-res image and run through the
       same OCR pipeline as a photo.
    """
    if not PYMUPDF_AVAILABLE:
        return {
            'ok': False,
            'error': 'pdf_unavailable',
            'message': (
                'PDF-Unterstutzung ist auf diesem Server nicht installiert. Der Administrator muss '
                '"PyMuPDF" installieren (siehe requirements.txt).'
            ),
        }
    try:
        pdf_bytes = file_stream.read()
        doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    except Exception:
        logger.exception('Could not open uploaded PDF')
        return {'ok': False, 'error': 'pdf_failed', 'message': 'Die PDF-Datei konnte nicht gelesen werden.'}

    try:
        text_layer = '\n'.join(page.get_text('text') for page in doc)
        if text_layer.strip():
            # Exact text layer available -- highest-precision path, no OCR at all.
            result = _extract_fields_from_text_and_image(text_layer, img=None)
            result['extraction_method'] = 'pdf_text_layer'
            return result

        if not TESSERACT_AVAILABLE:
            return {
                'ok': False,
                'error': 'ocr_unavailable',
                'message': 'Diese PDF enthalt keinen Text (gescannt) und OCR ist nicht installiert.',
            }

        # Scanned PDF: render the first 2 pages at high resolution and OCR them.
        combined_text = []
        first_page_img = None
        for page in list(doc)[:2]:
            pix = page.get_pixmap(matrix=fitz.Matrix(3, 3))  # 3x ~= 300dpi, better OCR accuracy
            img = Image.open(io.BytesIO(pix.tobytes('png')))
            img = _prepare_image_for_ocr(img)
            if first_page_img is None:
                first_page_img = img
            combined_text.append(pytesseract.image_to_string(img, lang='deu+eng+fra'))

        result = _extract_fields_from_text_and_image('\n'.join(combined_text), first_page_img)
        result['extraction_method'] = 'pdf_ocr_scanned'
        return result
    except Exception:
        logger.exception('OCR failed while reading invoice PDF')
        return {'ok': False, 'error': 'ocr_failed', 'message': 'Die PDF-Datei konnte nicht verarbeitet werden. Bitte erneut versuchen.'}
    finally:
        doc.close()


@app.route('/factures/ocr', methods=['POST'])
@login_required('admin')
def factures_ocr():
    upload = request.files.get('image') or request.files.get('file')
    if not upload or not upload.filename:
        return jsonify({'ok': False, 'error': 'no_image', 'message': 'Kein Bild oder PDF erhalten.'}), 400

    filename = (upload.filename or '').lower()
    is_pdf = filename.endswith('.pdf') or (upload.mimetype or '') == 'application/pdf'
    result = extract_facture_fields_from_pdf(upload.stream) if is_pdf else extract_facture_fields_from_image(upload.stream)
    status = 200 if result.get('ok') else 422
    return jsonify(result), status



# ---------------------------------------------------------------------------
# Assistant — additional actions so the chat can do everything an admin can
# do from the regular pages (items, sellers, salary, cash register), not
# just debts/invoices/stock/cash-balance.
# ---------------------------------------------------------------------------

def _generate_item_barcode():
    for _ in range(20):
        candidate = str(random.randint(10**11, 10**12 - 1))
        if not fetch_one("SELECT 1 FROM products WHERE barcode = %s;", (candidate,)):
            return candidate
    return str(uuid.uuid4().int)[:12]


@app.route('/assistant/api/items', methods=['GET'])
@login_required(['admin', 'seller'])
def assistant_list_items():
    # search_items() is the single search implementation shared with the
    # Inventory page and the AI chat tool — matches barcode/SKU (exact or
    # partial) and product name (substring), case-insensitive.
    items = search_items(request.args.get('q') or '', limit=20)
    out = [{
        'id': i.get('barcode'),
        'product_name': i.get('product_name'),
        'barcode': i.get('barcode'),
        'sku': i.get('sku'),
        'quantity': int(i.get('quantity') or 0),
        'selling_price': float(i.get('selling_price') or 0),
        'purchase_price': float(i.get('purchase_price') or 0),
    } for i in items]
    return jsonify({'items': out})


@app.route('/assistant/api/items', methods=['POST'])
@login_required('admin')
def assistant_add_item():
    data = request.get_json(silent=True) or {}
    name = (data.get('product_name') or '').strip()
    if not name:
        return jsonify({'success': False, 'message': 'Der Produktname ist erforderlich.'}), 400
    try:
        quantity = int(data.get('quantity') or 0)
        purchase_price = float(data.get('purchase_price') or 0)
        selling_price = float(data.get('selling_price') or 0)
        min_selling_price = float(data.get('min_selling_price') or selling_price)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Preise/Menge müssen Zahlen sein.'}), 400

    # Reuses _add_item_record (same helper backing the chat assistant's
    # add_item tool) so barcode auto-generation, barcode uniqueness, and
    # SKU uniqueness are all enforced in exactly one place.
    try:
        barcode_value = _add_item_record(
            name, quantity, purchase_price, selling_price, min_selling_price,
            data.get('barcode'), data.get('sku'), data.get('description') or '',
        )
    except ValueError as e:
        message = str(e)
        if 'barcode' in message:
            message = 'Dieser Barcode existiert bereits.'
        elif 'SKU' in message:
            message = 'Diese SKU existiert bereits.'
        return jsonify({'success': False, 'message': message}), 400
    except Exception:
        logger.exception('Assistant failed to add item')
        return jsonify({'success': False, 'message': 'Datenbankfehler beim Hinzufügen des Artikels.'}), 500

    return jsonify({'success': True, 'message': f'Artikel "{name}" wurde mit Barcode {barcode_value} hinzugefügt.', 'barcode': barcode_value})


@app.route('/assistant/api/items/<path:identifier>', methods=['PUT'])
@login_required('admin')
def assistant_update_item(identifier):
    data = request.get_json(silent=True) or {}
    try:
        result = _update_item_record(
            identifier,
            product_name=data.get('product_name'),
            quantity=data.get('quantity'),
            purchase_price=data.get('purchase_price'),
            selling_price=data.get('selling_price'),
            min_selling_price=data.get('min_selling_price'),
            barcode_value=data.get('barcode'),
            sku=data.get('sku'),
            description=data.get('description'),
        )
        log_audit('product_updated', 'item', result['barcode'],
                   format_product_details(result.get('product_name'), result.get('barcode'), result.get('sku')),
                   actor=session.get('username'), module='inventory')
    except LookupError as e:
        return jsonify({'success': False, 'message': str(e)}), 404
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except Exception:
        logger.exception('Assistant failed to update item %s', identifier)
        return jsonify({'success': False, 'message': 'Datenbankfehler beim Aktualisieren des Artikels.'}), 500

    return jsonify({'success': True, 'message': f"Artikel \"{result['product_name']}\" wurde aktualisiert.", 'item': result})


@app.route('/assistant/api/items/<path:identifier>', methods=['DELETE'])
@login_required('admin')
def assistant_delete_item(identifier):
    try:
        result = _delete_item_record(identifier)
        log_audit('product_deleted', 'item', result['barcode'],
                   format_product_details(result.get('product_name'), result.get('barcode')),
                   actor=session.get('username'), module='inventory')
    except LookupError as e:
        return jsonify({'success': False, 'message': str(e)}), 404
    except Exception:
        logger.exception('Assistant failed to delete item %s', identifier)
        return jsonify({'success': False, 'message': 'Fehler beim Loeschen des Artikels.'}), 500

    return jsonify({'success': True, 'message': f"Artikel \"{result.get('product_name') or result['barcode']}\" wurde geloescht."})


@app.route('/assistant/api/sellers', methods=['GET'])
@login_required('admin')
def assistant_list_sellers():
    sellers = [s for s in load_users() if s.get('role') == 'seller']
    out = [{
        'username': s['username'],
        'salary': float(s.get('salary') or 0),
        'activated': bool(s.get('activated')),
    } for s in sellers]
    return jsonify({'sellers': out})


@app.route('/assistant/api/sellers', methods=['POST'])
@login_required('admin')
def assistant_add_seller():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    if not username or not password:
        return jsonify({'success': False, 'message': 'Benutzername und Passwort sind erforderlich.'}), 400
    if find_user(username):
        return jsonify({'success': False, 'message': 'Dieser Benutzername existiert bereits.'}), 400
    try:
        salary = float(data.get('salary') or 0)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Das Gehalt muss eine Zahl sein.'}), 400

    try:
        insert_user({
            'username': username,
            'password': generate_password_hash(password),
            'role': 'seller',
            'profile_img': data.get('profile_img') or '',
            'salary': salary,
            'activated': bool(data.get('activated', True)),
        })
    except Exception:
        logger.exception('Assistant failed to add seller')
        return jsonify({'success': False, 'message': 'Datenbankfehler beim Hinzufügen des Verkäufers.'}), 500

    return jsonify({'success': True, 'message': f'Verkäufer "{username}" wurde hinzugefügt.'})


@app.route('/assistant/api/sellers/<username>', methods=['PUT'])
@login_required('admin')
def assistant_update_seller(username):
    seller = find_user(username)
    if not seller or seller.get('role') != 'seller':
        return jsonify({'success': False, 'message': f'Kein Verkaeufer mit dem Benutzernamen "{username}" gefunden.'}), 404

    data = request.get_json(silent=True) or {}
    try:
        salary = float(data.get('salary')) if data.get('salary') not in (None, '') else float(seller.get('salary') or 0)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Das Gehalt muss eine Zahl sein.'}), 400
    activated = bool(data.get('activated')) if data.get('activated') is not None else bool(seller.get('activated'))
    profile_img = data.get('profile_img') if data.get('profile_img') is not None else seller.get('profile_img', '')

    try:
        update_user(username, {'profile_img': profile_img, 'salary': salary, 'activated': activated})
        log_audit('seller_updated', 'seller', username,
                   format_seller_details(username, salary),
                   actor=session.get('username'), module='sellers')
    except Exception:
        logger.exception('Assistant failed to update seller %s', username)
        return jsonify({'success': False, 'message': 'Datenbankfehler beim Aktualisieren des Verkaeufers.'}), 500

    return jsonify({'success': True, 'message': f'Verkaeufer "{username}" wurde aktualisiert.',
                     'seller': {'username': username, 'salary': salary, 'activated': activated}})


@app.route('/assistant/api/sellers/<username>', methods=['DELETE'])
@login_required('admin')
def assistant_delete_seller(username):
    seller = find_user(username)
    if not seller or seller.get('role') != 'seller':
        return jsonify({'success': False, 'message': f'Kein Verkaeufer mit dem Benutzernamen "{username}" gefunden.'}), 404

    try:
        delete_user(username)
        log_audit('seller_deleted', 'seller', username, username, actor=session.get('username'), module='sellers')
    except Exception:
        logger.exception('Assistant failed to delete seller %s', username)
        return jsonify({'success': False, 'message': 'Fehler beim Loeschen des Verkaeufers.'}), 500

    return jsonify({'success': True, 'message': f'Verkaeufer "{username}" wurde geloescht.'})


@app.route('/assistant/api/salary', methods=['GET'])
@login_required('admin')
def assistant_list_salary():
    rows = fetch_all("SELECT employee, amount, source, payment_date FROM salaries ORDER BY payment_date DESC LIMIT 10;")
    out = [{
        'employee': r['employee'],
        'amount': float(r['amount']),
        'source': r.get('source'),
        'payment_date': r['payment_date'].strftime('%d.%m.%Y') if r.get('payment_date') else None,
    } for r in rows]
    return jsonify({'payments': out})


@app.route('/assistant/api/salary', methods=['POST'])
@login_required('admin')
def assistant_pay_salary():
    data = request.get_json(silent=True) or {}
    employee = (data.get('employee') or '').strip()
    try:
        amount = float(data.get('amount'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Bitte Mitarbeiter und einen gültigen Betrag angeben.'}), 400
    if not employee:
        return jsonify({'success': False, 'message': 'Bitte Mitarbeiter und einen gültigen Betrag angeben.'}), 400

    try:
        insert_salary_payment({
            'employee': employee,
            'amount': amount,
            'source': data.get('source') or 'kasse',
            'note': data.get('note') or '',
            'payment_date': datetime.now(),
        })
    except Exception:
        logger.exception('Assistant failed to pay salary')
        return jsonify({'success': False, 'message': 'Datenbankfehler bei der Gehaltszahlung.'}), 500

    return jsonify({'success': True, 'message': f'€{amount:,.2f} wurden an {employee} bezahlt.'})


@app.route('/assistant/api/kasse', methods=['POST'])
@login_required(['admin', 'seller'])
def assistant_add_kasse_transaction():
    if not seller_kasse_enabled():
        return jsonify({'success': False, 'message': 'Kasse-Zugriff (Ein-/Auszahlung) wurde für Ihr Konto deaktiviert.'}), 403
    data = request.get_json(silent=True) or {}
    typ = data.get('type')
    if typ not in ('einzahlung', 'auszahlung'):
        return jsonify({'success': False, 'message': "Typ muss 'einzahlung' oder 'auszahlung' sein."}), 400
    try:
        amount = float(data.get('amount'))
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Der Betrag muss eine positive Zahl sein.'}), 400

    description = (data.get('description') or '').replace('%', '%%')
    username = session.get('username', 'assistant').replace('%', '%%')
    payment_method = (data.get('payment_method') or 'cash').strip().lower()
    if payment_method not in PAYMENT_METHODS:
        payment_method = 'cash'
    try:
        execute_query(
            "INSERT INTO cash_transactions (date, amount, type, description, username, payment_method) VALUES (%s, %s, %s, %s, %s, %s)",
            (datetime.now(), amount, typ, description, username, payment_method),
        )
    except Exception:
        logger.exception('Assistant failed to add kasse transaction')
        return jsonify({'success': False, 'message': 'Datenbankfehler bei der Kassentransaktion.'}), 500

    return jsonify({'success': True, 'message': f'{"Einzahlung" if typ == "einzahlung" else "Auszahlung"} über €{amount:,.2f} wurde gespeichert.'})


@app.route('/assistant/api/orders/recent')
@login_required(['admin', 'seller'])
def assistant_orders_recent():
    q = (request.args.get('q') or '').strip().lower()
    orders = get_orders(role='admin')
    if q:
        # Free-text search across ALL orders (not just the latest 10) by
        # barcode/order number or product name — order_number IS the
        # product's barcode (see add_order in db.py), so this doubles as a
        # barcode scan lookup.
        orders = [o for o in orders if q in str(o['order_number']).lower() or q in (o['product_name'] or '').lower()][:20]
    else:
        orders = orders[:10]
    out = [{
        'order_number': o['order_number'],
        'product_name': o['product_name'],
        'quantity': o['quantity'],
        'price': o['price'],
        'total_price': o['total_price'],
        'date': o['date'],
        'user': o['user'],
    } for o in orders]
    return jsonify({'orders': out})


@app.route('/assistant/api/orders/<order_number>/delete', methods=['POST'])
@login_required('admin')
def assistant_delete_order(order_number):
    """Permanently delete a purchase order from the button-driven chat's
    recent-orders list — the same delete_order() the AI's delete_order tool
    and the classic web page use, so nothing here duplicates logic."""
    existing = next((o for o in get_orders(role='admin') if str(o['order_number']) == order_number), None)
    if not existing:
        return jsonify({'success': False, 'message': f'No purchase order found with number "{order_number}".'}), 404
    try:
        delete_order(order_number)
    except Exception:
        logger.exception('Assistant failed to delete purchase order %s', order_number)
        return jsonify({'success': False, 'message': 'Datenbankfehler beim Löschen der Bestellung.'}), 500
    log_audit('order_deleted', 'order', order_number,
               f'Purchase order #{order_number} ({existing["product_name"]}) deleted via chat',
               actor=session.get('username'), module='orders')
    return jsonify({'success': True})


@app.route('/assistant/api/orders', methods=['POST'])
@login_required('admin')
def assistant_add_order():
    """Record a new purchase order (Einkauf) straight from the chat."""
    data = request.get_json(silent=True) or {}
    name = (data.get('product_name') or '').strip()
    if not name:
        return jsonify({'success': False, 'message': 'Der Produktname ist erforderlich.'}), 400
    try:
        quantity = int(data.get('quantity') or 0)
        price = float(data.get('price') or 0)
        selling_price = float(data.get('selling_price') or 0)
        min_selling_price = float(data.get('min_selling_price') or selling_price)
        if quantity <= 0 or price < 0:
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Preise/Menge müssen gültige Zahlen sein.'}), 400

    total_price = round(price * quantity, 2)

    payment_method = (data.get('payment_method') or 'cash').strip().lower()
    if payment_method not in PAYMENT_METHODS:
        payment_method = 'cash'

    # A "card" order from the chat assistant must go through the same
    # physical Stripe Terminal reader as the manual purchase-order form —
    # never just trust the chat's word that a card payment "went through".
    stripe_payment_intent_id = (data.get('stripe_payment_intent_id') or '').strip() or None
    if payment_method == 'card':
        try:
            verify_stripe_terminal_payment(stripe_payment_intent_id, total_price)
        except Exception as e:
            return jsonify({'success': False, 'message': f'Kartenzahlung konnte nicht bestätigt werden: {e}'}), 400

    order = {
        'product_name': name,
        'ref_number': (data.get('ref_number') or '').strip(),
        'description': data.get('description') or '',
        'price': price,
        'selling_price': selling_price,
        'min_selling_price': min_selling_price,
        'quantity': quantity,
        'total_price': total_price,
        'date': datetime.now().strftime('%Y-%m-%d'),
        'user': session.get('username'),
        'payment_method': payment_method,
        'stripe_payment_intent_id': stripe_payment_intent_id,
    }
    try:
        order_number = add_order(order)
    except Exception:
        logger.exception('Assistant failed to add purchase order')
        return jsonify({'success': False, 'message': 'Datenbankfehler beim Speichern der Bestellung.'}), 500

    return jsonify({
        'success': True,
        'message': f'✅ Bestellung "{name}" ({quantity} Stück, {total_price:,.2f} €) wurde gespeichert.',
        'product_name': name,
        'quantity': quantity,
        'total_price': total_price,
        'order_number': order_number,
    })


@app.route('/assistant/api/sales/recent')
@login_required(['admin', 'seller'])
def assistant_sales_recent():
    q = (request.args.get('q') or '').strip().lower()
    sales = load_sales()
    if q:
        # Search by the sale's own id or by a barcode/product name among its
        # line items — lets a barcode scan find the sale it was part of.
        sales = [
            s for s in sales
            if q in str(s.get('order_id') or '').lower()
            or any(q in str(i.get('barcode') or '').lower() or q in (i.get('product_name') or '').lower()
                   for i in (s.get('items') or []))
        ][:20]
    else:
        sales = sales[:10]
    out = []
    for s in sales:
        raw_date = s.get('date')
        date_str = raw_date.strftime('%d.%m.%Y %H:%M') if hasattr(raw_date, 'strftime') else (str(raw_date) if raw_date else None)
        out.append({
            'order_id': s.get('order_id'),
            'user': s.get('user'),
            'date': date_str,
            'total': float(s.get('total_order_price') or 0),
            'items_count': len(s.get('items') or []),
            'items': [{
                'sale_item_id': i.get('id'),
                'product_name': i.get('product_name'),
                'barcode': i.get('barcode'),
                'quantity': i.get('quantity'),
                'sale_price': float(i.get('sale_price') or 0),
            } for i in (s.get('items') or [])],
        })
    return jsonify({'sales': out})


@app.route('/assistant/api/sales/<order_id>/delete', methods=['POST'])
@login_required('admin')
def assistant_sales_delete(order_id):
    ok, message = delete_sales_order(order_id)
    if ok:
        log_audit('assistant_sale', 'sale', order_id, message, actor=session.get('username'), source='assistant', module='sales')
        return jsonify({'success': True, 'message': message})
    return jsonify({'success': False, 'message': message}), 400


@app.route('/assistant/api/sales/<order_id>/item/<barcode_value>/edit-url')
@login_required('admin')
def assistant_sale_item_edit_url(order_id, barcode_value):
    """Resolves (sale order_id, barcode) -> the real sale_items.id and
    returns the existing edit page's URL. The chat UI only ever passes the
    barcode it already showed the user — never the internal row id — so
    barcode stays the single reference key across Items, Orders, and Sales."""
    row = fetch_one(
        "SELECT id FROM sale_items WHERE sale_id = %s AND barcode = %s LIMIT 1;",
        (order_id, barcode_value)
    )
    if not row:
        return jsonify({'success': False, 'message': f'No sale item with barcode "{barcode_value}" found in sale #{order_id}.'}), 404
    return jsonify({'success': True, 'edit_url': url_for('edit_sale', sale_item_id=row['id'])})


@app.route('/assistant/api/sales/<order_id>/item/<barcode_value>', methods=['DELETE'])
@login_required('admin')
def assistant_sale_item_delete(order_id, barcode_value):
    """Deletes a single line item from a sale by (order_id, barcode) instead
    of requiring the caller to know the internal sale_items.id. If it was
    the sale's only item, the whole (now-empty) sale is gone too."""
    row = fetch_one(
        "SELECT id, product_name FROM sale_items WHERE sale_id = %s AND barcode = %s LIMIT 1;",
        (order_id, barcode_value)
    )
    if not row:
        return jsonify({'success': False, 'message': f'No sale item with barcode "{barcode_value}" found in sale #{order_id}.'}), 404
    try:
        execute_query("DELETE FROM sale_items WHERE id = %s;", (row['id'],))
    except Exception:
        logger.exception('Assistant failed to delete sale item %s (barcode %s, sale %s)', row['id'], barcode_value, order_id)
        return jsonify({'success': False, 'message': 'Datenbankfehler beim Löschen der Verkaufsposition.'}), 500
    log_audit('assistant_sale_item_deleted', 'sale_item', str(row['id']),
              f'Sale item "{row["product_name"]}" (barcode {barcode_value}) deleted from sale #{order_id} via chat',
              actor=session.get('username'), source='assistant', module='sales')
    return jsonify({'success': True})


@app.route('/assistant/api/items/<barcode_value>/barcode')
@login_required('admin')
def assistant_item_barcode(barcode_value):
    """Returns the printable barcode image URL for an item, so the chat
    assistant can offer a 'print barcode' action after a sale or lookup
    instead of only being reachable from the admin items page."""
    item = find_item_by_barcode(barcode_value)
    if not item:
        return jsonify({'success': False, 'message': 'Item not found'}), 404
    return jsonify({
        'success': True,
        'barcode': barcode_value,
        'product_name': item.get('product_name'),
        'image_url': url_for('barcode_print', barcode_value=barcode_value),
        'print_url': url_for('barcode_print', barcode_value=barcode_value),
    })


@app.route('/assistant/api/sell', methods=['POST'])
@login_required(['admin', 'seller'])
def assistant_quick_sell():
    """Sell one product line from the chat: by barcode or (partial) product name."""
    data = request.get_json(silent=True) or {}
    identifier = (data.get('identifier') or '').strip()
    customer_name = (data.get('customer_name') or '').strip() or None
    payment_method = (data.get('payment_method') or '').strip().lower() or None
    if payment_method not in (None, 'cash', 'card'):
        payment_method = None
    try:
        quantity = int(data.get('quantity') or 1)
        if quantity <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Die Menge muss eine positive Zahl sein.'}), 400
    if not identifier:
        return jsonify({'success': False, 'message': 'Bitte Produktname oder Barcode angeben.'}), 400

    items = load_items()
    item = next((i for i in items if i.get('barcode') == identifier), None)
    if not item:
        identifier_lower = identifier.lower()
        item = next((i for i in items if identifier_lower in (i.get('product_name') or '').lower()), None)
    if not item:
        return jsonify({'success': False, 'message': f'Kein Artikel für "{identifier}" gefunden.'}), 404

    if quantity > int(item.get('quantity') or 0):
        return jsonify({
            'success': False,
            'message': f'Nicht genug Bestand für {item.get("product_name")} (verfügbar: {item.get("quantity")}).',
        }), 400

    sale_price = float(item.get('selling_price') or 0)
    if sale_price <= 0:
        return jsonify({'success': False, 'message': f'Ungültiger Verkaufspreis für {item.get("product_name")}.'}), 400

    total_price = round(sale_price * quantity, 2)

    # A "card" quick sale from the chat assistant must be captured by the
    # same physical Stripe Terminal (TPE) reader as a manual card sale —
    # never just trust the chat's word that a card payment "went through".
    stripe_payment_intent_id = (data.get('stripe_payment_intent_id') or '').strip() or None
    if payment_method == 'card':
        try:
            verify_stripe_terminal_payment(stripe_payment_intent_id, total_price)
        except Exception as e:
            return jsonify({'success': False, 'message': f'Kartenzahlung konnte nicht bestätigt werden: {e}'}), 400

    purchase_price = float(item.get('purchase_price') or 0)
    profit = round(total_price - purchase_price * quantity, 2)
    sale_id = str(uuid.uuid4())
    sale_date = datetime.now()
    new_qty = int(item.get('quantity') or 0) - quantity

    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sales (sale_id, username, sale_date, total_sale_price, customer_name, payment_method, stripe_payment_intent_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (sale_id, session['username'], sale_date, total_price, customer_name, payment_method, stripe_payment_intent_id),
        )
        cur.execute(
            """INSERT INTO sale_items
               (sale_id, barcode, product_name, quantity, sale_price, total_price, purchase_price, profit)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (sale_id, item.get('barcode'), item.get('product_name'), quantity, sale_price, total_price, purchase_price, profit),
        )
        cur.execute("UPDATE products SET quantity = %s WHERE barcode = %s", (new_qty, item.get('barcode')))
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        logger.exception('Assistant failed to record quick sale')
        return jsonify({'success': False, 'message': 'Datenbankfehler beim Speichern des Verkaufs.'}), 500
    finally:
        if conn:
            conn.close()

    log_audit('assistant_sale', 'sale', sale_id,
               format_sale_details(item.get('product_name'), quantity, item.get('barcode'), total_price, payment_method, customer_name),
               actor=session.get('username'), source='assistant', module='sales')

    return jsonify({
        'success': True,
        'message': f'{quantity} × {item.get("product_name")} verkauft für insgesamt €{total_price:,.2f}.',
        'product_name': item.get('product_name'),
        'barcode': item.get('barcode'),
        'quantity': quantity,
        'total_price': total_price,
        'profit': profit,
        'remaining': new_qty,
        'low_stock': new_qty <= 5,
        'customer_name': customer_name,
        'payment_method': payment_method,
    })


def _find_open_debt(name_or_id):
    """Look up an open debt by exact debt_id or reference_number, or the
    most recent open debt whose client name contains the given text
    (case-insensitive)."""
    if not name_or_id:
        return None
    row = fetch_one(
        "SELECT debt_id, client_name, amount FROM debts WHERE (debt_id = %s OR reference_number = %s) AND paid = FALSE;",
        (name_or_id, name_or_id),
    )
    if row:
        return row
    return fetch_one(
        "SELECT debt_id, client_name, amount FROM debts WHERE paid = FALSE AND LOWER(client_name) LIKE %s ORDER BY created_at DESC LIMIT 1;",
        (f"%{name_or_id.lower()}%",),
    )


def _find_any_debt(name_or_id):
    """Same as _find_open_debt but also matches already-paid debts — used
    by delete_debt, since deleting (unlike marking-as-paid) should be able
    to target a debt regardless of its paid status."""
    if not name_or_id:
        return None
    row = fetch_one(
        "SELECT debt_id, client_name, amount, paid FROM debts WHERE debt_id = %s OR reference_number = %s;",
        (name_or_id, name_or_id),
    )
    if row:
        return row
    return fetch_one(
        "SELECT debt_id, client_name, amount, paid FROM debts WHERE LOWER(client_name) LIKE %s ORDER BY created_at DESC LIMIT 1;",
        (f"%{name_or_id.lower()}%",),
    )


def _add_debt_record(client_name, amount, phone_number=None, description=None):
    client_name = (client_name or '').strip()
    if not client_name or amount is None:
        raise ValueError('client_name and amount are required')
    amount = float(amount)
    debt_id = str(uuid.uuid4())[:8].upper()
    reference_number = generate_debt_reference_number()
    execute_query(
        "INSERT INTO debts (debt_id, reference_number, client_name, description, amount, original_amount, phone_number) VALUES (%s, %s, %s, %s, %s, %s, %s);",
        (debt_id, reference_number, client_name, (description or '').strip() or None, amount, amount, (phone_number or '').strip() or None),
    )
    return debt_id


def _find_unpaid_facture(issuer_or_id):
    if not issuer_or_id:
        return None
    try:
        f = get_facture(int(issuer_or_id))
        if f and f.get('status') != 'paid':
            return f
    except (TypeError, ValueError):
        pass
    matches = [f for f in get_factures(status='unpaid') if issuer_or_id.lower() in (f.get('issuer') or '').lower()]
    return matches[0] if matches else None


def _find_facture(issuer_or_id):
    """Like _find_unpaid_facture but matches any status — for edit/delete,
    which should work on paid invoices too, not just unpaid ones."""
    if not issuer_or_id:
        return None
    try:
        f = get_facture(int(issuer_or_id))
        if f:
            return f
    except (TypeError, ValueError):
        pass
    matches = [f for f in get_factures() if issuer_or_id.lower() in (f.get('issuer') or '').lower()]
    return matches[0] if matches else None


def _add_facture_record(facture_type, issuer, amount, currency='EUR', issue_date=None, due_date=None, notes=None, created_by=None):
    issuer = (issuer or '').strip()
    if not issuer:
        raise ValueError('issuer is required')
    payload = {
        'facture_type': facture_type or 'other',
        'issuer': issuer,
        'amount': float(amount),
        'currency': currency or 'EUR',
        'issue_date': issue_date or datetime.now().date().isoformat(),
        'due_date': due_date or None,
        'status': 'unpaid',
        'notes': notes or None,
        'created_by': created_by,
    }
    add_facture(payload)
    return payload


def _add_item_record(product_name, quantity=0, purchase_price=0, selling_price=0, min_selling_price=None,
                      barcode_value=None, sku=None, description='', item_condition='neu'):
    name = (product_name or '').strip()
    if not name:
        raise ValueError('product_name is required')
    min_selling_price = float(min_selling_price) if min_selling_price not in (None, '') else float(selling_price or 0)
    barcode_value = (barcode_value or '').strip() or _generate_item_barcode()
    if fetch_one("SELECT 1 FROM products WHERE barcode = %s;", (barcode_value,)):
        raise ValueError('a product with this barcode already exists')
    sku = (sku or '').strip() or None
    if sku and fetch_one("SELECT 1 FROM products WHERE sku = %s;", (sku,)):
        raise ValueError('a product with this SKU already exists')
    condition = (item_condition or 'neu').strip().lower()
    if condition not in ('neu', 'gebraucht', 'defekt'):
        condition = 'neu'
    execute_query(
        """INSERT INTO products
           (product_name, description, quantity, barcode, sku, purchase_price, selling_price, min_selling_price, date_added, item_condition)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (name, description or '', int(quantity or 0), barcode_value, sku,
         float(purchase_price or 0), float(selling_price or 0), min_selling_price, datetime.now().strftime('%Y-%m-%d'), condition),
    )
    return barcode_value


def _find_item_by_identifier(identifier):
    """Resolve a product for the chat assistant's update_item/delete_item
    tools by, in order: exact barcode (the product's primary key), exact
    SKU, or the most recently added product whose name contains the given
    text (case-insensitive) — same "exact code, then fuzzy name" lookup
    style as _find_open_debt / _find_unpaid_facture above."""
    identifier = (identifier or '').strip()
    if not identifier:
        raise ValueError('identifier is required')
    row = fetch_one("SELECT * FROM products WHERE barcode = %s;", (identifier,))
    if row:
        return row
    row = fetch_one("SELECT * FROM products WHERE sku = %s;", (identifier,))
    if row:
        return row
    row = fetch_one(
        "SELECT * FROM products WHERE LOWER(product_name) LIKE %s ORDER BY date_added DESC LIMIT 1;",
        (f"%{identifier.lower()}%",),
    )
    if not row:
        raise LookupError(f'no product found for "{identifier}"')
    return row


def _update_item_record(identifier, product_name=None, quantity=None, purchase_price=None,
                         selling_price=None, min_selling_price=None, barcode_value=None,
                         sku=None, description=None, item_condition=None):
    """Update only the fields actually provided; everything else keeps its
    current value. Mirrors the validation done in the /edit_item web route
    (required name/barcode, non-negative numbers, unique barcode/SKU) so the
    chat assistant can't create data the admin UI would reject."""
    item = _find_item_by_identifier(identifier)
    old_barcode = item['barcode']

    new_name = (product_name if product_name is not None else item.get('product_name')) or ''
    new_name = new_name.strip()
    if not new_name:
        raise ValueError('product_name cannot be empty')

    new_barcode = (barcode_value if barcode_value is not None else item.get('barcode')) or ''
    new_barcode = new_barcode.strip()
    if not new_barcode:
        raise ValueError('barcode cannot be empty')
    clash = fetch_one("SELECT barcode FROM products WHERE barcode = %s AND barcode != %s;", (new_barcode, old_barcode))
    if clash:
        raise ValueError('this barcode is already used by another product')

    # SKU is optional — unlike barcode, it's fine for it to stay empty —
    # but if a non-empty value is set (new or existing) it must be unique.
    new_sku = (sku if sku is not None else item.get('sku')) or ''
    new_sku = new_sku.strip()
    if new_sku:
        sku_clash = fetch_one("SELECT barcode FROM products WHERE sku = %s AND barcode != %s;", (new_sku, old_barcode))
        if sku_clash:
            raise ValueError('this SKU is already used by another product')

    def _num(new_value, current_value, label, cast, allow_negative=False):
        if new_value is None:
            return cast(current_value or 0)
        try:
            value = cast(new_value)
        except (TypeError, ValueError):
            raise ValueError(f'{label} must be a number')
        if not allow_negative and value < 0:
            raise ValueError(f'{label} cannot be negative')
        return value

    new_quantity = _num(quantity, item.get('quantity'), 'quantity', int)
    new_purchase_price = _num(purchase_price, item.get('purchase_price'), 'purchase_price', float)
    new_selling_price = _num(selling_price, item.get('selling_price'), 'selling_price', float)
    if min_selling_price is not None:
        new_min_selling_price = _num(min_selling_price, None, 'min_selling_price', float)
    elif item.get('min_selling_price') not in (None, ''):
        new_min_selling_price = _num(None, item.get('min_selling_price'), 'min_selling_price', float)
    else:
        new_min_selling_price = new_selling_price

    new_condition = (item_condition if item_condition is not None else item.get('item_condition')) or 'neu'
    new_condition = new_condition.strip().lower()
    if new_condition not in ('neu', 'gebraucht', 'defekt'):
        new_condition = 'neu'

    updates = {
        'product_name': new_name,
        'barcode': new_barcode,
        'sku': new_sku or None,
        'purchase_price': new_purchase_price,
        'selling_price': new_selling_price,
        'min_selling_price': new_min_selling_price,
        'quantity': new_quantity,
        'description': description if description is not None else item.get('description') or '',
        'photo_link': item.get('photo_link') or '',
        'item_condition': new_condition,
    }
    update_item(old_barcode, updates)
    return {'barcode': new_barcode, 'product_name': new_name, 'sku': new_sku}


def _delete_item_record(identifier):
    item = _find_item_by_identifier(identifier)
    db_delete_item(item['barcode'])
    return {'barcode': item['barcode'], 'product_name': item.get('product_name')}


def _add_seller_record(username, password, salary=0, activated=True, profile_img=''):
    username = (username or '').strip()
    if not username or not password:
        raise ValueError('username and password are required')
    if find_user(username):
        raise ValueError('this username already exists')
    insert_user({
        'username': username,
        'password': generate_password_hash(password),
        'role': 'seller',
        'profile_img': profile_img or '',
        'salary': float(salary or 0),
        'activated': bool(activated),
    })


def _pay_salary_record(employee, amount, source='kasse', note=''):
    employee = (employee or '').strip()
    if not employee or amount is None:
        raise ValueError('employee and amount are required')
    insert_salary_payment({
        'employee': employee,
        'amount': float(amount),
        'source': source or 'kasse',
        'note': note or '',
        'payment_date': datetime.now(),
    })


def _add_kasse_record(typ, amount, description='', username='assistant', payment_method='cash'):
    if typ not in ('einzahlung', 'auszahlung'):
        raise ValueError("type must be 'einzahlung' (deposit) or 'auszahlung' (withdrawal)")
    amount = float(amount)
    if amount <= 0:
        raise ValueError('amount must be a positive number')
    payment_method = (payment_method or 'cash').strip().lower()
    if payment_method not in PAYMENT_METHODS:
        raise ValueError(f"payment_method must be one of {PAYMENT_METHODS}")
    execute_query(
        "INSERT INTO cash_transactions (date, amount, type, description, username, payment_method) VALUES (%s, %s, %s, %s, %s, %s)",
        (datetime.now(), amount, typ, (description or '').replace('%', '%%'), (username or 'assistant').replace('%', '%%'), payment_method),
    )


def _add_order_record(product_name, quantity, price, selling_price=0, min_selling_price=None, ref_number='', description='', username=None, payment_method=None):
    name = (product_name or '').strip()
    if not name:
        raise ValueError('product_name is required')
    quantity = int(quantity)
    price = float(price)
    if quantity <= 0 or price < 0:
        raise ValueError('quantity must be positive and price must not be negative')
    selling_price = float(selling_price or 0)
    min_selling_price = float(min_selling_price) if min_selling_price not in (None, '') else selling_price
    payment_method = (payment_method or 'cash').strip().lower()
    if payment_method not in PAYMENT_METHODS:
        payment_method = 'cash'
    if payment_method == 'card':
        # This tool is driven by free-text chat parsing — there is no
        # physical Stripe Terminal reader in the loop here, so a "card"
        # payment can never actually be verified. Same rule as sales/debt
        # payments: never record a card payment as paid without Stripe
        # having actually confirmed it. Point to the form that can.
        raise ValueError(
            "Kartenzahlungen können nicht aus dem Chat heraus bestätigt werden. "
            "Bitte die Bestellung über das Formular \u201eNeue Bestellung\u201c aufgeben — "
            "dort wird die Kartenzahlung über das Kartenlesegerät erfasst."
        )
    total_price = round(price * quantity, 2)
    order = {
        'product_name': name, 'ref_number': (ref_number or '').strip(), 'description': description or '',
        'price': price, 'selling_price': selling_price, 'min_selling_price': min_selling_price,
        'quantity': quantity, 'total_price': total_price,
        'date': datetime.now().strftime('%Y-%m-%d'), 'user': username,
        'payment_method': payment_method,
    }
    order_number = add_order(order)
    order['order_number'] = order_number
    return order


def _quick_sell_record(identifier, quantity=1, username='assistant', customer_name=None, payment_method=None, sale_price=None):
    identifier = (identifier or '').strip()
    quantity = int(quantity or 1)
    customer_name = (customer_name or '').strip() or None
    payment_method = (payment_method or '').strip().lower() or None
    if payment_method not in (None, 'cash', 'card'):
        payment_method = None
    if not identifier or quantity <= 0:
        raise ValueError('identifier and a positive quantity are required')
    items = load_items()
    item = next((i for i in items if i.get('barcode') == identifier), None)
    if not item:
        identifier_lower = identifier.lower()
        item = next((i for i in items if identifier_lower in (i.get('product_name') or '').lower()), None)
    if not item:
        raise LookupError(f'no item found for "{identifier}"')
    if quantity > int(item.get('quantity') or 0):
        raise ValueError(f'not enough stock for {item.get("product_name")} (available: {item.get("quantity")})')
    catalog_price = float(item.get('selling_price') or 0)
    # sale_price lets a sale go out at something other than the catalog
    # price (a discount, a haggled price, a round number for a regular)
    # without having to edit the product itself. Falls back to the
    # catalog price when not given, exactly like before.
    if sale_price is not None:
        sale_price = float(sale_price)
        if sale_price <= 0:
            raise ValueError('sale_price must be greater than 0')
    else:
        sale_price = catalog_price
        if sale_price <= 0:
            raise ValueError(f'invalid selling price for {item.get("product_name")}')
    total_price = round(sale_price * quantity, 2)
    purchase_price = float(item.get('purchase_price') or 0)
    profit = round(total_price - purchase_price * quantity, 2)
    sale_id = str(uuid.uuid4())
    new_qty = int(item.get('quantity') or 0) - quantity
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sales (sale_id, username, sale_date, total_sale_price, customer_name, payment_method) VALUES (%s, %s, %s, %s, %s, %s)",
            (sale_id, username, datetime.now(), total_price, customer_name, payment_method),
        )
        cur.execute(
            """INSERT INTO sale_items
               (sale_id, barcode, product_name, quantity, sale_price, total_price, purchase_price, profit)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (sale_id, item.get('barcode'), item.get('product_name'), quantity, sale_price, total_price, purchase_price, profit),
        )
        cur.execute("UPDATE products SET quantity = %s WHERE barcode = %s", (new_qty, item.get('barcode')))
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()
    return {
        'product_name': item.get('product_name'), 'quantity': quantity, 'total_price': total_price,
        'sale_price': sale_price, 'catalog_price': catalog_price,
        'profit': profit, 'remaining': new_qty, 'low_stock': new_qty <= 5,
        'customer_name': customer_name, 'payment_method': payment_method,
    }


ASSISTANT_TOOLS = [
    {"name": "get_summary", "description": "Today's overall business snapshot: sales, profit, cash balance, open debts, unpaid invoices, low-stock count.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_open_debts", "description": "List the most recent open (unpaid) debts.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "add_debt", "description": "Record a new debt owed by a client.",
     "input_schema": {"type": "object", "properties": {
         "client_name": {"type": "string"}, "amount": {"type": "number"},
         "phone_number": {"type": "string"}, "description": {"type": "string"}},
         "required": ["client_name", "amount"]}},
    {"name": "pay_debt", "description": (
        "Mark a client's open debt as fully paid — settles the ENTIRE remaining balance in one payment. "
        "Use record_debt_payment instead if the client is only paying part of what they owe. "
        "Give the client's name, or a debt id if known."),
     "input_schema": {"type": "object", "properties": {
         "name_or_id": {"type": "string"},
         "payment_method": {"type": "string", "enum": ["cash", "card"], "description": "defaults to cash if not specified"}},
         "required": ["name_or_id"]}},
    {"name": "record_debt_payment", "description": (
        "Record a partial (or full) cash/card payment against a client's open debt without assuming it's "
        "settled — e.g. debt of €50, client pays €30 now: call this with amount=30, leaving €20 still owed. "
        "If the amount given happens to cover the whole remaining balance, the debt is automatically marked "
        "fully paid. Give the client's name, or a debt id if known, plus the amount being paid now."),
     "input_schema": {"type": "object", "properties": {
         "name_or_id": {"type": "string"},
         "amount": {"type": "number", "description": "amount being paid now"},
         "payment_method": {"type": "string", "enum": ["cash", "card"], "description": "defaults to cash if not specified"}},
         "required": ["name_or_id", "amount"]}},
    {"name": "delete_debt", "description": "Permanently delete one debt record (not just mark it paid). Give the client's name, or a debt id if known.",
     "input_schema": {"type": "object", "properties": {"name_or_id": {"type": "string"}}, "required": ["name_or_id"]}},
    {"name": "delete_all_debts", "description": "Permanently delete every debt record for every client (paid and unpaid). Irreversible — always confirm before calling.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_clients", "description": "List all clients who have ever had a debt on file, with their open balance.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_client_debts", "description": "Full debt history (paid and unpaid) for one specific client.",
     "input_schema": {"type": "object", "properties": {"client_name": {"type": "string"}}, "required": ["client_name"]}},
    {"name": "list_unpaid_factures", "description": "List unpaid invoices/bills (factures) — rent, electricity, suppliers, etc.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "add_facture", "description": "Record a new invoice/bill to pay.",
     "input_schema": {"type": "object", "properties": {
         "facture_type": {"type": "string", "enum": ["ebay", "electricity", "water", "internet", "phone", "supplier", "rent", "other"]},
         "issuer": {"type": "string"}, "amount": {"type": "number"}, "due_date": {"type": "string", "description": "YYYY-MM-DD"}},
         "required": ["issuer", "amount"]}},
    {"name": "pay_facture", "description": "Mark an unpaid invoice as paid. Give the issuer name (e.g. 'EDF') or the invoice id.",
     "input_schema": {"type": "object", "properties": {"issuer_or_id": {"type": "string"}}, "required": ["issuer_or_id"]}},
    {"name": "list_low_stock", "description": "List items currently low on stock.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_kasse_today", "description": "Today's cash register (kasse) balance, sales and purchases.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_kasse_transactions", "description": "List the most recent cash register (kasse) deposit/withdrawal entries, with their ids — use this to find a transaction_id before calling edit_kasse_transaction.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "add_kasse_transaction", "description": "Book a manual cash deposit or withdrawal.",
     "input_schema": {"type": "object", "properties": {
         "type": {"type": "string", "enum": ["einzahlung", "auszahlung"], "description": "einzahlung=deposit, auszahlung=withdrawal"},
         "amount": {"type": "number"}, "description": {"type": "string"},
         "payment_method": {"type": "string", "enum": ["cash", "card"], "description": "defaults to cash if not specified"}}, "required": ["type", "amount"]}},
    {"name": "list_items", "description": "Search/list products in stock. The query matches product name (substring), and exact or partial barcode/SKU — same search used on the Inventory page.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string", "description": "product name, barcode, or SKU to search for; leave empty to list everything"}}}},
    {"name": "add_item", "description": "Add a new product to stock.",
     "input_schema": {"type": "object", "properties": {
         "product_name": {"type": "string"}, "quantity": {"type": "integer"},
         "purchase_price": {"type": "number"}, "selling_price": {"type": "number"},
         "min_selling_price": {"type": "number"}, "barcode": {"type": "string"},
         "sku": {"type": "string", "description": "optional internal stock-keeping unit code"}},
         "required": ["product_name"]}},
    {"name": "update_item", "description": (
        "Edit an existing product's name, quantity, prices, barcode, or SKU. Give an identifier "
        "(exact barcode, SKU, or a name to search for) plus only the field(s) that "
        "should change — anything left out keeps its current value. Use list_items first if "
        "you're not sure which exact product is meant."),
     "input_schema": {"type": "object", "properties": {
         "identifier": {"type": "string", "description": "barcode, SKU, or (part of) the product name"},
         "product_name": {"type": "string"}, "quantity": {"type": "integer"},
         "purchase_price": {"type": "number"}, "selling_price": {"type": "number"},
         "min_selling_price": {"type": "number"}, "barcode": {"type": "string"},
         "sku": {"type": "string", "description": "optional internal stock-keeping unit code"}},
         "required": ["identifier"]}},
    {"name": "delete_item", "description": "Permanently remove a product from stock. Give an identifier (exact barcode, SKU, or a name to search for). Use list_items first if you're not sure which exact product is meant.",
     "input_schema": {"type": "object", "properties": {
         "identifier": {"type": "string", "description": "barcode, SKU, or (part of) the product name"}},
         "required": ["identifier"]}},
    {"name": "list_sellers", "description": "List seller/employee accounts.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "add_seller", "description": "Create a new seller/employee login.",
     "input_schema": {"type": "object", "properties": {
         "username": {"type": "string"}, "password": {"type": "string"}, "salary": {"type": "number"}},
         "required": ["username", "password"]}},
    {"name": "list_salary_payments", "description": "List recent salary payments.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "pay_salary", "description": "Pay salary to an employee.",
     "input_schema": {"type": "object", "properties": {
         "employee": {"type": "string"}, "amount": {"type": "number"}, "note": {"type": "string"}},
         "required": ["employee", "amount"]}},
    {"name": "list_recent_orders", "description": "List recent purchase orders (Einkauf).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "add_order", "description": "Record a new purchase order (stock bought in).",
     "input_schema": {"type": "object", "properties": {
         "product_name": {"type": "string"}, "quantity": {"type": "integer"}, "price": {"type": "number"},
         "selling_price": {"type": "number"}, "ref_number": {"type": "string"},
         "payment_method": {"type": "string", "enum": ["cash", "card"], "description": "How the purchase was paid for — defaults to cash. Only cash purchases reduce the Kasse (cash drawer) balance. A card payment cannot be confirmed from plain chat text: this tool call will be rejected for payment_method=card. Tell the person to use the 'Neue Bestellung' form instead, which collects the card payment through the physical Stripe Terminal reader before saving."}},
         "required": ["product_name", "quantity", "price"]}},
    {"name": "list_recent_sales", "description": "List recent sales.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "quick_sell", "description": (
        "Sell a product by name or barcode right now. Before calling this, if the product name is "
        "ambiguous (e.g. several matching variants/models), call list_items first and ask the person "
        "which exact one they mean. If quantity, customer_name, or payment_method are not yet known, "
        "ask for them one at a time in natural conversation instead of guessing or assuming defaults — "
        "only call quick_sell once you have (or the person has explicitly skipped) all of them."),
     "input_schema": {"type": "object", "properties": {
         "identifier": {"type": "string", "description": "exact product name (or barcode) — the specific variant, not a generic category"},
         "quantity": {"type": "integer", "description": "how many units to sell"},
         "customer_name": {"type": "string", "description": "who the sale is for, if the person gave a name (optional)"},
         "payment_method": {"type": "string", "enum": ["cash", "card"], "description": "how the customer paid"},
         "sale_price": {"type": "number", "description": "unit price to sell at, only if it's different from the catalog price (e.g. a discount or haggled price). Leave out to use the catalog price as-is."}},
         "required": ["identifier", "quantity"]}},
    {"name": "edit_sale_item", "description": "Correct the quantity and/or unit price of a single line item within an already-recorded sale. Use list_recent_sales first if you don't already know the sale_item_id.",
     "input_schema": {"type": "object", "properties": {
         "sale_item_id": {"type": "integer"}, "quantity": {"type": "integer"}, "sale_price": {"type": "number"}},
         "required": ["sale_item_id", "quantity", "sale_price"]}},
    {"name": "delete_sale", "description": "Permanently delete an entire sale/order (all its line items). Stock quantities are NOT automatically restored. Use list_recent_sales first to find the order_id.",
     "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}},
    {"name": "print_barcode", "description": "Get the printable barcode image for a product, identified by name or barcode.",
     "input_schema": {"type": "object", "properties": {"identifier": {"type": "string"}}, "required": ["identifier"]}},
    {"name": "delete_salary_payment", "description": "Permanently delete a previously recorded salary payment (e.g. it was entered by mistake). Use list_salary_payments first if you don't already know the payment id.",
     "input_schema": {"type": "object", "properties": {"payment_id": {"type": "integer"}}, "required": ["payment_id"]}},
    {"name": "edit_salary_payment", "description": "Correct the amount, source, or note of a previously recorded salary payment. Use list_salary_payments first if you don't already know the payment id. Any field left out keeps its current value.",
     "input_schema": {"type": "object", "properties": {
         "payment_id": {"type": "integer"}, "amount": {"type": "number"},
         "source": {"type": "string"}, "note": {"type": "string"}},
         "required": ["payment_id", "amount"]}},
    {"name": "edit_kasse_transaction", "description": "Correct the amount, type, description, or payment method of a previously booked cash register (Kasse) transaction.",
     "input_schema": {"type": "object", "properties": {
         "transaction_id": {"type": "integer"},
         "type": {"type": "string", "enum": ["einzahlung", "auszahlung"]},
         "amount": {"type": "number"}, "description": {"type": "string"},
         "payment_method": {"type": "string", "enum": ["cash", "card"]}},
         "required": ["transaction_id", "type", "amount"]}},
    {"name": "delete_kasse_transaction", "description": "Permanently delete a previously booked cash register (Kasse) deposit/withdrawal entry. Use list_kasse_transactions first if you don't already know the transaction id.",
     "input_schema": {"type": "object", "properties": {"transaction_id": {"type": "integer"}}, "required": ["transaction_id"]}},
    {"name": "edit_seller", "description": (
        "Edit an existing seller/employee account — salary, active/inactive status, or profile image. "
        "Use list_sellers first if you're not sure of the exact username. Leave a field out to keep its current value."),
     "input_schema": {"type": "object", "properties": {
         "username": {"type": "string"}, "salary": {"type": "number"},
         "activated": {"type": "boolean", "description": "whether the account can log in"},
         "profile_img": {"type": "string"}},
         "required": ["username"]}},
    {"name": "delete_seller", "description": "Permanently delete a seller/employee account and its login. Use list_sellers first if you don't already know the exact username.",
     "input_schema": {"type": "object", "properties": {"username": {"type": "string"}}, "required": ["username"]}},
    {"name": "edit_order", "description": (
        "Edit an existing purchase order (Einkauf) — product name, quantity, purchase price, selling price, "
        "min selling price, reference number, description, or barcode. Use list_recent_orders first to find "
        "the order_number. Leave a field out to keep its current value."),
     "input_schema": {"type": "object", "properties": {
         "order_number": {"type": "string"}, "product_name": {"type": "string"},
         "quantity": {"type": "integer"}, "price": {"type": "number", "description": "purchase price"},
         "selling_price": {"type": "number"}, "min_selling_price": {"type": "number"},
         "ref_number": {"type": "string"}, "description": {"type": "string"}, "barcode": {"type": "string"}},
         "required": ["order_number"]}},
    {"name": "delete_order", "description": "Permanently delete a purchase order (Einkauf). Use list_recent_orders first to find the order_number.",
     "input_schema": {"type": "object", "properties": {"order_number": {"type": "string"}}, "required": ["order_number"]}},
    {"name": "edit_facture", "description": (
        "Edit an existing invoice/bill (facture) — type, issuer, amount, currency, issue/due date, status, or notes. "
        "Use list_unpaid_factures or give the invoice id if known. Leave a field out to keep its current value."),
     "input_schema": {"type": "object", "properties": {
         "issuer_or_id": {"type": "string", "description": "invoice id, or the issuer name to search for"},
         "facture_type": {"type": "string", "enum": ["ebay", "electricity", "water", "internet", "phone", "supplier", "rent", "other"]},
         "issuer": {"type": "string"}, "amount": {"type": "number"}, "currency": {"type": "string"},
         "issue_date": {"type": "string", "description": "YYYY-MM-DD"}, "due_date": {"type": "string", "description": "YYYY-MM-DD"},
         "status": {"type": "string", "enum": ["paid", "unpaid"]}, "notes": {"type": "string"}},
         "required": ["issuer_or_id"]}},
    {"name": "delete_facture", "description": "Permanently delete an invoice/bill record (not just mark it paid). Give the issuer name or the invoice id.",
     "input_schema": {"type": "object", "properties": {"issuer_or_id": {"type": "string"}}, "required": ["issuer_or_id"]}},
    {"name": "list_debt_payments", "description": "Full payment history recorded against one specific client's debt — use to see individual partial payments, not just the current remaining balance.",
     "input_schema": {"type": "object", "properties": {"name_or_id": {"type": "string"}}, "required": ["name_or_id"]}},
    {"name": "update_debt", "description": "Correct a debt's own details — client name, remaining amount owed, phone number, or description. This is different from record_debt_payment (which reduces the balance via an actual payment): use this only to fix a mistake in the record itself. Give the debt_id, its reference_number, or the client name to search for, plus only the field(s) that should change.",
     "input_schema": {"type": "object", "properties": {
         "name_or_id": {"type": "string", "description": "debt_id, reference_number, or (part of) the client name"},
         "client_name": {"type": "string"}, "amount": {"type": "number"},
         "phone_number": {"type": "string"}, "description": {"type": "string"}},
         "required": ["name_or_id"]}},
    {"name": "edit_debt_payment", "description": "Correct the amount or payment method of a previously recorded debt payment. Use list_debt_payments first if you don't already know the payment id.",
     "input_schema": {"type": "object", "properties": {
         "payment_id": {"type": "integer"}, "amount": {"type": "number"},
         "payment_method": {"type": "string", "enum": ["cash", "card"]}},
         "required": ["payment_id"]}},
    {"name": "delete_debt_payment", "description": "Permanently delete a previously recorded debt payment (e.g. it was entered by mistake) — this increases the client's remaining owed balance back up. Use list_debt_payments first if you don't already know the payment id.",
     "input_schema": {"type": "object", "properties": {"payment_id": {"type": "integer"}}, "required": ["payment_id"]}},
    {"name": "delete_client", "description": "Permanently delete a client's record. Fails if the client still has an open (unpaid) debt — settle or delete that debt first.",
     "input_schema": {"type": "object", "properties": {"client_name": {"type": "string"}}, "required": ["client_name"]}},
    {"name": "rename_client", "description": "Rename a client — since a client is just the name on their debt records, this renames it across every debt row they have. Give the client's current name and the new name.",
     "input_schema": {"type": "object", "properties": {
         "client_name": {"type": "string", "description": "the client's current name"},
         "new_client_name": {"type": "string", "description": "the new name to rename them to"}},
         "required": ["client_name", "new_client_name"]}},
    {"name": "get_dashboard_stats", "description": "Full live dashboard KPIs: today's sales/profit/purchases, inventory value, low-stock count, outstanding vs paid debt, and today's debt payments/withdrawals broken down by cash vs card — everything shown on the admin dashboard cards.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "search_audit_log", "description": "Search the Activity Log (who did what, when, and from where — web page or AI assistant). Any filter left out matches everything.",
     "input_schema": {"type": "object", "properties": {
         "actor": {"type": "string", "description": "username who performed the action"},
         "module": {"type": "string", "description": "e.g. sales, purchases, inventory, debts, invoices, cash, sellers, salaries"},
         "action": {"type": "string", "description": "e.g. debt_created, assistant_sale, seller_deleted — substring match"},
         "source": {"type": "string", "enum": ["web", "assistant"], "description": "whether the action was done via the web UI or the AI assistant"},
         "date": {"type": "string", "description": "YYYY-MM-DD — only entries from this day"},
         "limit": {"type": "integer", "description": "max entries to return, defaults to 20"}},
         "required": []}},
    {"name": "remember_note", "description": (
        "Save one short, durable fact about this user or their shop so you remember it in FUTURE conversations "
        "too, not just this one (e.g. 'prefers card over cash for big sales', 'main supplier is Ahmed Wholesale', "
        "'closes the shop on Sundays', 'always calls the iPhone 13 case just \"the black case\"'). Use sparingly — "
        "only for things worth recalling weeks from now, not for details already obvious from this conversation. "
        "Never needs confirmation; it doesn't touch shop data, only your own memory of this user."),
     "input_schema": {"type": "object", "properties": {
         "note": {"type": "string", "description": "one short, self-contained fact, written so it still makes sense out of context later"}},
         "required": ["note"]}},
]

# Tools that mutate data (as opposed to list/read-only lookups) — used to
# decide what gets written to the audit_log table when the chat assistant
# calls a tool, so the trail covers admin-panel actions and chat actions
# the same way.
# Read-only tool results rich/structured enough to be worth rendering as an
# actual widget (product cards, a table, a small chart) in the chat, instead
# of only the AI's prose. See renderWidget() in assistant.js.
WIDGETABLE_TOOLS = {
    'get_summary', 'list_items', 'list_recent_sales', 'list_open_debts',
    'list_low_stock', 'list_recent_orders',
}

# The chat is now available to sellers too (not just admins), but the tools
# themselves were written assuming an admin caller and have no per-row
# ownership checks. A seller's reachable tool set is SELLER_BASE_TOOLS plus
# whatever categories the admin has granted them on the Edit Seller page
# ("KI-Assistent — zusätzliche Berechtigungen") — computed per-request by
# seller_allowed_tools() (see near shop_feature_enabled() above) so a
# revoke takes effect immediately. Everything else raises PermissionError
# (see the check at the top of _execute_assistant_tool) and the seller
# gets a plain "not permitted" message instead of the action running.

ASSISTANT_WRITE_TOOLS = {
    'add_debt', 'pay_debt', 'record_debt_payment', 'delete_debt', 'delete_all_debts', 'update_debt',
    'add_facture', 'pay_facture', 'add_kasse_transaction', 'add_item',
    'update_item', 'delete_item',
    'add_seller', 'pay_salary', 'add_order', 'quick_sell',
    'edit_sale_item', 'delete_salary_payment', 'edit_salary_payment', 'edit_kasse_transaction',
    'delete_sale',
    'delete_kasse_transaction', 'edit_seller', 'delete_seller',
    'edit_order', 'delete_order', 'edit_facture', 'delete_facture',
    'edit_debt_payment', 'delete_debt_payment', 'delete_client', 'rename_client',
}


def _describe_assistant_action(name, args, result):
    """Map a completed assistant write-tool call to a descriptive audit
    action name, entity type, module, and human-readable details —
    replacing the previous blanket action='assistant_tool' logging that
    made every AI-driven write indistinguishable in the Activity Log.
    `args` are the tool call's own arguments; `result` is whatever
    _execute_assistant_tool returned for it.
    """
    result = result if isinstance(result, dict) else {}

    if name == 'add_debt':
        return ('debt_created', 'debt', 'debts',
                format_debt_details(args.get('client_name'), args.get('amount'), args.get('description'), args.get('phone_number')))
    if name == 'pay_debt':
        return ('debt_payment', 'debt', 'debts',
                format_debt_payment_details(result.get('client_name'), result.get('amount_paid', result.get('amount')),
                                             result.get('payment_method') or args.get('payment_method') or 'cash',
                                             remaining=result.get('remaining')))
    if name == 'record_debt_payment':
        return ('debt_payment', 'debt', 'debts',
                format_debt_payment_details(result.get('client_name'), result.get('amount_paid', result.get('amount')),
                                             result.get('payment_method') or args.get('payment_method') or 'cash',
                                             remaining=result.get('remaining')))
    if name == 'delete_debt':
        return ('debt_deleted', 'debt', 'debts', format_debt_details(result.get('client_name'), result.get('amount')))
    if name == 'delete_all_debts':
        return ('debt_deleted_all', 'debt', 'debts', f"Deleted all {result.get('deleted_count', 0)} debt record(s)")
    if name == 'add_facture':
        return ('invoice_created', 'facture', 'invoices', format_invoice_details(result.get('issuer'), result.get('amount')))
    if name == 'pay_facture':
        return ('invoice_payment', 'facture', 'invoices', format_invoice_details(result.get('issuer'), result.get('amount'), status='paid'))
    if name == 'add_kasse_transaction':
        typ = args.get('type')
        action = 'deposit_created' if typ == 'einzahlung' else 'withdrawal_created'
        return (action, 'cash_transaction', 'cash', format_kasse_details(typ, args.get('amount'), args.get('description')))
    if name in ('add_item', 'update_item', 'delete_item'):
        product_name = result.get('product_name') or args.get('product_name') or args.get('identifier')
        barcode = result.get('barcode') or args.get('barcode')
        sku = result.get('sku') or args.get('sku')
        return ('assistant_inventory_update', 'item', 'inventory', format_product_details(product_name, barcode, sku))
    if name == 'add_seller':
        return ('seller_created', 'seller', 'sellers', format_seller_details(args.get('username'), args.get('salary')))
    if name == 'pay_salary':
        return ('salary_paid', 'salary_payment', 'salaries', format_salary_details(args.get('employee'), args.get('amount')))
    if name == 'add_order':
        return ('assistant_purchase', 'order', 'purchases',
                format_purchase_details(result.get('product_name'), args.get('quantity'), args.get('price')))
    if name == 'quick_sell':
        return ('assistant_sale', 'sale', 'sales',
                format_sale_details(result.get('product_name'), result.get('quantity'), None,
                                     result.get('total_price'), result.get('payment_method'), result.get('customer_name')))
    if name == 'edit_sale_item':
        return ('assistant_sale', 'sale_item', 'sales', result.get('message') or f"Updated sale item #{args.get('sale_item_id')}")
    if name == 'delete_salary_payment':
        return ('salary_payment_deleted', 'salary_payment', 'salaries',
                f"Deleted salary payment of {format_money(result.get('amount'))} to {result.get('employee')}")
    if name == 'edit_salary_payment':
        return ('salary_payment_updated', 'salary_payment', 'salaries',
                f"Updated salary payment #{args.get('payment_id')}: {format_money(result.get('amount'))} to {result.get('employee')}")
    if name == 'edit_kasse_transaction':
        typ = args.get('type')
        action = 'deposit_updated' if typ == 'einzahlung' else 'withdrawal_updated'
        return (action, 'cash_transaction', 'cash', format_kasse_details(typ, args.get('amount'), args.get('description')))
    if name == 'delete_sale':
        return ('assistant_sale', 'sale', 'sales', result.get('message') or f"Deleted sale #{args.get('order_id')}")
    if name == 'delete_kasse_transaction':
        typ = result.get('type')
        action = 'deposit_deleted' if typ == 'einzahlung' else 'withdrawal_deleted'
        return (action, 'cash_transaction', 'cash', format_kasse_details(typ, result.get('amount'), result.get('description')))
    if name == 'edit_seller':
        return ('seller_updated', 'seller', 'sellers', format_seller_details(args.get('username'), result.get('salary')))
    if name == 'delete_seller':
        return ('seller_deleted', 'seller', 'sellers', args.get('username'))
    if name == 'edit_order':
        return ('purchase_updated', 'order', 'purchases',
                format_purchase_details(result.get('product_name'), result.get('quantity'), result.get('price')))
    if name == 'delete_order':
        return ('purchase_deleted', 'order', 'purchases',
                format_purchase_details(result.get('product_name'), result.get('quantity'), result.get('price')))
    if name == 'edit_facture':
        return ('invoice_updated', 'facture', 'invoices', format_invoice_details(result.get('issuer'), result.get('amount'), status=result.get('status')))
    if name == 'delete_facture':
        return ('invoice_deleted', 'facture', 'invoices', format_invoice_details(result.get('issuer'), result.get('amount')))
    if name == 'edit_debt_payment':
        return ('debt_payment', 'debt_payment', 'debts', f"Updated debt payment #{args.get('payment_id')}")
    if name == 'update_debt':
        return ('debt_updated', 'debt', 'debts',
                format_debt_details(result.get('client_name'), result.get('amount'), result.get('description'), result.get('phone_number')))
    if name == 'delete_debt_payment':
        return ('debt_payment_deleted', 'debt_payment', 'debts',
                f"Deleted debt payment of {format_money(result.get('amount'))}")
    if name == 'rename_client':
        return ('customer_renamed', 'client', 'customers',
                f"Renamed customer \"{args.get('client_name')}\" to \"{args.get('new_client_name')}\"")
    if name == 'delete_client':
        return ('customer_deleted', 'client', 'customers', f"Deleted customer: {args.get('client_name')}")
    # Shouldn't happen — every entry in ASSISTANT_WRITE_TOOLS is handled
    # above — but fail safe with *some* descriptive-ish label rather than
    # silently losing the log entry.
    return ('assistant_tool', name, 'assistant', f'{name}({args})')


def _execute_assistant_tool(name, args, username):
    if session.get('role') != 'admin' and name not in seller_allowed_tools(username):
        raise PermissionError(f"'{name}' ist für dieses Konto nicht freigegeben.")
    if name == 'get_summary':
        today = datetime.now().date()
        items = load_items()
        debts_open = fetch_one("SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS cnt FROM debts WHERE paid = FALSE;")
        factures_summary = get_facture_summary()
        low_stock = get_low_stock_notifications(items, threshold=5)
        return {
            'sales_today': calculate_today_sales(), 'profit_today': calculate_today_profit(),
            'sales_month': calculate_monthly_sales(), 'kasse_balance': get_kasse_balance_for_date(today),
            'debts_open_total': round(float(debts_open['total'] or 0), 2),
            'debts_open_count': int(debts_open['cnt'] or 0),
            'factures_unpaid_total': factures_summary['unpaid_amount'], 'low_stock_count': len(low_stock),
        }
    if name == 'list_open_debts':
        rows = fetch_all(
            "SELECT debt_id, client_name, description, amount, phone_number FROM debts "
            "WHERE paid = FALSE ORDER BY created_at DESC LIMIT 20;")
        return {'debts': rows}
    if name == 'add_debt':
        debt_id = _add_debt_record(args.get('client_name'), args.get('amount'), args.get('phone_number'), args.get('description'))
        return {'success': True, 'debt_id': debt_id, 'message': f'Debt created for {args.get("client_name")}'}
    if name == 'pay_debt':
        target = _find_open_debt(str(args.get('name_or_id') or ''))
        if not target:
            return {'success': False, 'message': f'No open debt found for "{args.get("name_or_id")}".'}
        amount_paid = float(target['amount'])  # always the full remaining balance — pay_debt never partial-pays
        payment_method = (args.get('payment_method') or 'cash').strip().lower()
        updated_debt = record_debt_payment(target['debt_id'], amount_paid, payment_method, recorded_by=username)
        message = f'Full payment of €{amount_paid:,.2f} recorded for {target["client_name"]} — debt fully paid.'
        return {'success': True, 'client_name': target['client_name'], 'amount_paid': amount_paid,
                'remaining': float(updated_debt['amount']), 'paid': bool(updated_debt['paid']), 'payment_method': payment_method,
                'message': message}
    if name == 'record_debt_payment':
        target = _find_open_debt(str(args.get('name_or_id') or ''))
        if not target:
            return {'success': False, 'message': f'No open debt found for "{args.get("name_or_id")}".'}
        amount_raw = args.get('amount')
        if amount_raw in (None, ''):
            raise ValueError('amount is required for record_debt_payment')
        amount_paid = float(amount_raw)
        payment_method = (args.get('payment_method') or 'cash').strip().lower()
        updated_debt = record_debt_payment(target['debt_id'], amount_paid, payment_method, recorded_by=username)
        remaining = float(updated_debt['amount'])
        message = (f'Full payment of €{amount_paid:,.2f} recorded for {target["client_name"]} — debt fully paid.'
                   if updated_debt['paid'] else
                   f'Partial payment of €{amount_paid:,.2f} recorded for {target["client_name"]} — €{remaining:,.2f} still owed.')
        return {'success': True, 'client_name': target['client_name'], 'amount_paid': amount_paid,
                'remaining': remaining, 'paid': bool(updated_debt['paid']), 'payment_method': payment_method,
                'message': message}
    if name == 'delete_debt':
        target = _find_any_debt(str(args.get('name_or_id') or ''))
        if not target:
            return {'success': False, 'message': f'No debt found for "{args.get("name_or_id")}".'}
        execute_query("DELETE FROM debts WHERE debt_id = %s;", (target['debt_id'],))
        return {'success': True, 'client_name': target['client_name'], 'amount': float(target['amount']), 'message': f'Debt deleted for {target["client_name"]}'}
    if name == 'delete_all_debts':
        count_row = fetch_one("SELECT COUNT(*) AS cnt FROM debts;")
        execute_query("DELETE FROM debts;")
        return {'success': True, 'deleted_count': int(count_row['cnt'] or 0), 'message': f'All debts deleted'}
    if name == 'list_clients':
        rows = fetch_all(
            """SELECT client_name, MAX(paid = FALSE) AS has_unpaid, COUNT(*) AS debt_count,
                      COALESCE(SUM(CASE WHEN paid = FALSE THEN amount ELSE 0 END), 0) AS total_unpaid
               FROM debts GROUP BY client_name ORDER BY client_name;""")
        return {'clients': [{
            'client_name': r['client_name'], 'has_unpaid': bool(r['has_unpaid']),
            'debt_count': int(r['debt_count'] or 0), 'total_unpaid': round(float(r['total_unpaid'] or 0), 2),
        } for r in rows]}
    if name == 'get_client_debts':
        client_name = args.get('client_name')
        rows = fetch_all(
            """SELECT debt_id, client_name, description, amount, phone_number, paid, created_at
               FROM debts WHERE client_name = %s ORDER BY created_at DESC;""", (client_name,))
        return {'client_name': client_name, 'debts': [{
            'debt_id': r['debt_id'], 'description': r.get('description'), 'amount': round(float(r['amount'] or 0), 2),
            'phone_number': r.get('phone_number'), 'paid': bool(r['paid']),
            'created_at': r['created_at'].strftime('%Y-%m-%d') if r.get('created_at') else None,
        } for r in rows]}
    if name == 'list_unpaid_factures':
        rows = get_factures(status='unpaid')[:20]
        return {'factures': [{
            'id': f['id'], 'issuer': f['issuer'], 'amount': float(f['amount']), 'currency': f.get('currency', 'EUR'),
            'facture_type': FACTURE_TYPES.get(f.get('facture_type'), f.get('facture_type')),
            'due_date': f['due_date'].strftime('%d.%m.%Y') if f.get('due_date') else None,
        } for f in rows]}
    if name == 'add_facture':
        payload = _add_facture_record(args.get('facture_type'), args.get('issuer'), args.get('amount'),
                                       due_date=args.get('due_date'), created_by=username)
        return {'success': True, 'issuer': payload['issuer'], 'amount': payload['amount']}
    if name == 'pay_facture':
        target = _find_unpaid_facture(str(args.get('issuer_or_id') or ''))
        if not target:
            return {'success': False, 'message': f'No unpaid invoice found for "{args.get("issuer_or_id")}".'}
        execute_query("UPDATE factures SET status = 'paid' WHERE id = %s;", (target['id'],))
        return {'success': True, 'issuer': target['issuer'], 'amount': float(target['amount'])}
    if name == 'list_low_stock':
        items = load_items()
        notes = get_low_stock_notifications(items, threshold=5)
        return {'items': [{'barcode': n.get('barcode'), 'message': n.get('message')} for n in notes[:20]]}
    if name == 'get_kasse_today':
        today = datetime.now().date()
        return {'balance': get_kasse_balance_for_date(today), 'sales_today': calculate_sales_for_date(today),
                'purchases_today': calculate_purchases_for_date(today),
                'cash_debt_payments_today': calculate_cash_debt_payments_for_date(today),
                'cash_salary_payments_today': calculate_cash_salary_payments_for_date(today)}
    if name == 'list_kasse_transactions':
        rows = fetch_all(
            "SELECT id, date, amount, type, description, username, payment_method FROM cash_transactions ORDER BY date DESC LIMIT 15;"
        )
        return {'transactions': [{
            'transaction_id': r['id'], 'date': r['date'].strftime('%d.%m.%Y %H:%M') if r.get('date') else None,
            'amount': float(r['amount'] or 0), 'type': r.get('type'), 'description': r.get('description'),
            'payment_method': r.get('payment_method') or 'cash',
        } for r in rows]}
    if name == 'add_kasse_transaction':
        if not seller_kasse_enabled(username):
            raise PermissionError('Kasse-Zugriff (Ein-/Auszahlung) wurde für Ihr Konto deaktiviert.')
        _add_kasse_record(args.get('type'), args.get('amount'), args.get('description'), username,
                           args.get('payment_method') or 'cash')
        return {'success': True}
    if name == 'list_items':
        # search_items() is the single search implementation shared with
        # the Inventory page and the external API — matches barcode/SKU
        # (exact or partial) and product name (substring), case-insensitive.
        items = search_items(args.get('query') or '', limit=20)
        return {'items': [{
            'id': i.get('barcode'), 'product_name': i.get('product_name'), 'barcode': i.get('barcode'),
            'sku': i.get('sku'), 'quantity': int(i.get('quantity') or 0),
            'selling_price': float(i.get('selling_price') or 0),
            'purchase_price': float(i.get('purchase_price') or 0),
        } for i in items]}
    if name == 'add_item':
        barcode_value = _add_item_record(args.get('product_name'), args.get('quantity'), args.get('purchase_price'),
                                          args.get('selling_price'), args.get('min_selling_price'), args.get('barcode'),
                                          args.get('sku'))
        return {'success': True, 'barcode': barcode_value}
    if name == 'update_item':
        item = _update_item_record(
            args.get('identifier'), product_name=args.get('product_name'), quantity=args.get('quantity'),
            purchase_price=args.get('purchase_price'), selling_price=args.get('selling_price'),
            min_selling_price=args.get('min_selling_price'), barcode_value=args.get('barcode'),
            sku=args.get('sku'),
        )
        return {'success': True, 'product_name': item['product_name'], 'barcode': item['barcode'], 'sku': item.get('sku')}
    if name == 'delete_item':
        item = _delete_item_record(args.get('identifier'))
        return {'success': True, 'product_name': item.get('product_name'), 'barcode': item.get('barcode')}
    if name == 'list_sellers':
        sellers = [s for s in load_users() if s.get('role') == 'seller']
        return {'sellers': [{'username': s['username'], 'salary': float(s.get('salary') or 0),
                              'activated': bool(s.get('activated'))} for s in sellers]}
    if name == 'add_seller':
        _add_seller_record(args.get('username'), args.get('password'), args.get('salary'))
        return {'success': True}
    if name == 'list_salary_payments':
        rows = fetch_all("SELECT id, employee, amount, source, payment_date FROM salaries ORDER BY payment_date DESC LIMIT 10;")
        return {'payments': [{
            'payment_id': r['id'], 'employee': r['employee'], 'amount': float(r['amount']), 'source': r.get('source'),
            'payment_date': r['payment_date'].strftime('%d.%m.%Y') if r.get('payment_date') else None,
        } for r in rows]}
    if name == 'pay_salary':
        _pay_salary_record(args.get('employee'), args.get('amount'), note=args.get('note'))
        return {'success': True}
    if name == 'list_recent_orders':
        orders = get_orders(role='admin')[:10]
        return {'orders': [{
            'order_number': o['order_number'], 'product_name': o['product_name'], 'quantity': o['quantity'],
            'price': o['price'], 'total_price': o['total_price'], 'date': o['date'], 'user': o['user'],
        } for o in orders]}
    if name == 'add_order':
        order = _add_order_record(args.get('product_name'), args.get('quantity'), args.get('price'),
                                   args.get('selling_price'), ref_number=args.get('ref_number'), username=username,
                                   payment_method=args.get('payment_method'))
        return {'success': True, 'product_name': order['product_name'], 'total_price': order['total_price']}
    if name == 'list_recent_sales':
        sales = load_sales()[:10]
        out = []
        for s in sales:
            raw_date = s.get('date')
            date_str = raw_date.strftime('%d.%m.%Y %H:%M') if hasattr(raw_date, 'strftime') else (str(raw_date) if raw_date else None)
            out.append({
                'order_id': s.get('order_id'), 'user': s.get('user'), 'date': date_str,
                'total': float(s.get('total_order_price') or 0), 'items_count': len(s.get('items') or []),
                'items': [{
                    'sale_item_id': i.get('id'), 'product_name': i.get('product_name'),
                    'quantity': i.get('quantity'), 'sale_price': float(i.get('sale_price') or 0),
                } for i in (s.get('items') or [])],
            })
        return {'sales': out}
    if name == 'quick_sell':
        return _quick_sell_record(
            args.get('identifier'), args.get('quantity'), username,
            customer_name=args.get('customer_name'), payment_method=args.get('payment_method'),
            sale_price=args.get('sale_price'),
        )
    if name == 'edit_sale_item':
        ok, message = update_sale_item(args.get('sale_item_id'), args.get('quantity'), args.get('sale_price'))
        return {'success': ok, 'message': message}
    if name == 'delete_sale':
        ok, message = delete_sales_order(args.get('order_id'))
        return {'success': ok, 'message': message}
    if name == 'print_barcode':
        identifier = (args.get('identifier') or '').strip()
        items = load_items()
        item = next((i for i in items if i.get('barcode') == identifier), None)
        if not item:
            identifier_lower = identifier.lower()
            item = next((i for i in items if identifier_lower in (i.get('product_name') or '').lower()), None)
        if not item:
            raise LookupError(f'no item found for "{identifier}"')
        return {
            'success': True,
            'product_name': item.get('product_name'),
            'barcode': item.get('barcode'),
            'image_url': url_for('barcode_print', barcode_value=item.get('barcode')),
        }
    if name == 'delete_salary_payment':
        payment = get_salary_payment(args.get('payment_id'))
        if not payment:
            return {'success': False, 'message': f'No salary payment found with id {args.get("payment_id")}.'}
        delete_salary_payment(args.get('payment_id'))
        return {'success': True, 'employee': payment.get('employee'), 'amount': float(payment.get('amount') or 0)}
    if name == 'edit_salary_payment':
        payment = get_salary_payment(args.get('payment_id'))
        if not payment:
            return {'success': False, 'message': f'No salary payment found with id {args.get("payment_id")}.'}
        amount = args.get('amount')
        if amount is None or float(amount) <= 0:
            return {'success': False, 'message': 'Please provide a valid amount greater than 0.'}
        source = args.get('source') or payment.get('source') or 'Kasse'
        note = args.get('note') if args.get('note') is not None else (payment.get('note') or '')
        update_salary_payment(args.get('payment_id'), float(amount), source, note)
        return {'success': True, 'employee': payment.get('employee'), 'amount': float(amount)}
    if name == 'edit_kasse_transaction':
        transaction = get_cash_transaction(args.get('transaction_id'))
        if not transaction:
            return {'success': False, 'message': f'No cash transaction found with id {args.get("transaction_id")}.'}
        update_cash_transaction(args.get('transaction_id'), args.get('amount'), args.get('type'),
                                 args.get('description') or transaction.get('description'),
                                 args.get('payment_method'))
        return {'success': True}
    if name == 'delete_kasse_transaction':
        transaction = get_cash_transaction(args.get('transaction_id'))
        if not transaction:
            return {'success': False, 'message': f'No cash transaction found with id {args.get("transaction_id")}.'}
        execute_query("DELETE FROM cash_transactions WHERE id = %s", (args.get('transaction_id'),))
        return {'success': True, 'type': transaction.get('type'), 'amount': float(transaction.get('amount') or 0),
                'description': transaction.get('description')}
    if name == 'edit_seller':
        username_arg = args.get('username')
        sellers = load_users()
        seller = next((s for s in sellers if s['username'] == username_arg), None)
        if not seller:
            return {'success': False, 'message': f'No seller found with username "{username_arg}".'}
        salary = float(args.get('salary')) if args.get('salary') not in (None, '') else float(seller.get('salary') or 0)
        activated = bool(args.get('activated')) if args.get('activated') is not None else bool(seller.get('activated'))
        profile_img = args.get('profile_img') if args.get('profile_img') is not None else seller.get('profile_img', '')
        update_user(username_arg, {'profile_img': profile_img, 'salary': salary, 'activated': activated})
        return {'success': True, 'username': username_arg, 'salary': salary, 'activated': activated}
    if name == 'delete_seller':
        username_arg = args.get('username')
        sellers = load_users()
        if not any(s['username'] == username_arg for s in sellers):
            return {'success': False, 'message': f'No seller found with username "{username_arg}".'}
        delete_user(username_arg)
        return {'success': True, 'username': username_arg}
    if name == 'edit_order':
        order_number = args.get('order_number')
        order = query_one("SELECT * FROM orders WHERE order_number = %s;", (order_number,))
        if not order:
            return {'success': False, 'message': f'No purchase order found with number "{order_number}".'}
        quantity = int(args.get('quantity')) if args.get('quantity') not in (None, '') else int(order.get('quantity') or 0)
        selling_price = float(args.get('selling_price')) if args.get('selling_price') not in (None, '') else float(order.get('selling_price') or 0)
        order_data = {
            'product_name': args.get('product_name') or order.get('product_name'),
            'ref_number': args.get('ref_number') if args.get('ref_number') is not None else order.get('ref_number'),
            'description': args.get('description') if args.get('description') is not None else order.get('description'),
            'price': float(args.get('price')) if args.get('price') not in (None, '') else float(order.get('price') or 0),
            'selling_price': selling_price,
            'min_selling_price': float(args.get('min_selling_price')) if args.get('min_selling_price') not in (None, '') else float(order.get('min_selling_price') or 0),
            'quantity': quantity,
            'total_price': selling_price * quantity,
            'date': datetime.now().strftime('%Y-%m-%d'),
            'user': username,
            'barcode': args.get('barcode') if args.get('barcode') is not None else order.get('barcode'),
        }
        update_order(order_number, order_data)
        return {'success': True, 'order_number': order_number, 'product_name': order_data['product_name'],
                'quantity': order_data['quantity'], 'price': order_data['price']}
    if name == 'delete_order':
        order_number = args.get('order_number')
        order = query_one("SELECT product_name, quantity, price FROM orders WHERE order_number = %s;", (order_number,))
        if not order:
            return {'success': False, 'message': f'No purchase order found with number "{order_number}".'}
        delete_order(order_number)
        return {'success': True, 'order_number': order_number, 'product_name': order.get('product_name'),
                'quantity': order.get('quantity'), 'price': order.get('price')}
    if name == 'edit_facture':
        facture = _find_facture(str(args.get('issuer_or_id') or ''))
        if not facture:
            return {'success': False, 'message': f'No invoice found for "{args.get("issuer_or_id")}".'}
        data = {
            'facture_type': args.get('facture_type') or facture.get('facture_type') or 'other',
            'reference': facture.get('reference'),
            'issuer': (args.get('issuer') or facture.get('issuer') or '').strip(),
            'amount': float(args.get('amount')) if args.get('amount') not in (None, '') else float(facture.get('amount') or 0),
            'currency': args.get('currency') or facture.get('currency') or 'EUR',
            'issue_date': args.get('issue_date') or (facture.get('issue_date').isoformat() if hasattr(facture.get('issue_date'), 'isoformat') else facture.get('issue_date')),
            'due_date': args.get('due_date') if args.get('due_date') is not None else (facture.get('due_date').isoformat() if hasattr(facture.get('due_date'), 'isoformat') else facture.get('due_date')),
            'status': args.get('status') or facture.get('status') or 'unpaid',
            'notes': args.get('notes') if args.get('notes') is not None else facture.get('notes'),
        }
        update_facture(facture['id'], data)
        return {'success': True, 'issuer': data['issuer'], 'amount': data['amount'], 'status': data['status']}
    if name == 'delete_facture':
        facture = _find_facture(str(args.get('issuer_or_id') or ''))
        if not facture:
            return {'success': False, 'message': f'No invoice found for "{args.get("issuer_or_id")}".'}
        delete_facture(facture['id'])
        return {'success': True, 'issuer': facture.get('issuer'), 'amount': float(facture.get('amount') or 0)}
    if name == 'list_debt_payments':
        target = _find_any_debt(str(args.get('name_or_id') or ''))
        if not target:
            return {'success': False, 'message': f'No debt found for "{args.get("name_or_id")}".'}
        payments = list_debt_payments(target['debt_id'])
        return {'client_name': target['client_name'], 'payments': [{
            'payment_id': p['id'], 'amount': float(p['amount']), 'payment_method': p.get('payment_method'),
            'paid_at': p['paid_at'].strftime('%d.%m.%Y %H:%M') if hasattr(p.get('paid_at'), 'strftime') else p.get('paid_at'),
        } for p in payments]}
    if name == 'update_debt':
        target = _find_any_debt(str(args.get('name_or_id') or ''))
        if not target:
            return {'success': False, 'message': f'No debt found for "{args.get("name_or_id")}".'}
        debt = query_one("SELECT * FROM debts WHERE debt_id = %s;", (target['debt_id'],))
        new_name = args.get('client_name')
        new_amount = args.get('amount')
        client_name = (new_name.strip() if new_name is not None else debt['client_name']) or ''
        if not client_name:
            return {'success': False, 'message': 'client_name cannot be empty'}
        if new_amount is not None:
            try:
                amount = float(new_amount)
            except (TypeError, ValueError):
                return {'success': False, 'message': 'amount must be a number'}
            if amount < 0:
                return {'success': False, 'message': 'amount cannot be negative'}
        else:
            amount = float(debt['amount'])
        new_phone = args.get('phone_number')
        new_desc = args.get('description')
        phone_number = (new_phone.strip() if new_phone is not None else debt.get('phone_number')) or None
        description = (new_desc.strip() if new_desc is not None else debt.get('description')) or None
        execute_query(
            "UPDATE debts SET client_name = %s, amount = %s, phone_number = %s, description = %s WHERE debt_id = %s;",
            (client_name, amount, phone_number, description, debt['debt_id']),
        )
        return {'success': True, 'debt_id': debt['debt_id'], 'client_name': client_name, 'amount': amount,
                'phone_number': phone_number, 'description': description,
                'message': f'Debt updated for {client_name}: {format_money(amount)} remaining.'}
    if name == 'edit_debt_payment':
        payment = get_debt_payment(args.get('payment_id'))
        if not payment:
            return {'success': False, 'message': f'No debt payment found with id {args.get("payment_id")}.'}
        try:
            updated_payment, updated_debt = edit_debt_payment(
                args.get('payment_id'), amount=args.get('amount'), payment_method=args.get('payment_method'))
        except ValueError as e:
            return {'success': False, 'message': str(e)}
        remaining = float(updated_debt['amount'])
        message = (f"Payment #{args.get('payment_id')} updated to {format_money(updated_payment['amount'])} "
                   f"via {updated_payment['payment_method']}. {updated_debt['client_name']} now "
                   + ('has no remaining balance.' if updated_debt['paid'] else f'owes {format_money(remaining)}.'))
        return {'success': True, 'payment_id': args.get('payment_id'), 'amount': float(updated_payment['amount']),
                'payment_method': updated_payment['payment_method'], 'client_name': updated_debt['client_name'],
                'remaining': remaining, 'paid': bool(updated_debt['paid']), 'message': message}
    if name == 'delete_debt_payment':
        payment = get_debt_payment(args.get('payment_id'))
        if not payment:
            return {'success': False, 'message': f'No debt payment found with id {args.get("payment_id")}.'}
        try:
            updated_debt = delete_debt_payment(args.get('payment_id'))
        except ValueError as e:
            return {'success': False, 'message': str(e)}
        remaining = float(updated_debt['amount'])
        message = (f"Deleted payment of {format_money(payment['amount'])}. {updated_debt['client_name']} now "
                   f"owes {format_money(remaining)}.")
        return {'success': True, 'amount': float(payment.get('amount') or 0), 'client_name': updated_debt['client_name'],
                'remaining': remaining, 'paid': bool(updated_debt['paid']), 'message': message}
    if name == 'rename_client':
        old_name = (args.get('client_name') or '').strip()
        new_name = (args.get('new_client_name') or '').strip()
        if not old_name or not new_name:
            return {'success': False, 'message': 'client_name and new_client_name are both required.'}
        if old_name == new_name:
            return {'success': False, 'message': 'The new name is identical to the current one.'}
        exists = fetch_one("SELECT 1 FROM debts WHERE client_name = %s;", (old_name,))
        if not exists:
            return {'success': False, 'message': f'No client found with the name "{old_name}".'}
        execute_query("UPDATE debts SET client_name = %s WHERE client_name = %s;", (new_name, old_name))
        return {'success': True, 'old_client_name': old_name, 'client_name': new_name}
    if name == 'delete_client':
        client_name = args.get('client_name')
        open_count = fetch_one("SELECT COUNT(*) AS cnt FROM debts WHERE client_name = %s AND paid = FALSE", (client_name,))
        if int(open_count['cnt'] or 0) > 0:
            return {'success': False, 'message': f'{client_name} still has an open debt — settle or delete it first.'}
        execute_query("DELETE FROM debts WHERE client_name = %s", (client_name,))
        return {'success': True, 'client_name': client_name}
    if name == 'get_dashboard_stats':
        return get_dashboard_stats()
    if name == 'search_audit_log':
        limit = int(args.get('limit') or 20)
        rows = get_audit_log(limit=300, entity=None, module=(args.get('module') or None))
        actor = (args.get('actor') or '').strip().lower()
        action = (args.get('action') or '').strip().lower()
        source = (args.get('source') or '').strip().lower()
        date_str = (args.get('date') or '').strip()
        def _match(r):
            if actor and actor not in (r.get('actor') or '').lower():
                return False
            if action and action not in (r.get('action') or '').lower():
                return False
            if source and source != (r.get('source') or '').lower():
                return False
            if date_str:
                created = r.get('created_at')
                created_date = created.strftime('%Y-%m-%d') if hasattr(created, 'strftime') else str(created)
                if created_date != date_str:
                    return False
            return True
        filtered = [r for r in rows if _match(r)][:limit]
        return {'entries': [{
            'action': r.get('action'), 'entity': r.get('entity'), 'entity_id': r.get('entity_id'),
            'module': r.get('module'), 'actor': r.get('actor'), 'source': r.get('source'),
            'details': r.get('details'),
            'created_at': r['created_at'].strftime('%d.%m.%Y %H:%M') if hasattr(r.get('created_at'), 'strftime') else r.get('created_at'),
        } for r in filtered]}
    if name == 'remember_note':
        note = (args.get('note') or '').strip()
        if not note:
            return {'success': False, 'message': 'Nothing to remember — the note was empty.'}
        existing = get_assistant_memory(username) or ''
        lines = [l.strip() for l in existing.split('\n') if l.strip()]
        lines.append(f'- {note}')
        # Cap how much memory accumulates so this can't grow forever and
        # start crowding out the system prompt — keep the most recent facts.
        lines = lines[-40:]
        save_assistant_memory(username, '\n'.join(lines))
        return {'success': True, 'message': "Got it, I'll remember that."}

    raise ValueError(f'unknown tool: {name}')


ASSISTANT_SYSTEM_PROMPT = """You are the in-app assistant for a small shop's management system (debts, invoices, stock, cash register, items, sellers, salaries, purchase orders, sales). You live in a chat bubble inside the app, so people will naturally also greet you, vent about their day, ask general questions, or chat about things that have nothing to do with the shop — that's welcome, not a distraction. Be a real conversational partner, not a form that only accepts business commands.

You have tools to look up and change the real shop data — always use them instead of guessing or inventing numbers, and never claim an action succeeded unless the matching tool call actually returned success. But most of what someone says to you won't need a tool at all: answer general knowledge questions, give opinions and advice, make small talk, and just be a good listener when that's what's wanted. Only reach for a tool when the person is actually asking about their shop's data or wants you to do something in it.

Personality: warm, understanding, and talkative — genuinely curious about what the person means, quick to pick up on their tone, and happy to chat rather than just execute commands. A little playful and fun, but read the room: if someone's stressed about the shop or venting, meet that with empathy first, not jokes. When this is real business data, get the facts right before being funny about it. Keep replies conversational-length: usually a few sentences for a chat bubble, not an essay — but let yourself write more when someone's asked a real question that deserves it, and don't cut a warm reply short just to be terse.

If a message is ambiguous, ask a short clarifying question instead of guessing or bailing out with a generic "I don't understand" — you're allowed to not be sure yet and just ask. This matters most before you answer with data or change anything: if a name could match more than one client, or you're not confident which record someone means, ask which one they mean rather than silently picking one — a wrong guess here means answering with the wrong person's data or editing the wrong row. But don't ask about something you already know: check the memory notes below and the recent conversation first, and only ask if it's genuinely still missing.

Memory: you have a remember_note tool that persists a short fact about this user/shop across future conversations (not just this one) — a preference, a recurring supplier, a habit, anything worth not having to ask again. Use it in passing, without announcing it or asking permission, whenever something durable and reusable comes up (e.g. they mention they always pay suppliers by card, or that Fridays are their busiest day). Don't overuse it — one memory-worthy fact at a time, and skip anything that's only relevant to this one message. If you're given memory notes about this user below, weave them in naturally; never recite them back as a list unless asked.

Be a little proactive, not just reactive: if a lookup surfaces something clearly worth flagging (stock about to run out, a debt that's been open a long time, a number that looks off), it's fine to mention it briefly alongside the answer — but don't pad every reply with unsolicited observations, and never let a proactive aside distract from answering what was actually asked.

Language: always reply in whichever language the user's latest message is written in (German, English, or Arabic) — switch on the fly, don't stick to whatever language the earlier turns used.

Tool argument formatting: every numeric field (quantity, amount, price, salary, any *_id) must be a plain JSON number — 3, not "3"; 45.5, not "45.5". Never wrap a number in quotes in a tool call, even if the person wrote it as text (e.g. "drei Stück" or "3 Stück" still becomes the number 3). The tool call is rejected outright if a numeric field arrives as a string, so get this right the first time.

Selling something: when someone says they want to sell an item (e.g. "sell an iPhone"), don't jump straight to quick_sell. Walk it like a real checkout, one question at a time, in this order, skipping anything they've already told you: (1) which exact product/variant — if the name is generic or matches several items, call list_items and show the options so they can pick one; (2) quantity; (3) who it's for, i.e. customer name (they can say "no customer" / skip it — that's fine, it's optional); (4) cash or card. Once you have identifier and quantity (customer and payment method are optional but worth asking), restate the full line — product, quantity, price, customer if any, payment method if any — and get a yes before calling quick_sell.

Confirmation rule: before calling any tool that changes data or moves money (add_debt, pay_debt, record_debt_payment, delete_debt, delete_all_debts, update_debt, add_facture, pay_facture, add_kasse_transaction, add_item, update_item, delete_item, add_seller, pay_salary, add_order, quick_sell, edit_sale_item, delete_salary_payment, edit_salary_payment, edit_kasse_transaction, delete_sale, delete_kasse_transaction, edit_seller, delete_seller, edit_order, delete_order, edit_facture, delete_facture, edit_debt_payment, delete_debt_payment, delete_client, rename_client), first restate in plain language exactly what you're about to do (who/what/how much) and ask the user to confirm. Only call the tool once they've clearly said yes in a later message. For delete_all_debts, delete_seller, delete_order, delete_facture, and delete_client specifically, be extra clear that the action is permanent and cannot be undone, since these remove records entirely rather than just changing a status. For delete_all_debts, also state how many open/paid debts exist (via list_open_debts or get_summary) before asking for confirmation, since it is irreversible and affects every client at once. Read-only lookups never need confirmation — just answer."""


# ---------------------------------------------------------------------------
# External API (v1) — lets other systems (a POS, a website, a script, a
# second app) sell items and look up stock the same way the in-app chatbot
# does, without needing a logged-in browser session. Secured with a single
# shared secret (API_KEY in .env) sent as the `X-API-Key` header, since this
# is meant for trusted server-to-server integrations, not public clients.
# ---------------------------------------------------------------------------

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        configured_key = (os.getenv('API_KEY') or '').strip()
        if not configured_key:
            return jsonify({
                'success': False,
                'message': 'The external API is not enabled on this server. Set API_KEY in .env to enable it.',
            }), 503
        provided_key = (request.headers.get('X-API-Key') or '').strip()
        if not provided_key or provided_key != configured_key:
            return jsonify({'success': False, 'message': 'Missing or invalid API key.'}), 401
        return f(*args, **kwargs)
    return decorated


# --- Simple in-memory rate limiter -----------------------------------------
# A small shop app running as a single gunicorn worker doesn't need Redis or
# an extra dependency for this — a per-key sliding window in a dict is
# enough to stop a runaway script or bug from hammering the paid OpenAI
# call or the external sell API. NOTE: this resets per process, so it does
# NOT enforce a global limit across multiple gunicorn workers/dynos; if you
# scale to more than one worker, switch to Flask-Limiter with a shared
# Redis backend instead.
import threading
from collections import deque

_rate_limit_buckets = {}
_rate_limit_lock = threading.Lock()


def rate_limit(max_requests=30, window_seconds=60, key_func=None):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            key = key_func() if key_func else request.remote_addr or 'unknown'
            bucket_key = f'{f.__name__}:{key}'
            now = datetime.now().timestamp()
            with _rate_limit_lock:
                bucket = _rate_limit_buckets.setdefault(bucket_key, deque())
                while bucket and now - bucket[0] > window_seconds:
                    bucket.popleft()
                if len(bucket) >= max_requests:
                    retry_after = int(window_seconds - (now - bucket[0])) + 1
                    return jsonify({
                        'success': False,
                        'message': f'Rate limit exceeded. Try again in {retry_after}s.',
                    }), 429, {'Retry-After': str(retry_after)}
                bucket.append(now)
            return f(*args, **kwargs)
        return wrapped
    return decorator


@app.route('/admin/audit-log')
@login_required('admin')
def admin_audit_log():
    entity_filter = (request.args.get('entity') or '').strip() or None
    module_filter = (request.args.get('module') or '').strip() or None
    entries = get_audit_log(limit=300, entity=entity_filter, module=module_filter)
    return render_template('audit_log.html', entries=entries, entity_filter=entity_filter, module_filter=module_filter)


@app.route('/api/v1/health', methods=['GET'])
def api_v1_health():
    """Unauthenticated health/status endpoint — for uptime monitors and load
    balancers. Reports DB connectivity and whether the AI assistant and
    external sell API are configured, without leaking secrets."""
    db_ok = True
    db_error = None
    try:
        fetch_one("SELECT 1 AS ok;")
    except Exception as e:
        db_ok = False
        db_error = str(e)

    return jsonify({
        'status': 'ok' if db_ok else 'degraded',
        'database': 'ok' if db_ok else 'unreachable',
        'database_error': db_error if not db_ok else None,
        'assistant_configured': get_openai_client() is not None,
        'external_api_configured': bool((os.getenv('API_KEY') or '').strip()),
        'ocr_available': TESSERACT_AVAILABLE,
        'time': datetime.utcnow().isoformat() + 'Z',
    }), (200 if db_ok else 503)


@app.route('/api/v1/items', methods=['GET'])
@require_api_key
@rate_limit(max_requests=60, window_seconds=60,
            key_func=lambda: request.headers.get('X-API-Key', request.remote_addr))
def api_v1_items():
    """Look up items by (partial) name, barcode, or SKU — GET /api/v1/items?q=cola
    Reuses search_items() (db.py), the same search used by the Inventory
    page and the AI chat assistant, so results are consistent everywhere
    and matching happens at the database level instead of scanning every
    row in Python.
    """
    query = (request.args.get('q') or '').strip()
    items = search_items(query, limit=50)
    out = [{
        'barcode': i.get('barcode'),
        'sku': i.get('sku'),
        'product_name': i.get('product_name'),
        'quantity': int(i.get('quantity') or 0),
        'selling_price': float(i.get('selling_price') or 0),
    } for i in items]
    return jsonify({'success': True, 'items': out})


@app.route('/api/v1/sell', methods=['POST'])
@require_api_key
@rate_limit(max_requests=60, window_seconds=60,
            key_func=lambda: request.headers.get('X-API-Key', request.remote_addr))
def api_v1_sell():
    """Record a one-line sale from an external system.

    Body: {"identifier": "<barcode or product name>", "quantity": 1}
    identifier may be an exact barcode or a (partial, case-insensitive)
    product name — same matching rules as the chatbot's quick sale.
    """
    data = request.get_json(silent=True) or {}
    identifier = (data.get('identifier') or '').strip()
    quantity = data.get('quantity', 1)

    if not identifier:
        return jsonify({'success': False, 'message': 'identifier is required.'}), 400
    try:
        quantity = int(quantity)
        if quantity <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'quantity must be a positive whole number.'}), 400

    try:
        result = _quick_sell_record(
            identifier, quantity, username='api',
            customer_name=data.get('customer_name'), payment_method=data.get('payment_method'),
            sale_price=data.get('sale_price'),
        )
    except LookupError as e:
        return jsonify({'success': False, 'message': str(e)}), 404
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except Exception:
        logger.exception('External API sell failed')
        return jsonify({'success': False, 'message': 'Server error while recording the sale.'}), 500

    log_audit('assistant_sale', 'sale', result.get('sale_id') if isinstance(result, dict) else None,
               format_sale_details(result.get('product_name'), result.get('quantity'), None,
                                    result.get('total_price'), result.get('payment_method'), result.get('customer_name')),
               actor='api', source='external_api', module='sales')
    return jsonify({'success': True, **result})


@app.route('/api/v1/items', methods=['POST'])
@require_api_key
@rate_limit(max_requests=60, window_seconds=60,
            key_func=lambda: request.headers.get('X-API-Key', request.remote_addr))
def api_v1_add_item():
    """Create a new product from an external system.

    Body: {"product_name": "...", "quantity": 0, "purchase_price": 0,
           "selling_price": 0, "barcode": "<optional>", "sku": "<optional>"}
    Reuses _add_item_record — same validation/uniqueness rules as the
    Inventory page and the AI chat assistant's add_item tool.
    """
    data = request.get_json(silent=True) or {}
    name = (data.get('product_name') or '').strip()
    if not name:
        return jsonify({'success': False, 'message': 'product_name is required.'}), 400
    try:
        quantity = int(data.get('quantity') or 0)
        purchase_price = float(data.get('purchase_price') or 0)
        selling_price = float(data.get('selling_price') or 0)
        min_selling_price = float(data.get('min_selling_price') or selling_price)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'quantity/prices must be numbers.'}), 400

    try:
        barcode_value = _add_item_record(
            name, quantity, purchase_price, selling_price, min_selling_price,
            data.get('barcode'), data.get('sku'), data.get('description') or '',
        )
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except Exception:
        logger.exception('External API failed to add item')
        return jsonify({'success': False, 'message': 'Server error while adding the item.'}), 500

    log_audit('product_created', 'item', barcode_value,
               format_product_details(name, barcode_value, data.get('sku')),
               actor='api', source='external_api', module='inventory')
    return jsonify({'success': True, 'barcode': barcode_value, 'product_name': name})


@app.route('/api/v1/items/<path:identifier>', methods=['PUT'])
@require_api_key
@rate_limit(max_requests=60, window_seconds=60,
            key_func=lambda: request.headers.get('X-API-Key', request.remote_addr))
def api_v1_update_item(identifier):
    """Update a product (exact barcode, exact SKU, or a name to search
    for). Only fields present in the JSON body are changed."""
    data = request.get_json(silent=True) or {}
    try:
        result = _update_item_record(
            identifier,
            product_name=data.get('product_name'),
            quantity=data.get('quantity'),
            purchase_price=data.get('purchase_price'),
            selling_price=data.get('selling_price'),
            min_selling_price=data.get('min_selling_price'),
            barcode_value=data.get('barcode'),
            sku=data.get('sku'),
            description=data.get('description'),
        )
    except LookupError as e:
        return jsonify({'success': False, 'message': str(e)}), 404
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except Exception:
        logger.exception('External API failed to update item %s', identifier)
        return jsonify({'success': False, 'message': 'Server error while updating the item.'}), 500

    log_audit('product_updated', 'item', result['barcode'],
               format_product_details(result.get('product_name'), result.get('barcode'), result.get('sku')),
               actor='api', source='external_api', module='inventory')
    return jsonify({'success': True, 'item': result})


@app.route('/api/v1/items/<path:identifier>', methods=['DELETE'])
@require_api_key
@rate_limit(max_requests=60, window_seconds=60,
            key_func=lambda: request.headers.get('X-API-Key', request.remote_addr))
def api_v1_delete_item(identifier):
    """Delete a product (exact barcode, exact SKU, or a name to search for)."""
    try:
        result = _delete_item_record(identifier)
    except LookupError as e:
        return jsonify({'success': False, 'message': str(e)}), 404
    except Exception:
        logger.exception('External API failed to delete item %s', identifier)
        return jsonify({'success': False, 'message': 'Server error while deleting the item.'}), 500

    log_audit('product_deleted', 'item', result['barcode'],
               format_product_details(result.get('product_name'), result.get('barcode')),
               actor='api', source='external_api', module='inventory')
    return jsonify({'success': True, 'barcode': result['barcode'], 'product_name': result.get('product_name')})





# How many of the most recent turns are always sent to the model verbatim.
# Anything older than this used to just fall off the end of a hard
# [-20:] slice and be gone — fine for a short chat, but it meant a
# long-running conversation quietly "forgot" anything before the last ~10
# exchanges, with no warning to the user. _get_or_update_history_summary
# below compresses that older part into a few bullet points instead of
# dropping it, so long conversations degrade gracefully instead of abruptly.
RECENT_RAW_TURNS = 16
# Only refresh the summary once this many additional turns have rolled off
# since it was last generated — summarizing on every single message would
# double the number of LLM calls for no real benefit once the summary is
# already reasonably fresh.
RESUMMARIZE_EVERY = 10


def _get_or_update_history_summary(username, history, client, model):
    """Returns (summary_text_or_None, recent_turns) for `history`.

    `recent_turns` is the tail of `history` that should still be sent
    verbatim; anything before that is represented (if there's enough of it)
    by a short cached summary instead of being silently truncated away.
    """
    if len(history) <= RECENT_RAW_TURNS:
        return None, history

    older = history[:-RECENT_RAW_TURNS]
    recent = history[-RECENT_RAW_TURNS:]

    cached_summary, cached_covered = get_assistant_summary(username)
    if cached_summary and (len(older) - cached_covered) < RESUMMARIZE_EVERY:
        # Fresh enough — reuse it rather than spend another LLM call.
        return cached_summary, recent

    if client is None:
        return cached_summary, recent

    convo_text = '\n'.join(
        f"{t.get('role')}: {t.get('content')}" for t in older if t.get('content')
    )[:8000]
    if not convo_text:
        return cached_summary, recent

    try:
        completion = client.chat.completions.create(
            model=model, temperature=0, max_tokens=220,
            messages=[
                {'role': 'system', 'content': (
                    'Summarize this older part of a shop-assistant conversation in 3-5 short, '
                    'plain bullet points: durable facts mentioned, anything still open/unresolved, '
                    'and anything pending confirmation. No preamble, no restating the question.')},
                {'role': 'user', 'content': convo_text},
            ],
        )
        summary_text = (completion.choices[0].message.content or '').strip()
    except Exception:
        logger.exception('Assistant history summarization failed; reusing previous summary')
        return cached_summary, recent

    if summary_text:
        save_assistant_summary(username, summary_text, len(older))
        return summary_text, recent
    return cached_summary, recent


def _run_assistant_chat(user_message, history, username, lang=None):
    """Shared assistant loop used by both the in-app widget
    (/assistant/api/chat, cookie-session auth) and the external API
    (/api/v1/assistant/chat, X-API-Key auth). Returns (status_code, payload).
    `lang` is the chat UI's current language ('de'/'en'/'ar'), used only to
    pick which language the *fallback* error messages below are shown in —
    the model's own replies already follow whatever language the user
    wrote in, per the system prompt.
    """
    lang = lang if lang in ('de', 'en', 'ar') else 'en'
    client = get_openai_client()
    if client is None:
        return 503, {'success': False, 'message': _llm_unavailable_message()}

    if not user_message:
        return 400, {'success': False, 'message': 'Empty message.'}

    model = (_get_llm_provider_config() or {}).get('chat_model', 'gpt-4o-mini')

    # OpenAI's chat.completions API has no top-level `system=` parameter
    # (that's an Anthropic Messages API concept). The system prompt has to
    # be the first message in the list with role 'system', or the API
    # rejects the whole request. This was left over from the Anthropic ->
    # OpenAI migration and made every single chat request fail.
    system_prompt = ASSISTANT_SYSTEM_PROMPT
    memory_notes = get_assistant_memory(username)
    if memory_notes:
        system_prompt += (
            "\n\nThings you've learned about this user/shop from earlier conversations "
            f"(use naturally, don't recite as a list unless asked):\n{memory_notes}"
        )
    messages = [{'role': 'system', 'content': system_prompt}]

    # Anything older than the last RECENT_RAW_TURNS is compressed into a
    # short summary instead of being silently dropped — see
    # _get_or_update_history_summary above.
    summary_text, recent_turns = _get_or_update_history_summary(username, history or [], client, model)
    if summary_text:
        messages.append({'role': 'system', 'content': f'Summary of earlier conversation with this user:\n{summary_text}'})

    # OpenAI tool-calling expects a flat message list.
    # We keep only string content for chat turns.
    for turn in recent_turns:
        role = turn.get('role')
        content = turn.get('content')
        if role in ('user', 'assistant') and isinstance(content, str) and content:
            messages.append({'role': role, 'content': content})
    messages.append({'role': 'user', 'content': user_message})

    # Tool names whose result is worth rendering as a chat widget (card grid /
    # table / small chart) instead of only the AI's prose summary of it — see
    # WIDGETABLE_TOOLS below and renderWidget() in assistant.js. A single
    # turn can trigger more than one widget-worthy lookup (e.g. checking
    # stock AND recent sales), so all of them are collected and rendered,
    # not just the last one.
    widgets = []

    # Tracks whether a write action (add debt, record a sale, book a
    # kasse entry, ...) already succeeded and was committed to the DB
    # *this turn*. This is what actually matters to the user — if it's
    # True, a later failure (e.g. the model choking while composing its
    # follow-up sentence, or a flaky provider on the next round-trip)
    # must not be reported as "something went wrong", since the requested
    # operation did in fact go through. Without this, an exception raised
    # after a successful tool call was being surfaced as a generic error
    # even though nothing actually failed.
    completed_write = None  # will hold {'name': ..., 'result': ...} once a write succeeds

    try:
        # Up to 6 tool-iterations (same as the previous Anthropic loop).
        for _ in range(6):
            completion = _create_completion_with_schema_retry(
                client,
                model=model,
                temperature=0.3,
                max_tokens=1024,
                messages=messages,
                tools=[{
                    'type': 'function',
                    'function': {
                        'name': t['name'],
                        'description': t.get('description', ''),
                        'parameters': t.get('input_schema', {'type': 'object', 'properties': {}}),
                    }
                } for t in ASSISTANT_TOOLS],
                tool_choice='auto',
            )

            msg = completion.choices[0].message

            # If the model didn't request tool calls, it's a normal assistant reply.
            if not getattr(msg, 'tool_calls', None):
                reply = (getattr(msg, 'content', None) or '').strip()
                payload = {'success': True, 'reply': reply or '...'}
                if widgets:
                    payload['widgets'] = widgets
                    payload['widget'] = widgets[-1]  # kept for older cached frontends
                return 200, payload

            tool_results = []
            for tool_call in msg.tool_calls:
                name = tool_call.function.name
                args = tool_call.function.arguments
                # arguments is a JSON string
                try:
                    parsed_args = json.loads(args) if isinstance(args, str) else (args or {})
                except Exception:
                    parsed_args = {}

                try:
                    result = _execute_assistant_tool(name, parsed_args, username)
                except PermissionError as e:
                    result = {'success': False, 'message': str(e)}
                except (ValueError, LookupError) as e:
                    result = {'success': False, 'message': str(e)}

                if name in ASSISTANT_WRITE_TOOLS and isinstance(result, dict) and result.get('success', True):
                    action_name, entity_name, module_name, details_text = _describe_assistant_action(name, parsed_args, result)
                    log_audit(action_name, entity_name, None, details_text, actor=username, source='assistant', module=module_name)
                    completed_write = {'name': name, 'result': result}

                if name in WIDGETABLE_TOOLS and isinstance(result, (dict, list)):
                    widgets.append({'type': name, 'data': result})

                tool_results.append({
                    'role': 'tool',
                    'tool_call_id': tool_call.id,
                    'content': json.dumps(result, default=str, ensure_ascii=False),
                })

            # Append the assistant tool-call message and then tool outputs.
            messages.append({
                'role': 'assistant',
                'content': msg.content or '',
                'tool_calls': [
                    {
                        'id': tc.id,
                        'type': 'function',
                        'function': {'name': tc.function.name, 'arguments': tc.function.arguments},
                    } for tc in msg.tool_calls
                ],
            })
            messages.extend(tool_results)

        return 200, {'success': True, 'reply': _fallback_message('too_many_steps', lang)}

    except Exception as e:
        # Always log the full exception server-side first, regardless of
        # what we end up telling the user — this is what makes the failure
        # diagnosable later even when we decide below that it isn't
        # actually an error from the user's point of view.
        logger.exception('Assistant chat loop raised an exception: %s', str(e))

        # The requested action (add debt / record sale / book kasse entry /
        # ...) already succeeded and was committed to the database before
        # this exception happened — e.g. the model choked while composing
        # its follow-up sentence, or the next round-trip to the LLM
        # provider timed out. That is NOT a failure of the user's request,
        # so don't show "something went wrong" for something that, in
        # fact, went right. Confirm the completed action instead.
        if completed_write is not None:
            result = completed_write['result'] if isinstance(completed_write['result'], dict) else {}
            reply = result.get('message') or 'Done — that was saved successfully.'
            payload = {'success': True, 'reply': reply}
            if widgets:
                payload['widgets'] = widgets
                payload['widget'] = widgets[-1]
            return 200, payload

        # Improve handling for the common OpenAI failure mode seen in logs:
        # 429 insufficient_quota. This shouldn't be shown as "unreachable".
        try:
            from openai import RateLimitError  # available when OpenAI SDK is installed
            if isinstance(e, RateLimitError):
                # Most reliable signal across SDK versions is the exception string.
                if 'insufficient_quota' in str(e):
                    message = _fallback_message('quota_exceeded', lang)
                else:
                    message = _fallback_message('rate_limited', lang)

                return 502, {'success': False, 'message': message}
        except Exception:
            # Fall through to generic handler below
            pass

        message = _fallback_message('unreachable', lang)
        # In debug mode, append the real exception so this is diagnosable
        # from the browser instead of requiring server log access. Never
        # do this in production (could leak internal details).
        if app.debug:
            message += f' [debug: {type(e).__name__}: {e}]'
        return 502, {'success': False, 'message': message}


def _run_assistant_chat_stream(user_message, history, username, lang=None):
    """Streaming twin of _run_assistant_chat: same system prompt, same
    memory/summary context, same tool-calling loop — but the model's final
    reply is yielded token-by-token as Server-Sent Events instead of only
    being returned once the whole thing is done, and a lightweight
    `status` event is sent while a tool is running (e.g. "checking your
    stock...") instead of the chat sitting on a silent spinner. Older
    clients can keep using /assistant/api/chat (non-streaming) unchanged.

    Yields raw SSE-formatted strings ("event: ...\\ndata: ...\\n\\n").
    The final event is always either `done` (payload has `reply`, and
    `widgets`/`widget` if any) or `error` (payload has `message`). `lang`
    picks the language of the *fallback* messages only — see
    _run_assistant_chat for the full explanation.
    """
    lang = lang if lang in ('de', 'en', 'ar') else 'en'

    def sse(event, data):
        return f"event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"

    client = get_openai_client()
    if client is None:
        yield sse('error', {'message': _llm_unavailable_message()})
        return
    if not user_message:
        yield sse('error', {'message': 'Empty message.'})
        return

    model = (_get_llm_provider_config() or {}).get('chat_model', 'gpt-4o-mini')

    system_prompt = ASSISTANT_SYSTEM_PROMPT
    memory_notes = get_assistant_memory(username)
    if memory_notes:
        system_prompt += (
            "\n\nThings you've learned about this user/shop from earlier conversations "
            f"(use naturally, don't recite as a list unless asked):\n{memory_notes}"
        )
    messages = [{'role': 'system', 'content': system_prompt}]

    summary_text, recent_turns = _get_or_update_history_summary(username, history or [], client, model)
    if summary_text:
        messages.append({'role': 'system', 'content': f'Summary of earlier conversation with this user:\n{summary_text}'})
    for turn in recent_turns:
        role = turn.get('role')
        content = turn.get('content')
        if role in ('user', 'assistant') and isinstance(content, str) and content:
            messages.append({'role': role, 'content': content})
    messages.append({'role': 'user', 'content': user_message})

    widgets = []
    completed_write = None

    try:
        for _ in range(6):
            stream = _create_completion_with_schema_retry(
                client,
                model=model,
                temperature=0.3,
                max_tokens=1024,
                messages=messages,
                tools=[{
                    'type': 'function',
                    'function': {
                        'name': t['name'],
                        'description': t.get('description', ''),
                        'parameters': t.get('input_schema', {'type': 'object', 'properties': {}}),
                    }
                } for t in ASSISTANT_TOOLS],
                tool_choice='auto',
                stream=True,
            )

            content_parts = []
            # Tool-call fragments stream in piecemeal (name/arguments arrive
            # split across many chunks, keyed by their position in the
            # list) — accumulate by index before parsing/executing, same
            # approach as OpenAI's own streaming + function-calling docs.
            tool_calls_acc = {}

            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    content_parts.append(delta.content)
                    yield sse('token', {'text': delta.content})
                if delta and getattr(delta, 'tool_calls', None):
                    for tc_delta in delta.tool_calls:
                        acc = tool_calls_acc.setdefault(tc_delta.index, {'id': None, 'name': '', 'arguments': ''})
                        if tc_delta.id:
                            acc['id'] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                acc['name'] += tc_delta.function.name
                            if tc_delta.function.arguments:
                                acc['arguments'] += tc_delta.function.arguments

            if not tool_calls_acc:
                reply = ''.join(content_parts).strip()
                payload = {'reply': reply or '...'}
                if widgets:
                    payload['widgets'] = widgets
                    payload['widget'] = widgets[-1]
                yield sse('done', payload)
                return

            tool_calls_sorted = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
            tool_results = []
            for tc in tool_calls_sorted:
                name = tc['name']
                yield sse('status', {'tool': name})
                try:
                    parsed_args = json.loads(tc['arguments']) if tc['arguments'] else {}
                except Exception:
                    parsed_args = {}

                try:
                    result = _execute_assistant_tool(name, parsed_args, username)
                except PermissionError as e:
                    result = {'success': False, 'message': str(e)}
                except (ValueError, LookupError) as e:
                    result = {'success': False, 'message': str(e)}

                if name in ASSISTANT_WRITE_TOOLS and isinstance(result, dict) and result.get('success', True):
                    action_name, entity_name, module_name, details_text = _describe_assistant_action(name, parsed_args, result)
                    log_audit(action_name, entity_name, None, details_text, actor=username, source='assistant', module=module_name)
                    completed_write = {'name': name, 'result': result}

                if name in WIDGETABLE_TOOLS and isinstance(result, (dict, list)):
                    widget = {'type': name, 'data': result}
                    widgets.append(widget)
                    yield sse('widget', widget)

                tool_results.append({
                    'role': 'tool',
                    'tool_call_id': tc['id'],
                    'content': json.dumps(result, default=str, ensure_ascii=False),
                })

            messages.append({
                'role': 'assistant',
                'content': ''.join(content_parts) or None,
                'tool_calls': [
                    {'id': tc['id'], 'type': 'function',
                     'function': {'name': tc['name'], 'arguments': tc['arguments']}}
                    for tc in tool_calls_sorted
                ],
            })
            messages.extend(tool_results)

        yield sse('done', {'reply': _fallback_message('too_many_steps', lang)})

    except Exception as e:
        logger.exception('Streaming assistant chat loop raised an exception: %s', str(e))
        if completed_write is not None:
            result = completed_write['result'] if isinstance(completed_write['result'], dict) else {}
            reply = result.get('message') or 'Done — that was saved successfully.'
            payload = {'reply': reply}
            if widgets:
                payload['widgets'] = widgets
                payload['widget'] = widgets[-1]
            yield sse('done', payload)
            return

        try:
            from openai import RateLimitError
            if isinstance(e, RateLimitError):
                if 'insufficient_quota' in str(e):
                    message = _fallback_message('quota_exceeded', lang)
                else:
                    message = _fallback_message('rate_limited', lang)
                yield sse('error', {'message': message})
                return
        except Exception:
            pass

        message = _fallback_message('unreachable', lang)
        if app.debug:
            message += f' [debug: {type(e).__name__}: {e}]'
        yield sse('error', {'message': message})


@app.route('/assistant/app')
@login_required(['admin', 'seller'])
def assistant_app():
    """The standalone, chat-only installable app.

    This is the app clients put on their phone's home screen — for many
    shops it's the *only* app they'll ever open, so it's available to both
    admin and seller accounts (same per-tool permission split already
    enforced in /assistant/api/chat via seller_allowed_tools() — a seller
    installing this doesn't get more access than they had in the
    dashboard's chat widget, just the same assistant full-screen with its
    own icon). Installed via the browser's "Add to Home Screen" (Android:
    automatic prompt; iOS: Share -> Add to Home Screen).
    """
    return render_template('chat_app.html')


@app.route('/assistant/api/chat', methods=['POST'])
@login_required(['admin', 'seller'])
@rate_limit(max_requests=30, window_seconds=60, key_func=lambda: session.get('username', request.remote_addr))
def assistant_chat():
    data = request.get_json(silent=True) or {}
    user_message = (data.get('message') or '').strip()
    username = session.get('username')
    lang = (data.get('lang') or '').strip()[:5] or None
    # Which GPT-style conversation this turn belongs to. Optional for
    # backward compatibility with any older cached frontend that doesn't
    # send it yet — falls back to that user's most recent conversation.
    conversation_id = (data.get('conversation_id') or '').strip()[:36] or None

    # Server-side history is now the source of truth (see db.py — "Assistant
    # chat history"). A client-sent `history` array is still accepted as a
    # fallback for older cached frontends, but normal operation loads the
    # last turns straight from the database instead, so the conversation
    # survives page reloads, storage clears, and different devices.
    history = [
        {'role': row['role'], 'content': row['content']}
        for row in get_assistant_history(username, limit=40, conversation_id=conversation_id)
    ] or (data.get('history') or [])

    status, payload = _run_assistant_chat(user_message, history, username, lang=lang)

    if user_message:
        save_assistant_message(username, 'user', user_message, lang, conversation_id=conversation_id)
    if status == 200 and payload.get('success') and payload.get('reply'):
        save_assistant_message(username, 'assistant', payload['reply'], lang, conversation_id=conversation_id)

    return jsonify(payload), status


@app.route('/assistant/api/chat/stream', methods=['POST'])
@login_required(['admin', 'seller'])
@rate_limit(max_requests=30, window_seconds=60, key_func=lambda: session.get('username', request.remote_addr))
def assistant_chat_stream():
    """Streaming twin of /assistant/api/chat (same assistant, same tools,
    same history/memory) — sends the reply as Server-Sent Events so the
    chat can render it token-by-token instead of waiting for the whole
    thing, the same way ChatGPT/Claude's own UIs feel responsive instead
    of sitting on a silent spinner for a few seconds.
    """
    data = request.get_json(silent=True) or {}
    user_message = (data.get('message') or '').strip()
    username = session.get('username')
    lang = (data.get('lang') or '').strip()[:5] or None
    conversation_id = (data.get('conversation_id') or '').strip()[:36] or None

    history = [
        {'role': row['role'], 'content': row['content']}
        for row in get_assistant_history(username, limit=40, conversation_id=conversation_id)
    ] or (data.get('history') or [])

    if user_message:
        save_assistant_message(username, 'user', user_message, lang, conversation_id=conversation_id)

    def generate():
        final_reply = None
        for event_str in _run_assistant_chat_stream(user_message, history, username, lang=lang):
            if event_str.startswith('event: done') or event_str.startswith('event: error'):
                try:
                    data_line = next(l for l in event_str.split('\n') if l.startswith('data: '))
                    payload = json.loads(data_line[len('data: '):])
                    if payload.get('reply'):
                        final_reply = payload['reply']
                except Exception:
                    pass
            yield event_str
        if final_reply:
            save_assistant_message(username, 'assistant', final_reply, lang, conversation_id=conversation_id)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/assistant/api/history', methods=['GET'])
@login_required('admin')
def assistant_history():
    """Load this user's saved conversation — called once when the chat panel
    opens so a reload / new device picks up where the person left off.
    Pass ?conversation_id=... to load a specific past conversation instead
    of the most recent one (see /assistant/api/conversations). Always
    returns the resolved conversation_id so the client knows which
    conversation subsequent messages should be saved into."""
    username = session.get('username')
    conversation_id = (request.args.get('conversation_id') or '').strip()[:36] or None
    if not conversation_id:
        convos = list_assistant_conversations(username, limit=1)
        conversation_id = convos[0]['conversation_id'] if convos else str(uuid.uuid4())
    rows = get_assistant_history(username, limit=40, conversation_id=conversation_id)
    return jsonify({
        'success': True,
        'conversation_id': conversation_id,
        'history': [{'role': r['role'], 'content': r['content']} for r in rows],
    })


@app.route('/assistant/api/conversations', methods=['GET'])
@login_required('admin')
def assistant_conversations():
    """List this user's past conversations, most-recently-active first —
    the data behind the chat's GPT-style history sidebar."""
    username = session.get('username')
    conversations = list_assistant_conversations(username)
    out = []
    for c in conversations:
        last = c['last_message_at']
        out.append({
            'conversation_id': c['conversation_id'],
            'title': c['title'],
            'last_message_at': last.isoformat() if hasattr(last, 'isoformat') else str(last) if last else None,
            'message_count': c['message_count'],
        })
    return jsonify({'success': True, 'conversations': out})


@app.route('/assistant/api/conversations/new', methods=['POST'])
@login_required('admin')
def assistant_conversations_new():
    """Mints a fresh conversation_id for a 'New chat' action. Nothing is
    written to the database until the first message is actually sent in
    it, so starting-then-abandoning a new chat doesn't clutter the list."""
    return jsonify({'success': True, 'conversation_id': str(uuid.uuid4())})


@app.route('/assistant/api/conversations/<conversation_id>/delete', methods=['POST'])
@login_required('admin')
def assistant_conversations_delete(conversation_id):
    username = session.get('username')
    delete_assistant_conversation(username, conversation_id)
    return jsonify({'success': True})


@app.route('/assistant/api/history/clear', methods=['POST'])
@login_required('admin')
def assistant_history_clear():
    """Wipe this user's saved conversation (the chat's 'start over' action)."""
    clear_assistant_history(session.get('username'))
    return jsonify({'success': True})


@app.route('/assistant/api/memory', methods=['GET'])
@login_required('admin')
def assistant_memory_get():
    """Show what the assistant currently remembers about this user/shop —
    lets the person audit or sanity-check it rather than it being an
    invisible black box."""
    notes = get_assistant_memory(session.get('username'))
    return jsonify({'success': True, 'notes': notes})


@app.route('/assistant/api/memory/clear', methods=['POST'])
@login_required('admin')
def assistant_memory_clear():
    """Deliberately wipe the assistant's durable memory of this user —
    separate from clearing chat history, since memory is meant to survive
    a 'start over' by default (see clear_assistant_history in db.py)."""
    clear_assistant_memory(session.get('username'))
    return jsonify({'success': True})


@app.route('/assistant/api/transcribe', methods=['POST'])
@login_required('admin')
@rate_limit(max_requests=20, window_seconds=60, key_func=lambda: session.get('username', request.remote_addr))
def assistant_transcribe():
    """Speech-to-text for the chat widget's mic button.

    The previous implementation relied entirely on the browser's Web Speech
    API (webkitSpeechRecognition), which only works reliably in Chrome,
    needs a live connection to Google's own speech servers regardless of
    your OPENAI_API_KEY, and is unsupported or flaky in a lot of
    mobile/embedded browsers. That is exactly why voice input worked for
    some people/devices and not others.

    This endpoint replaces that with server-side transcription, the same
    approach ChatGPT's voice input uses: the browser just records raw audio
    (MediaRecorder, works everywhere) and uploads it here, and the server
    transcribes it with an OpenAI-compatible Whisper model. Works on any
    browser/device, independent of Web Speech API support.
    """
    client = get_openai_client()
    if client is None:
        return jsonify({'success': False, 'message': _llm_unavailable_message()}), 503

    audio_file = request.files.get('audio')
    if not audio_file:
        return jsonify({'success': False, 'message': 'No audio file received.'}), 400

    lang = (request.form.get('lang') or '').strip()[:2] or None
    audio_model = (_get_llm_provider_config() or {}).get('audio_model', 'whisper-1')

    try:
        # The SDK needs a (filename, filedata, content_type) tuple, not the
        # raw Werkzeug FileStorage, to reliably pass content-type through.
        file_tuple = (audio_file.filename or 'audio.webm', audio_file.stream, audio_file.mimetype or 'audio/webm')
        kwargs = {'model': audio_model, 'file': file_tuple}
        detected_lang = None
        if lang:
            # The client only sends this when the person explicitly chose a
            # language (flag chips) — forcing a language Whisper wasn't
            # actually given correctly makes it worse, not better, so we
            # only do this on a real signal.
            kwargs['language'] = lang
        else:
            # No forced hint: ask for verbose_json purely to get Whisper's
            # own detected language back, so the chat UI can sync to
            # whatever was actually spoken instead of guessing beforehand.
            kwargs['response_format'] = 'verbose_json'
        try:
            transcript = client.audio.transcriptions.create(**kwargs)
        except Exception:
            if kwargs.get('response_format') == 'verbose_json':
                # Some OpenAI-compatible providers don't support verbose_json
                # on this endpoint — retry without it rather than failing
                # the whole request; we just won't get a detected_lang back.
                kwargs.pop('response_format', None)
                transcript = client.audio.transcriptions.create(**kwargs)
            else:
                raise
        text = (getattr(transcript, 'text', None) or '').strip()
        detected_lang = getattr(transcript, 'language', None)
        # Whisper's verbose_json reports full language names (e.g.
        # "english"), not ISO codes — normalize to what the frontend expects.
        lang_name_to_code = {'german': 'de', 'english': 'en', 'arabic': 'ar'}
        if detected_lang:
            detected_lang = lang_name_to_code.get(str(detected_lang).lower(), str(detected_lang)[:2])
        return jsonify({'success': True, 'text': text, 'detected_lang': detected_lang})
    except Exception as e:
        logger.exception('Assistant transcribe failed')
        message = 'Voice transcription failed. Please try again or type instead.'
        if app.debug:
            message += f' [debug: {type(e).__name__}: {e}]'
        # Common cause worth surfacing: some OpenAI-compatible providers set
        # via OPENAI_BASE_URL (proxies, some free tiers) don't implement the
        # /audio/transcriptions endpoint at all, only /chat/completions.
        return jsonify({'success': False, 'message': message}), 502


@app.route('/api/v1/assistant/chat', methods=['POST'])
@require_api_key
@rate_limit(max_requests=30, window_seconds=60,
            key_func=lambda: request.headers.get('X-API-Key', request.remote_addr))
def api_v1_assistant_chat():
    """Same assistant, reachable without a logged-in browser session.

    This is what the standalone downloadable chat app (and any other
    external client — a kiosk, a second app, a script) talks to, the same
    way /api/v1/sell and /api/v1/items already let external systems record
    sales and look up stock. Secured with the same X-API-Key header/shared
    secret (set API_KEY in .env to enable it).

    Body: {"message": "...", "history": [{"role": "user"|"assistant", "content": "..."}], "user_id": "optional-caller-id"}
    `user_id` is optional but recommended when multiple external callers share
    one API_KEY — it keeps each caller's server-side chat history separate
    (falls back to a shared "api" bucket if omitted).
    """
    data = request.get_json(silent=True) or {}
    user_message = (data.get('message') or '').strip()
    caller = f"api:{data.get('user_id')}" if data.get('user_id') else 'api'
    history = [
        {'role': row['role'], 'content': row['content']}
        for row in get_assistant_history(caller, limit=40)
    ] or (data.get('history') or [])
    status, payload = _run_assistant_chat(user_message, history, caller)
    if user_message:
        save_assistant_message(caller, 'user', user_message)
    if status == 200 and payload.get('success'):
        if payload.get('reply'):
            save_assistant_message(caller, 'assistant', payload['reply'])
        log_audit('assistant_chat_message', 'chat', None,
                   f'External API chat message: "{user_message[:200]}"',
                   actor='api', source='external_api', module='assistant')
    return jsonify(payload), status


def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(calculate_and_save_today_closing_balance, 'cron', hour=23, minute=59)
    scheduler.start()


if __name__ == '__main__':
    
    # Ensure initial admin user exists
    users = load_users()
    # Start scheduler when Flask app starts
    start_scheduler()
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv('PORT', '5001')))



