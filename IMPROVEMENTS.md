# Refund Feature — Code Quality & Maintainability Improvements

## Information Gathered

After thoroughly reading `app.py`, `db.py`, `sales-refund.js`, `admin_sales.html`, and `translations.py`:

### Key Observations

**app.py (Refund Routes)**:
- `process_refund()` (line ~4160) and `assistant_process_refund()` (line ~4280) share ~80% identical validation/business logic — duplicated sale lookup, item validation, qty checks, total calculation, Stripe refund processing
- Error messages are hardcoded in German with `\u201e`/`\u201c` unicode escape sequences, making the code hard to read
- No translations used for error messages (not using the `TR` dict)
- The routes validate and process refunds inline rather than delegating to a shared business-logic function

**db.py (Refund Functions)**:
- `record_refund()` — solid: handles DB insert, stock restoration, Kasse withdrawal logging
- `load_sales_with_refunds()` — good optimization, avoids N+1 queries
- `stripe_refund_payment()` — has a local `import stripe` inside the function body instead of using the module-level `stripe` import

**sales-refund.js**:
- Modal DOM is built via template strings — works but is hard to maintain/extend
- `stripeRefundClient` variable initialized to `null` but never actually assigned/used
- Card refund flow shows an `alert()` instead of properly integrating with the Stripe Terminal JS SDK
- No loading state / spinner during AJAX calls beyond disabling the submit button
- Uses `XMLHttpRequest` instead of `fetch()` (modern, cleaner)

**admin_sales.html**:
- i18n injection is thorough and well-structured
- `openRefundModal` properly escapes JSON with `tojson | safe`
- Refund summary already displayed per sale card

---

## Improvement Plan

### 1. Extract shared refund logic in `app.py`

**Problem**: `process_refund()` and `assistant_process_refund()` duplicate sale lookup, item validation, refund calculation, and Stripe refund processing.

**Fix**: Extract a shared `_process_refund_internal()` function that both routes call.

### 2. Use translations for refund error messages

**Problem**: Error messages are hardcoded in German with unicode escape sequences like `\u201e`/`\u201c`.

**Fix**: Add translation keys in `translations.py` and use them, OR at minimum use readable string literals.

### 3. Fix `stripe_refund_payment()` import

**Problem**: `import stripe` inside the function body at line ~300 of db.py shadows the module-level import.

**Fix**: Remove the local import — the module-level `stripe` SDK is already imported conditionally at the top of app.py and db.py.

### 4. Improve `sales-refund.js` — use `fetch()` API

**Problem**: Uses `XMLHttpRequest` for AJAX calls (verbose, callback-heavy).

**Fix**: Replace with `fetch()` for cleaner async/await code.

### 5. Improve `sales-refund.js` — card refund flow

**Problem**: The card refund path shows an `alert()` and doesn't actually integrate with Stripe Terminal.

**Fix**: Add proper Stripe Terminal integration for card refunds, or at minimum process them server-side with clear messaging.

### 6. Improve `sales-refund.js` — modal DOM building

**Problem**: Modal HTML is built via string concatenation (`modal.innerHTML = '...'`), making it hard to read and maintain.

**Fix**: Use `document.createElement()` for a cleaner, more maintainable approach.

### 7. Add loading states and better error feedback

**Problem**: During refund processing, only the submit button text changes. No spinner/loading indicator.

**Fix**: Add a spinner/overlay during processing, show toast notifications instead of `alert()`.

---

## Files to Edit

1. **`app.py`** — Extract shared refund logic (Items 1, 2)
2. **`db.py`** — Fix stripe import (Item 3)
3. **`static/js/sales-refund.js`** — Modernize JS (Items 4, 5, 6, 7)
4. **`translations.py`** — Add error message keys (Item 2)

## Follow-up

- After changes, verify the Flask app starts without errors
- Test refund flow via web UI
- Test refund flow via assistant API endpoint

