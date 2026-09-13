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

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ------------------------------------------------------------------
# REST framework / JWT
# ------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
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
