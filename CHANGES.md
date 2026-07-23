# What changed

## Latest: chat becomes the app's home page (AI-first)

The assistant is no longer a small floating helper — it's now the app's
landing page (`/`, `index()` → `home.html`). Every classic page (Dashboard,
Products, Sales, Debts, Sellers, Salary, Invoices, Kasse, Audit Log...) is
still there, one click away in the sidebar, but the app now opens straight
into a full-height chat panel instead of a dashboard.

- **New `templates/home.html`** — reuses the exact same `#assistantPanel`
  markup/IDs as the floating widget (so `assistant.js` drives it unmodified),
  laid out inline via a new `.home-chat-shell` instead of a fixed-position
  overlay. A tiny inline script force-opens it since there's no launcher to
  click.
- **`index()` in `app.py`** now renders `home.html` for logged-in users
  instead of redirecting straight to a dashboard.
- **Sidebar**: a new "Assistant (Home)" link sits above Dashboard on both the
  admin and seller nav (`templates/base.html`, `translations.py` — `de`/`en`/`ar`).
- **The floating chat widget is now available to sellers too**, not just
  admins (it was previously gated to `role == 'admin'` in `base.html`).
  Because the underlying tools weren't written with per-role scoping, a new
  `SELLER_ALLOWED_TOOLS` allowlist in `app.py` restricts non-admin chat
  sessions to read-only lookups + `quick_sell` + `print_barcode`; anything
  else (`pay_salary`, `delete_all_debts`, seller/facture management, ...)
  raises `PermissionError` and the chat reports it as a plain "admin-only"
  message instead of running. `/assistant/api/chat` and `/assistant/api/sell`
  are now `@login_required(['admin', 'seller'])` accordingly.

### Rich chat widgets (product cards, tables, dashboard stats, a chart)

The AI's replies used to be text-only. `WIDGETABLE_TOOLS` in `app.py` now
tracks the last read-only tool result worth rendering visually
(`get_summary`, `list_items`, `list_recent_sales`, `list_open_debts`,
`list_low_stock`, `list_recent_orders`) and attaches it as `widget` on the
`/assistant/api/chat` response. `assistant.js` (`renderWidget()` and friends)
turns that into an actual card grid, data table, or a small dashboard
stat-strip + bar chart, styled with the same design tokens as the rest of
the app (new CSS block at the end of `static/css/styles.css`) — not another
wall of bullet points.

### Conversational checkout ("sell an iPhone" → follow-ups → done)

`quick_sell` now also accepts `customer_name` and `payment_method`
(`cash`/`card`), both optional. The `sales` table gained matching nullable
columns (`seed_db.py` for new installs; `db.py`'s new
`ensure_sales_extra_columns()` ALTERs existing databases at startup, safe to
run every boot). The assistant's system prompt now explicitly walks a sale
one question at a time — which exact product/variant (searching via
`list_items` if the name is ambiguous), quantity, who it's for, cash or
card — restating the full line for confirmation before actually calling
`quick_sell`, instead of guessing at missing details.

## Previous: AI chat was actually broken, missing CRUD completed, security fix, and new ops tooling

### Critical fix: the AI chat assistant was completely non-functional

The migration from the Anthropic API to OpenAI (`/assistant/api/chat`) left
one line unconverted: the code called OpenAI's `chat.completions.create()`
with a `system=...` keyword argument, which is an Anthropic Messages API
concept that doesn't exist in the OpenAI SDK. Every single request to the
chat assistant — typed or spoken — was failing before it ever reached
OpenAI. The system prompt is now passed correctly as the first message in
the list (`{'role': 'system', ...}`), which is how OpenAI expects it. If you
had `OPENAI_API_KEY` configured and the chat still showed generic errors,
this was why.

Also cleaned up the leftover `.env.example` entry (`ANTHROPIC_API_KEY` ->
`OPENAI_API_KEY`) and removed the now-unused `anthropic` package from
`requirements.txt`, both stale artifacts of the same migration.

### Completed: missing CRUD for sales, salary payments, and cash register entries

Three entities had Create/Read but no Update, and two had no Delete either
— meaning a typo in a sale's quantity, a wrong salary amount, or a
mis-typed Kasse entry could only be fixed by deleting and re-entering data
(sales/Kasse) or not at all (salary payments):

- **Sales**: `templates/edit_sale.html` already existed but nothing linked
  to it — there was no backend route. Added `/admin/sales/edit/<id>`,
  wired an "Edit" link into the sales history page, and made the update
  correctly recompute the line's total/profit, keep the parent sale's
  total in sync, and adjust product stock by the quantity difference.
- **Salary payments**: the list page didn't even select the row `id` from
  the database, so individual payments couldn't be targeted at all. Added
  `id` to the query plus `/admin/salary/edit/<id>` and
  `/admin/salary/delete/<id>`, with Edit/Delete buttons on the payments
  list.
