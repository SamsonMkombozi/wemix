"""
Django settings for the Habari Platform project.

Environment-driven configuration: every value that changes between
local/staging/production is read from the environment (via a .env file
in development). See .env.example in the project root for the full list.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)
# Reads a .env file if present; in production these come from real
# environment variables instead (systemd, Docker, etc.).
environ.Env.read_env(BASE_DIR / ".env")

# ------------------------------------------------------------------
# Core
# ------------------------------------------------------------------
SECRET_KEY = env("SECRET_KEY", default="django-insecure-CHANGE-ME-IN-PRODUCTION")

# Used to encrypt integration credentials (Selcom/Nala API keys, SMTP
# password, etc.) stored in the database via the Settings tab -- see
# core/credentials.py. SEPARATE from SECRET_KEY as a security best
# practice (rotating one shouldn't force rotating the other). If unset,
# falls back to a key deterministically derived from SECRET_KEY so the
# app still works out of the box -- but for production, set a real
# CREDENTIAL_ENCRYPTION_KEY (generate one with:
# `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
# so encrypted credentials survive a SECRET_KEY rotation.
CREDENTIAL_ENCRYPTION_KEY = env("CREDENTIAL_ENCRYPTION_KEY", default="")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

AUTH_USER_MODEL = "accounts.User"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third-party
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    # habari apps
    "core",
    "accounts",
    "news",
    "payments",
    "wallet",
    "moderation",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "habari_platform.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "habari_platform.wsgi.application"

# ------------------------------------------------------------------
# Database — PostgreSQL in every environment (sqlite is not supported;
# the platform relies on JSONField + transactional integrity for wallets).
# ------------------------------------------------------------------
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres://habari:habari@localhost:5432/habari_platform",
    )
}

# ------------------------------------------------------------------
# Password validation
# ------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# django.contrib.auth.tokens.PasswordResetTokenGenerator (used by
# accounts/views.py's password-reset endpoints) reads this directly --
# tighter than Django's own 3-day default given this is a live financial
# marketplace, not just a content site.
PASSWORD_RESET_TIMEOUT = 60 * 60 * 24  # 24 hours

# ------------------------------------------------------------------
# Internationalization
# ------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Dar_es_Salaam"
USE_I18N = True
USE_TZ = True

# ------------------------------------------------------------------
# Static / media
# ------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# gunicorn (unlike runserver) never serves STATIC_URL itself, and this
# deployment has no separate web server/CDN in front of it for static
# files -- WhiteNoise serves them straight from the WSGI app with
# compression and far-future cache headers.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ------------------------------------------------------------------
# REST framework / JWT
# ------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
        "core.authentication.APIKeyAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.AnonRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "user": "600/hour",
        "anon": "100/hour",
        "login": "10/minute",
        "otp": "5/minute",
        "payment_webhook": "120/minute",
        "kyc_submit": "5/hour",
        "withdrawal_submit": "10/hour",
        "password_reset": "5/hour",
    },
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
}

from datetime import timedelta  # noqa: E402

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}

# ------------------------------------------------------------------
# CORS
# ------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])

# ------------------------------------------------------------------
# Security hardening (relaxed automatically when DEBUG=True)
# ------------------------------------------------------------------
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True

if not DEBUG:
    # Behind a reverse proxy (Traefik) that terminates TLS and forwards plain
    # HTTP internally -- without this, request.is_secure() is always False,
    # and SECURE_SSL_REDIRECT below redirects every already-HTTPS request to
    # itself in an infinite loop.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# ------------------------------------------------------------------
# File upload limits (KYC documents / news media)
# ------------------------------------------------------------------
DATA_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024  # 20 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024
ALLOWED_UPLOAD_IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp"]
ALLOWED_UPLOAD_VIDEO_TYPES = ["video/mp4", "video/quicktime"]

# ------------------------------------------------------------------
# Platform business rules
# ------------------------------------------------------------------
PLATFORM_COMMISSION_RATE = env.float("PLATFORM_COMMISSION_RATE", default=0.10)  # 10%

# How long a seller's earning is held before it counts toward their
# withdrawable balance -- covers the window a buyer has to file a refund
# request, the same pattern Airbnb/Upwork/Etsy use to guard against
# pay-then-refund fraud. Not a confirmed business policy figure; tune via
# env once finance signs off on a real number.
WALLET_HOLD_PERIOD_HOURS = env.int("WALLET_HOLD_PERIOD_HOURS", default=72)

# Terms & Conditions: bump this whenever the terms text materially
# changes -- accounts/views.py re-prompts a user to re-accept whenever
# their latest TermsAcceptance.version doesn't match this.
TERMS_VERSION = env("TERMS_VERSION", default="2026-09-13")

# Withdrawal fraud/velocity limits (TZS). A moderator can still see and
# override an individual request; these just gate self-service creation.
# NOTE: these defaults are a reasonable starting point, not a confirmed
# business policy figure -- tune via env vars once finance signs off on
# real limits.
WITHDRAWAL_DAILY_LIMIT = env.float("WITHDRAWAL_DAILY_LIMIT", default=2_000_000)
WITHDRAWAL_WEEKLY_LIMIT = env.float("WITHDRAWAL_WEEKLY_LIMIT", default=8_000_000)
WITHDRAWAL_MONTHLY_LIMIT = env.float("WITHDRAWAL_MONTHLY_LIMIT", default=20_000_000)
# A single request at/above this is flagged for extra moderator scrutiny
# (not blocked) -- surfaced as a warning pill in the withdrawal queue.
WITHDRAWAL_RISK_FLAG_THRESHOLD = env.float("WITHDRAWAL_RISK_FLAG_THRESHOLD", default=1_500_000)

# Metered paywall: free full-story unlocks a buyer gets per calendar
# month, across any listings, before every story requires payment (or an
# active subscription) -- the standard "read 3 free articles" conversion
# pattern. Not a confirmed business figure; tune once there's real
# traffic to A/B against.
FREE_PREVIEW_QUOTA_PER_MONTH = env.int("FREE_PREVIEW_QUOTA_PER_MONTH", default=3)

# ------------------------------------------------------------------
# Selcom payment gateway
# ------------------------------------------------------------------
SELCOM_API_BASE_URL = env("SELCOM_API_BASE_URL", default="https://apigwtest.selcommobile.com")
SELCOM_API_KEY = env("SELCOM_API_KEY", default="")
SELCOM_API_SECRET = env("SELCOM_API_SECRET", default="")
SELCOM_VENDOR_ID = env("SELCOM_VENDOR_ID", default="")
SELCOM_WEBHOOK_IP_ALLOWLIST = env.list("SELCOM_WEBHOOK_IP_ALLOWLIST", default=[])
SELCOM_WEBHOOK_URL = env(
    "SELCOM_WEBHOOK_URL",
    default="http://localhost:8000/api/payments/webhooks/selcom/",
)
SELCOM_SUBSCRIPTION_WEBHOOK_URL = env(
    "SELCOM_SUBSCRIPTION_WEBHOOK_URL",
    default="http://localhost:8000/api/payments/webhooks/selcom-subscription/",
)

# Nala/Rafiki -- international payment collections. See
# payments/nala_client.py's module docstring: these are placeholders
# until a real NALA/Rafiki business account and API reference exist.
NALA_API_BASE_URL = env("NALA_API_BASE_URL", default="https://api.rafiki.com")  # PLACEHOLDER host
NALA_API_KEY = env("NALA_API_KEY", default="")
NALA_API_SECRET = env("NALA_API_SECRET", default="")
NALA_WEBHOOK_SECRET = env("NALA_WEBHOOK_SECRET", default="")
NALA_WEBHOOK_URL = env(
    "NALA_WEBHOOK_URL",
    default="http://localhost:8000/api/payments/webhooks/nala/",
)

# ------------------------------------------------------------------
# AI verification engine (external service or in-process pipeline)
# ------------------------------------------------------------------
AI_VERIFICATION_API_URL = env("AI_VERIFICATION_API_URL", default="")
AI_VERIFICATION_API_KEY = env("AI_VERIFICATION_API_KEY", default="")
AI_AUTO_REJECT_THRESHOLD = env.float("AI_AUTO_REJECT_THRESHOLD", default=80.0)

# ------------------------------------------------------------------
# KYC face-match / liveness (external vendor -- Jumio/Onfido/Veriff/etc).
# Same honest-placeholder pattern as AI_VERIFICATION_API_URL: a genuine
# no-op until a real vendor is configured. See accounts/face_match.py.
# ------------------------------------------------------------------
KYC_FACE_MATCH_API_URL = env("KYC_FACE_MATCH_API_URL", default="")
KYC_FACE_MATCH_API_KEY = env("KYC_FACE_MATCH_API_KEY", default="")
AI_AUTO_VERIFY_THRESHOLD = env.float("AI_AUTO_VERIFY_THRESHOLD", default=20.0)

# ------------------------------------------------------------------
# Email (used for verification links, password resets, notifications)
# ------------------------------------------------------------------
EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend" if DEBUG else "django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="Habari Platform <no-reply@habariplatform.co.tz>")
FRONTEND_BASE_URL = env("FRONTEND_BASE_URL", default="http://localhost:3000")

# ------------------------------------------------------------------
# SMS (Africa's Talking) -- for the small set of notifications time-
# sensitive enough that email alone isn't enough reach in a mobile-
# money-first market (sale confirmations, saved-search "breaking"
# alerts; see core/sms_client.py and core/notifications.py). Same
# honest-placeholder-until-confirmed tier as Selcom: built from Africa's
# Talking's public API docs, not verified against a live account from
# this sandbox. A genuine no-op with zero behavior change if
# AFRICASTALKING_API_KEY is unset, which it is by default everywhere
# until someone provisions a real account.
# ------------------------------------------------------------------
AFRICASTALKING_API_KEY = env("AFRICASTALKING_API_KEY", default="")
AFRICASTALKING_USERNAME = env("AFRICASTALKING_USERNAME", default="")
AFRICASTALKING_SENDER_ID = env("AFRICASTALKING_SENDER_ID", default="")
AFRICASTALKING_BASE_URL = env("AFRICASTALKING_BASE_URL", default="https://api.sandbox.africastalking.com/version1")

# ------------------------------------------------------------------
# Celery (async task queue) -- AI verification, OCR, and image analysis
# run as Celery tasks (see news/tasks.py, accounts/tasks.py) instead of
# inline inside the request that triggers them. CELERY_TASK_ALWAYS_EAGER
# defaults to True whenever CELERY_BROKER_URL is unset, which runs every
# task synchronously in-process -- identical to the old inline-call
# behavior, so nothing changes here until a real Redis broker is
# configured AND a worker process is started. Turning genuinely async:
# set CELERY_BROKER_URL, run `celery -A habari_platform worker`, and note
# that submit()/media() responses will then reflect "queued", not the
# final AI result -- a caller that wants the outcome needs to poll
# GET .../ai-results/ or listen for the existing Notification instead of
# reading it straight off the POST response.
# ------------------------------------------------------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="")
CELERY_RESULT_BACKEND = CELERY_BROKER_URL or None
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=not bool(CELERY_BROKER_URL))
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE

# ------------------------------------------------------------------
# Error tracking (Sentry) -- same honest-placeholder pattern as the
# Selcom/Nala credentials: reads from env, and is a genuine no-op with
# zero behavior change if SENTRY_DSN is unset (which it is by default in
# every environment until someone creates a real Sentry project and sets
# it -- that account creation is the user's to do, not something this
# codebase can provision on its own). Doesn't touch performance-tracing
# sample rates beyond a conservative default so it's cheap to turn on.
# ------------------------------------------------------------------
SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.1),
        environment=env("SENTRY_ENVIRONMENT", default="development" if DEBUG else "production"),
        send_default_pii=False,
    )

# ------------------------------------------------------------------
# Logging — everything to stdout/stderr in production (captured by
# systemd/journald or the hosting platform); rotate a local file in dev.
# ------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "habari": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
