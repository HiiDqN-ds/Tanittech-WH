# Going live: billing, signup, compliance

What was added on top of the existing app (`billing_db.py`, `billing.py`,
`gdpr.py` — nothing in the existing `app.py`/`db.py` route logic was
rewritten, only ~25 lines added to wire it in):

- **Self-serve signup** — `/signup`: a shop owner picks a name, creates
  their admin login, picks a plan, and gets a 14-day trial immediately.
- **Stripe subscription billing** — `/billing`: plan switching, Stripe
  Customer Portal for card updates/cancellation, full webhook handling for
  the subscription lifecycle (trial end, renewal, failed payment,
  cancellation).
- **Access gating** — the whole app now sits behind an
  `@app.before_request` check (`subscription_gate()` in `billing.py`). No
  active trial/subscription → redirected to `/locked`, a checkout page.
  Signup, login, and billing routes themselves stay open so a shop can
  always get back in to pay.
- **GDPR basics** — `/gdpr/export` (full JSON data export) and
  `/gdpr/delete` (typed-confirmation full account wipe), linked from the
  billing page.

## Deployment model (read this)

This ships as **one deployment (one database) per shop**, not one shared
multi-tenant app serving many shops. That's a deliberate, honest tradeoff:
retrofitting the existing ~100 data-access functions in `db.py` with
tenant scoping is the "Multi-tenancy — Foundational" item on your own
roadmap slide, realistically 4-8 weeks of careful, tested work. Doing that
blind, in one pass, without a database to test against, would just hand
you broken code with false confidence.

Single-tenant-per-deploy still gets you to "sellable" today:
1. A customer signs up → you (or a script) spin up a new instance +
   database for them (Render/Railway/Fly/Heroku "app per customer" — cheap
   and scriptable at this stage; a `render.yaml`/Terraform template that
   provisions app+DB from a signup webhook is a good next automation step).
2. Their `/signup` creates their shop + admin + trial in that instance.
3. Stripe handles billing per instance, same as any subscription SaaS.

When you outgrow this (dozens of shops, want one shared deployment): add
`tenant_id` to every table, thread it through `db.py`'s queries and
`login_required`, and swap `shop_settings`/`subscriptions` here from
"one row" to "one row per tenant" — the schema in `billing_db.py` already
keys off a `shop_id` UUID so that migration is additive, not a rewrite.

## Setup checklist

1. **Stripe Dashboard** → Products → create one product, add three
   recurring monthly prices: Starter €29, Pro €59, Business €99. Copy each
   `price_...` ID.
2. Copy `.env.example` → `.env`, fill in:
   - `STRIPE_SECRET_KEY` (you likely already have this for Terminal)
   - `STRIPE_PRICE_STARTER` / `STRIPE_PRICE_PRO` / `STRIPE_PRICE_BUSINESS`
   - `APP_BASE_URL` (your public domain)
3. **Webhook**: Stripe Dashboard → Developers → Webhooks → Add endpoint →
   `https://YOUR-DOMAIN/billing/webhook`, subscribe to:
   `checkout.session.completed`, `customer.subscription.created`,
   `customer.subscription.updated`, `customer.subscription.deleted`,
   `invoice.payment_failed`, `invoice.payment_succeeded`. Copy the signing
   secret into `STRIPE_WEBHOOK_SECRET`.
4. Deploy. On first boot the app auto-creates `shop_settings`,
   `subscriptions`, and `billing_events` tables (`ensure_billing_tables()`
   in `billing.py`, called at import time — same pattern the rest of the
   app already uses for schema migrations).
5. Visit `/signup` on the fresh instance → create the shop's admin account
   → you're selling.

## What's NOT done yet (be upfront about this with customers)

- **Business insights** (margin per item, reorder suggestions — slide 6,
  "High demo impact") — not built. Real leverage for a sales demo; the
  data (purchase price, selling price, quantities) already exists in
  `products`/`sales`, so it's a dashboard-query + chart job, not a
  schema change.
- **Multi-tenancy** — see above.
- **Seat limits per plan** aren't enforced yet (`seller_limit` in
  `billing_db.PLANS` is defined but not checked when adding sellers) —
  add a check in the seller-creation route if you want plans to actually
  gate headcount.
