# Habari Platform

Full project: Django REST API backend + static HTML/CSS/JS frontend.

```
habari_platform/
├── backend/     Django REST API (see backend/README.md and backend/STARTUP.md)
└── frontend/    Static HTML/CSS/JS site (see frontend/README.md)
```

## Quick start

**1. Backend** — follow `backend/STARTUP.md` in full the first time. Short version:

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: confirm DEBUG=True, ALLOWED_HOSTS includes 127.0.0.1,
# DATABASE_URL points at a real Postgres db, and add:
#   CORS_ALLOWED_ORIGINS=http://localhost:8080,http://127.0.0.1:8080
python3 manage.py migrate
python3 manage.py createsuperuser
python3 manage.py runserver
```

**2. Frontend** (in a second terminal):

```bash
cd frontend
python3 -m http.server 8080
```

**3. Open** `http://127.0.0.1:8080/index.html` in a browser.

Django admin is at `http://127.0.0.1:8000/admin/` — log in with the
superuser you created, and add at least one Category there before
creating listings (the create-listing form on the dashboard needs at
least one to exist).

## What's included

- **Backend**: accounts (auth, KYC, 2FA), news marketplace (listings, AI
  verification result storage, media), payments (Selcom checkout +
  webhook handling), wallet (ledger, withdrawals), moderation models
  (anti-circumvention, moderation queue) — see `backend/README.md` for
  the full breakdown of design decisions.
- **Frontend**: marketplace browse/search, registration/login (with
  2FA), listing detail with paywall + checkout, seller dashboard
  (create/submit listings, wallet, withdrawals, purchase history).

Both were tested together end-to-end with an automated browser driving
the actual UI against the actual API (register → login → browse →
create listing → submit → checkout) before this was packaged.

## Known gaps (see each README for detail)

- Frontend: no media/KYC upload UI yet, no moderator dashboard pages
  (use Django admin), no email-verification landing page yet.
- Backend: Selcom integration is unverified against their live API docs
  (this environment has no network access to confirm exact field names)
  — test in their sandbox before going live.


Ran clean — no errors, and the numbers make sense: Users: 18 and Categories: 8 are higher than my test run's 14/6 because your database already had a few accounts and categories from your earlier manual testing (your own mkombozi superuser, testuser, seller1, the Business/Sports categories you created, etc.) — the seed script correctly added its 14 demo users and 6 demo categories on top without touching or duplicating what was already there.

You're in good shape to explore now. A few ways to look around:

Django admin (http://127.0.0.1:8000/admin/, login as your mkombozi superuser or demo_superadmin/DemoPass!2026):

Accounts → Users — see the full spread of roles/KYC states/suspended account
News → News listings — filter by status to see draft/submitted/published/suspended side by side
Wallet → Wallets and Wallet transactions — see real ledger entries from actual sales
Moderation → Anti circumvention flags — see the enforcement actions applied
Payments → Orders — paid/failed orders with their linked Selcom transactions

Frontend (http://127.0.0.1:8080):

Log in as demo_amina / demo_amina@example.com	/ DemoPass!2026 — verified seller with real sales, check her dashboard wallet balance and listing stats
Log in as demo_john / demo_john@example.com	 / DemoPass!2026 — buyer with 2FA enabled and purchase history, check "My Purchases"
Browse the marketplace logged out — should show the published/verified listings with real prices from the seed data

Try logging in as a couple of these and let me know if anything looks off or behaves unexpectedly — that'd be the next thing worth chasing down.# wemix