- **Cash register (Kasse)**: had Create and Delete but no Edit. Added
  `/admin/kasse/edit/<id>` with an Edit button next to the existing delete
  button.
- Also deduplicated `/admin/sales/delete_sales_order/<id>`, which had its
  own copy of the delete SQL instead of calling the (identical) function
  already in `db.py` — the two could have silently drifted apart.

### Security fix: `/kasse` had no login check and a hardcoded role

The cash register page's route was missing `@login_required` entirely (it
only relied on links being hidden in the nav, not on actually checking the
session) and had `role = 'admin'  # Replace with session logic` — meaning
any signed-in seller viewing it, or anyone hitting the URL directly, saw
themselves treated as admin for that page. Now requires a logged-in
session and reads the real role from it.

### New: the chat assistant can fix its own mistakes (and yours)

Following on from the CRUD fixes above, the AI assistant can now also
correct a sale line item, delete a wrong salary payment, and edit a Kasse
entry through natural conversation — with the same "restate what I'm about
to do and wait for a yes" confirmation as every other data-changing action.
`list_recent_sales` now includes each line item's id and
`list_kasse_transactions` is a new read tool, so the assistant has a way to
find the right row before editing it instead of guessing.

### New: audit log

Every admin-panel action and every assistant tool call that changes data
now writes a row to a new `audit_log` table (who, what, when, from web UI
or chat) — visible at **Audit Log** in the admin sidebar, with per-area
filtering. This needs `python seed_db.py` to be re-run once after
upgrading (it only adds the new table; existing data is untouched).

### New: OpenAPI spec for the external API

Added `openapi.yaml` documenting `/api/v1/health`, `/api/v1/items`, and
`/api/v1/sell` — import it into Postman, Swagger UI, or any OpenAPI-aware
client/codegen tool if you want another system to integrate against this
API properly instead of reverse-engineering it from the source.

### New: `/api/v1/health` and basic rate limiting

- `GET /api/v1/health` — unauthenticated status endpoint (DB connectivity,
  whether the AI assistant / external API / OCR are configured) for
  uptime monitors or a load balancer.
- The external API (`/api/v1/*`) and the AI chat endpoint now have a
  lightweight in-memory rate limit (60 req/min and 30 req/min per
  key/user respectively) so a bug or runaway script can't run up your
  OpenAI bill or hammer the sell API. This is per-process — if you ever
  run more than one gunicorn worker/dyno, swap it for Flask-Limiter with a
  shared Redis backend instead.

## Previous: profit tracking fix, smarter Sell page, salary page fix, external sell API

### Fixed: "Gewinn" (profit) always showing €0

