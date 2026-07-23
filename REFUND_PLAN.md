# Refund Feature Implementation Plan

## Information Gathered

After thoroughly reading the codebase:

1. **`db.py`** — Contains all database helpers (`fetch_all`, `fetch_one`, `execute_query`, `get_connection`), sales CRUD (`load_sales`, `get_sale_item`, `update_sale_item`, `delete_sales_order`), product stock management, Stripe payment intent column ensures, and audit logging (`log_audit` with `format_*_details` helpers). The `PAYMENT_METHODS = ('cash', 'card')` tuple exists.

2. **`app.py`** — Contains Stripe Terminal handling (`create_stripe_terminal_payment_intent`, `verify_stripe_terminal_payment`), sales routes (`admin_sales`, `delete_sales_order_route`), Kasse withdrawal logic, and the full assistant chat backend.

3. **`translations.py`** — Contains DE/EN/AR translation dictionaries with existing sales-related keys under `'sales_*'`.

4. **`admin_sales.html`** — Renders each sale as a card with line-item table, receipt button, contract button, and delete button. Has inline JavaScript for receipt popup printing.

5. **`list_orders.html`** — Purchase orders list (Einkauf), also referenced.

6. **Stripe Terminal** — Already integrated for card payments on sales/debt payments/purchase orders. `verify_stripe_terminal_payment()` and `create_stripe_terminal_payment_intent()` exist.

## Plan

### Step 1: Database Layer (`db.py`)
- Add `ensure_refunds_table()` — Create `refunds` table
- Add `record_refund()` — Record refund + restore stock + log Kasse withdrawal if cash + create audit log entry
- Add `get_sale_refunds()` — Get all refunds for a sale
- Add `load_sales_with_refunds()` — Enhanced `load_sales` that includes refund data per sale
- Add `refund_format_sale_details()` helper for audit log formatting
- Add `stripe_refund_payment()` — Process Stripe refund for card payments

### Step 2: Translation Keys (`translations.py`)
- Add refund-related keys for DE/EN/AR:
  - `sales_refund_btn` — Refund button text
  - `sales_refund_title` — Refund modal title
  - `sales_refund_confirm` — Confirmation text
  - `sales_refund_cash` / `sales_refund_card` — Method labels
  - `sales_refund_success` — Success message
  - `sales_refund_partial` — Partial refund label
  - `sales_refund_stock_restored` — Stock restored notice
  - `sales_refund_reason` / `sales_refund_reason_ph` — Reason field
  - `sales_refund_history` — History section title
  - `sales_refund_total_refunded` — Already refunded label
  - `sales_refund_submit` — Submit button text
  - `sales_refund_processing` — Processing state
  - `sales_refund_no_refunds` — No refunds yet
  - `sales_refund_amount_refunded` — Amount refunded so far

### Step 3: Routes (`app.py`)
- Add route: `POST /admin/sales/refund/<sale_id>` — Process a refund for specific items
- Add route: `GET /admin/sales/<sale_id>/refunds` — Get refund history for a sale
- Add route: `POST /assistant/api/sales/refund` — Assistant tool endpoint for refunds

### Step 4: Refund UI JavaScript (`static/js/sales-refund.js`)
- Create a new JS file with:
  - `openRefundModal(sale)` — Opens modal with line-item selection
  - Line-item checkboxes + quantity toggles for partial refunds
  - Refund method selector (Cash / Card)
  - Reason text field
  - Live preview of refund total
  - Stripe Terminal integration for card refunds
  - Submit + show refund history

### Step 5: Template Updates (`admin_sales.html`)
- Add "Refund" button next to each sale card
- Add refund modal HTML (hidden, toggled by JS)
- Add refund history display section on each sale card

### Step 6: Test
- Launch the app and verify the refund flow works end-to-end

## Dependent Files to be edited
1. `db.py` — Database functions
2. `translations.py` — Translation keys
3. `app.py` — Flask routes
4. `templates/admin_sales.html` — Sales page UI
5. `static/js/sales-refund.js` — New file, refund client-side logic

## Database Schema (New `refunds` table)
```sql
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
```

## Followup Steps
- After implementation, restart the Flask app
- The `ensure_refunds_table()` runs at startup so no manual DB migration needed
- Test by visiting admin sales page, clicking refund on a sale, selecting items, choosing cash/card, entering reason, confirming

