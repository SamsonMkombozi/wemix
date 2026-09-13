# Habari Platform — Backend Foundation

Django 5.2 backend for the Habari Platform: a secure, AI-verified news
marketplace for Tanzania. This is the **models/backend foundation** —
custom user + KYC, news marketplace, AI verification, Selcom payments,
wallets/commission, and moderation/anti-circumvention — wired up, migrated,
and passing `manage.py check` cleanly. Views/serializers/API endpoints and
the actual AI/Selcom integration code are the next layer to build on top.

## App layout

| App          | Responsibility |
|--------------|----------------|
| `core`       | Shared abstract base models (UUID PK + timestamps), `AuditLog`, `PlatformSetting` |
| `accounts`   | Custom `User` (roles: buyer/seller/journalist/media_house/moderator/admin/super_admin), `IdentityVerification` (National ID + selfie + OCR + face match), 2FA recovery codes, login sessions |
| `news`       | `Category`, `Tag`, `NewsListing`, `NewsMedia`, `AIVerificationResult` (fake news/misinformation/clickbait/similarity/risk scoring), `ImageAnalysisResult` (manipulation/deepfake/EXIF/reverse-image) |
| `payments`   | `Order`, `SelcomTransaction`, `PaymentWebhookLog` (immutable, for reconciliation + signature auditing), `RefundRequest` |
| `wallet`     | `Wallet` (per-user + one platform singleton), `WalletTransaction` (append-only ledger — balances are never edited directly), `WithdrawalRequest` |
| `moderation` | `AntiCircumventionFlag` (phone/email/WhatsApp/Telegram/off-platform-language detection with graduated action), `UserViolationHistory`, `ModerationQueueItem` (unified review queue), `UserReport` |

## Design decisions worth knowing about

- **UUID primary keys everywhere** (via `core.models.BaseModel`) instead of
  auto-increment ints — avoids leaking sequential IDs (listing counts,
  transaction volume) to the public API.
- **Wallets are ledger-backed.** `Wallet.balance` is a cached total; the
  source of truth is the sum of `WalletTransaction` rows. Never write to
  `balance` directly outside the service-layer function that also inserts
  the matching ledger row in the same DB transaction.
- **`PaymentWebhookLog` logs every inbound Selcom call**, accepted or not —
  needed to prove/debug signature verification and to reconcile against
  Selcom's own records later.
- **Anti-circumvention has both a per-event log (`AntiCircumventionFlag`)
  and a per-user rolling tally (`UserViolationHistory`)** so enforcement
  can be graduated (warning → suspension → ban) instead of reacting to
  each flag in isolation.
- **`ModerationQueueItem` is a unified queue** across AI-verification
  escalations, KYC review, circumvention flags, and user reports, using a
  `related_object_id` UUID pointer rather than a `GenericForeignKey` — kept
  deliberately simple/decoupled rather than adding contenttypes coupling
  between apps.
- Money fields are `DecimalField`, never float. Percentage-derived amounts
  (`NewsListing.seller_earning` / `.platform_commission`) use `Decimal`
  arithmetic with explicit quantization.

## Getting started

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # then fill in DATABASE_URL, SECRET_KEY, Selcom + AI keys

python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Requires PostgreSQL — `DATABASE_URL` in `.env` points at it
(`postgres://user:pass@host:5432/dbname`). SQLite was used only for local
migration validation while building this out; it is not supported in
`settings.py` on purpose (the JSON fields and Decimal-precision wallet
math are validated against Postgres).

## What's not built yet (next steps)

1. DRF serializers + viewsets/URLs for each app (auth/register/login/2FA,
   listing CRUD, order/checkout, wallet/withdrawals, moderation queue).
2. Service-layer functions: `create_order`, `credit_wallet_on_sale`,
   `process_withdrawal`, `run_ai_verification`, `run_image_analysis`,
   `scan_for_circumvention` — these should be the *only* code paths that
   write to `Wallet`/`WalletTransaction` and `AIVerificationResult`/
   `ImageAnalysisResult`, so business rules stay centralized.
2. Selcom API client (OAuth/HMAC signing per their spec) + webhook view
   with signature verification and IP allowlist check.
3. AI verification engine integration (`AI_VERIFICATION_API_URL`) — the
   score fields on `AIVerificationResult`/`ImageAnalysisResult` are already
   shaped to receive whatever pipeline (in-house or third-party) you plug in.
4. Celery + Redis for async AI scoring, webhook retries, and withdrawal
   processing, so none of that blocks the request/response cycle.
5. DRF permission classes enforcing the role matrix (who can sell, who can
   moderate, who can access the admin dashboard sections).
