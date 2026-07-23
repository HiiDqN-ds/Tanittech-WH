# KassenPilot Platform — accounts, subscriptions & permissions

One app, three roles, one login form:

- **superadmin** — you. Username is always `superadmin`; password is
  whatever you set as `SUPERADMIN_PASSWORD`. Creates subscriptions
  (shop + admin account) instantly — the **Test** plan is €0 and activates
  immediately, no Stripe involved. Can grant/revoke *any* user anywhere,
  edit anyone's permissions, and suspend/reactivate a whole shop.
- **admin** — represents one subscribing store, created by you. Logs in
  with the username/password you gave them at creation. Creates seller/
  worker accounts for their own shop only, and grants/revokes exactly what
  each one can access (Kasse, inventory, debts, payroll, invoices, reports,
  clients, and the AI assistant).
- **seller** — a worker account. Sees only the modules their admin (or you,
  overriding) granted them.

## Run it
```bash
pip install -r requirements.txt
export SUPERADMIN_PASSWORD=choose-a-strong-password
export SECRET_KEY=any-random-string
python app.py
```
Open **http://localhost:5060**, log in as `superadmin` / your password.

## Try the full flow
1. Click **+ New subscription** → give it a store name, optionally upload a
   logo, pick the **Test** plan (€0, instant), set an admin username/password
   → **Create & activate**. It shows up immediately as a card on the
   dashboard (FIFA-card style: plan tier = card border color, green/red dot
   = active/suspended, logo in the middle).
2. Click the card to see that shop's users, suspend/reactivate the shop, or
   grant/revoke/edit permissions for its admin.
3. Log out, log back in as that shop's admin → **+ New seller** → create a
   worker account → click **Permissions** to check the boxes for what they
   can access (including the AI assistant).
4. Log in as that seller — they'll only see what was granted.
5. As superadmin, you can jump into any shop's detail page and override any
   of the above — grant/revoke a seller's access even though you didn't
   create them, or suspend the whole shop.

## Scope note
This is a self-contained accounts/permissions core with its own database —
it doesn't touch KassenPilot's existing POS app (`ookk_2/`, the one with
inventory/kasse/payroll/etc). That's intentional: retrofitting true
multi-tenant data isolation into that ~6,500-line app in one pass is how you
risk one store seeing another's sales data. This app is the safe place to
get subscriptions/permissions right first. When you're ready, the natural
next step is wiring KassenPilot's login to check against this same `users`
table (or having this app call KassenPilot's API with the permissions it
computes) — ask when you want that built.

## Deploy
Same pattern as your other apps — `Procfile` included, push this folder as
its own small deployment, set `SUPERADMIN_PASSWORD` and `SECRET_KEY` as env
vars. Uses SQLite, so no extra database service is required. Uploaded logos
live in `uploads/logos/` — make sure that directory persists across deploys
(e.g. mount a persistent volume) or logos will disappear on redeploy.