Every place that recorded a sale (`/sell`, the chat's "⚡ Quick sale", and the
AI assistant's `quick_sell` tool) inserted a row into `sale_items` without
ever setting the `profit` column — so it silently stayed at the database's
default of `0.00` forever, even though `sale_price`/`purchase_price`/
`quantity` were all recorded correctly. All three insert paths now compute
and store `profit = total_price - purchase_price × quantity`. A one-time
repair also runs automatically the next time the app starts (safe to run
more than once) to backfill profit on every sale recorded before this fix,
so historical dashboards/reports correct themselves too — no manual
database work needed.

### `/sell` page: smarter and easier

- A plain search box to add a product (in addition to the barcode scanner) —
  type a name, click a result, it's added instantly.
- Each product row now shows a live stock badge (green/yellow/red), a
  quantity +/− stepper, and its own running subtotal.
- Quantities over the available stock are flagged immediately, before you
  even try to submit.
- The old browser `confirm()`/`alert()` popups are replaced with a proper
  summary dialog listing every line item and the total, so you can double
  check (or go back and edit) before finalizing.
- "Clear all" button and an item counter, plus an empty-state hint when
  nothing has been added yet.

### Chat "⚡ Quick sale": you can change your mind

The quick-sale form in the chat assistant was the only inline form in the
whole assistant that didn't have a Back button, and cancelling the
confirmation used to wipe out what you'd typed and dump you back at the Sell
menu. It now matches every other form in the assistant: there's a Back
button on the form itself, and cancelling the confirmation just leaves the
form on screen with your values still there so you can adjust the quantity
or product and try again. It also now shows a live price preview and warns
about insufficient stock before you confirm.

### `/admin/pay-salary`: fixed employee selection

The dropdown used to list every user in the system — including the admin
account itself, plus deactivated sellers you can't actually pay. It now only
lists active seller accounts, shows each person's configured salary right in
the dropdown, and auto-fills the amount field with that salary the moment
you pick them (still fully editable for partial payments or bonuses). The
server also now rejects any employee/amount that wasn't a valid choice,
instead of silently accepting whatever was submitted.

### New: external sell/lookup API

Two new endpoints let another system (a POS, a website, a script) sell items
and check stock the same way the chatbot does, without a logged-in browser
session:

- `GET /api/v1/items?q=<name or barcode>` — look up items
- `POST /api/v1/sell` with JSON body `{"identifier": "<name or barcode>", "quantity": 1}` — record a sale

Both require an `X-API-Key: <your key>` header. Set `API_KEY=...` in `.env`
to enable them (see `.env.example`) — they return `503` until a key is
configured, and `401` for a missing/wrong key.

## 0. The chat is now powered by a real AI (Claude), not keyword-matching

Free-text/voice messages used to go through a small in-browser keyword
parser (see section 2 below) — it only recognised a fixed list of phrases
per section and fell back to "I didn't understand" for anything else,
including plain small talk or unexpected phrasing.

That parser has been replaced with a new backend endpoint,
`POST /assistant/api/chat`, which sends the conversation to the Anthropic
API (Claude) with tool access to every action the buttons can already do
(add/pay a debt, add/pay an invoice, book a cash transaction, add an item,
add a seller, pay salary, add a purchase order, sell an item, and all the
list/lookup actions). Claude decides which tool(s) to call, asks for
confirmation before anything that writes data or moves money, and replies
conversationally — in German, English, or Arabic, matching whatever
language you just typed or said — with some actual personality instead of
a flat error message.

Buttons and inline forms are completely unchanged and still work exactly as
before; this only changes what happens when you type or speak free text.

**Setup:** add `ANTHROPIC_API_KEY=...` to your `.env` (see `.env.example`)
and `pip install -r requirements.txt` to pick up the new `anthropic`
dependency. Without a key configured, free-text chat shows a clear "not
configured" message and the buttons/menus keep working as normal.

## 1. The chat assistant can now do (almost) everything an admin can do

Before: the chat could only show a summary, manage debts, manage invoices,
show low stock, and show today's cash balance.

Now it also has full sections for:
- **Items** — list items, show low stock, add a new item (auto-generates a
  barcode if you don't type one)
- **Sellers** — list sellers, add a new seller
- **Salary** — list recent payments, pay an employee
- **Cash register (Kasse)** — now also lets you book a deposit/withdrawal,
  not just view the balance

New backend endpoints (all admin-only, same permission model as the rest of
the app): `/assistant/api/items`, `/assistant/api/sellers`,
`/assistant/api/salary`, `/assistant/api/kasse` (POST).

## 2. The chat understands you better (free text + typos)

The old parser only matched exact keywords. It now:
- Tolerates typos (e.g. "rechnnung", "invoic") using edit-distance matching
- Understands more phrasings in German/English/Arabic for every section
- Can go straight to an action from a single sentence, e.g. "add item Cola 20 3 5"
  or "pay employee Ahmed 500"

This is all still a **rule-based/offline** system (no external AI API), as
requested — it is smarter than before but it does not "understand"
open-ended free-form questions the way a real language model would. If you
ever want that, the natural next step is wiring the assistant up to the
Claude API (or another LLM), which needs an API key and has a small
per-message cost.

## 3. Invoice photo scan → auto-fill the form

Added on **both** the chat ("📷 Take a photo of the invoice" inside the
Invoices menu) and the regular "Add invoice" page (a new button at the top).

How it works:
1. You take/upload a photo of the invoice.
2. The photo is sent to `/factures/ocr`, which runs it through **Tesseract**
   (offline OCR, no external API).
3. Simple pattern-matching guesses the issuer, total amount, invoice type,
   issue date and due date.
4. The invoice form is pre-filled with these guesses — **you always confirm
   or correct them before saving**, since OCR on phone photos is never 100%
   reliable, especially for messy/handwritten invoices.

### Important: this needs Tesseract installed on the server

`pytesseract` (added to `requirements.txt`) is only a thin Python wrapper —
it does **not** install the OCR engine itself. You must install Tesseract
separately:

- **Ubuntu/Debian / most VPS providers:**
  `sudo apt-get install tesseract-ocr tesseract-ocr-deu tesseract-ocr-fra`
- **macOS (local dev):** `brew install tesseract tesseract-lang`
- **Heroku:** add the `heroku-community/apt` buildpack (alongside your
  existing Python buildpack) — it will read the included `Aptfile` and
  install Tesseract automatically. Run:
  ```
  heroku buildpacks:add --index 1 heroku-community/apt
  ```
  then redeploy.

If Tesseract isn't installed, the photo-scan feature will show a clear error
message instead of crashing the app — everything else keeps working
normally.

## Accuracy expectations for OCR

Offline OCR without an AI model is noticeably less accurate than an
AI-vision service, especially on:
- Blurry or low-light phone photos
- Handwritten invoices
- Unusual invoice layouts

For clean, printed invoices (eBay PDFs, utility bills, etc.) it should work
reasonably well. This is the tradeoff of avoiding an API key/cost — accuracy
can be improved later by switching the same `/factures/ocr` route to call an
AI vision API instead of Tesseract, without changing anything on the
frontend.
