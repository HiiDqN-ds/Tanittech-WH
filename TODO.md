# Refund Feature Implementation — TODO

## Status: ✅ COMPLETE

### Step 1: `app.py` — Add refund routes ✅ DONE
- [x] Updated `admin_sales` route to use `load_sales_with_refunds()` 
- [x] Added route: `POST /admin/sales/refund/<sale_id>` — Process a refund
- [x] Added route: `GET /admin/sales/<sale_id>/refunds` — Get refund history
- [x] Added route: `POST /assistant/api/sales/refund` — Assistant tool endpoint

### Step 2: `static/js/sales-refund.js` — Create new JS file ✅ DONE
- [x] Created refund modal logic with item selection, quantity toggles, method selector
- [x] Added Stripe Terminal integration for card refunds
- [x] Added refund history panel display

### Step 3: `templates/admin_sales.html` — Update template ✅ DONE
- [x] Added "Refund" button on each sale card
- [x] Added i18n injection for refund translations
- [x] Added sales-refund.js script include
- [x] Added refund summary display (total refunded so far)

## How to Test the Refund Feature

### Prerequisites
1. The app must be running (Flask server on port 5001 by default)
2. You must be logged in as an admin
3. There must be at least one sale in the system with items that have not been fully refunded

### Testing Steps

#### 1. Via Web UI (admin_sales.html)
- Navigate to `/admin/sales` (Sales History page)
- Each sale card now has a **"Rückerstatten" (Refund)** button
- Click the Refund button to open the refund modal
- The modal shows all line items with:
  - Checkboxes to select which items to refund
  - Quantity inputs (auto-filled with max refundable qty)
  - Unit price and calculated line total
  - Live refund total at the bottom
- Choose refund method:
  - **Cash**: Records a Kasse withdrawal automatically
  - **Card**: Processes refund via Stripe (only if Stripe is configured and original sale was card)
- Enter a reason for the refund (optional)
- Click **"Rückerstattung ausführen"** to process
- On success, the page reloads and refund amounts are shown

#### 2. Refund History
- In the refund modal, click **"Rückerstattungsverlauf"** button
- Shows all previous refunds for this sale with:
  - Product name, quantity, amount
  - Refund method (cash/card)
  - Reason
  - Date and who processed it
  - Total refunded so far

#### 3. Via Assistant API
- `POST /assistant/api/sales/refund`
- JSON body: `{ "sale_id": "...", "items": [{"sale_item_id": 123, "qty": 1}], "refund_method": "cash", "reason": "..." }`

#### 4. What to verify
- ✅ Refund amount is calculated correctly (unit_price × qty)
- ✅ Stock is restored for refunded items
- ✅ Cash refunds create a Kasse withdrawal entry
- ✅ Card refunds process via Stripe (if configured)
- ✅ Audit log entry is created
- ✅ Already-refunded quantities are respected
- ✅ Cannot refund more than was bought
- ✅ Refund history is accurate

