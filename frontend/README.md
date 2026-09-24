# WEMIX — Frontend

Plain HTML5 + CSS3 + JavaScript frontend (no build step, no framework —
matches the original quotation's stated tech stack). Talks to the Django
backend entirely over its REST API.

## Pages

| File | What it does |
|---|---|
| `index.html` | Marketplace browse: search, filter by category/type/price, headline ticker |
| `register.html` | Account creation (buyer / journalist / seller / media house) |
| `login.html` | Login, including 2FA code prompt when enabled |
| `listing.html?slug=...` | Listing detail; paywalled body, checkout initiation |
| `checkout.html?order=...` | Polls order status after checkout until paid/failed |
| `dashboard.html` | My Listings, Sell News (create/submit), My Purchases, Wallet + Withdrawals |

## Running it locally

This is static — no build step. Serve it with any static file server:

```bash
cd habari-frontend
python3 -m http.server 8080
```

Then open `http://127.0.0.1:8080/index.html`.

**The Django backend must also be running** (see the backend's
`STARTUP.md`), and its `.env` needs your frontend's origin in
`CORS_ALLOWED_ORIGINS`:

```
CORS_ALLOWED_ORIGINS=http://localhost:8080,http://127.0.0.1:8080
```

By default the frontend expects the API at `http://127.0.0.1:8000`. To
point it elsewhere (e.g. a deployed backend), set this before the other
scripts load, in each HTML file's `<head>`, or centrally by editing
`js/api.js`:

```html
<script>window.HABARI_API_BASE_URL = 'https://api.habariplatform.co.tz';</script>
```

## What's implemented vs. stubbed

**Implemented and tested end-to-end** (via an automated browser test
against the live backend): registration, login (incl. 2FA), marketplace
browsing/search/filter, listing detail with paywall, checkout initiation,
seller draft creation, submit-for-review, wallet balance display,
withdrawal requests.

**Not yet built** (backend API exists, frontend doesn't call it yet):
- Media/image upload UI on the create-listing form (API endpoint
  `POST /api/news/listings/<slug>/media/` exists; form only sends
  text fields right now)
- KYC document upload UI (API endpoint `POST /api/accounts/kyc/` exists)
- 2FA enrollment UI (API endpoints exist under `/api/accounts/2fa/`)
- Moderator/admin views (verification queue, moderation queue,
  withdrawal approval) — these exist as API endpoints gated by role
  permissions, but have no dedicated frontend pages; use `/admin/` for
  now.
- Email verification landing page (the email links to
  `FRONTEND_BASE_URL/verify-email?token=...` — that route isn't built
  yet, so currently verification has to be done by calling
  `POST /api/accounts/verify-email/` directly, e.g. via curl, or through
  Django admin by toggling `is_email_verified` on the user).

## Design notes

Tokens live at the top of `css/style.css` as CSS custom properties. Theme
follows the original spec: white background, black text, gold accent
(`#D4AF37` / `#C9A227` / `#A67C00`), black buttons with gold text that
invert on hover, Poppins throughout, thin-gold-border cards. The
scrolling black/gold headline ticker on the homepage is the one
signature flourish, nodding to the "Bloomberg + Reuters + Forbes"
brief without overdoing the animation.
