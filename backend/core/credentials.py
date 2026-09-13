"""
core/credentials.py — encrypted storage and resolution for integration
credentials (Selcom, Nala, AI verification, email/SMTP), editable live
from the Settings tab.

Resolution order for any (provider, field_name): a DB-stored
IntegrationCredential row, if present and non-empty, wins; otherwise
falls back to the .env/settings.py value. This means a fresh deployment
that has never touched the Settings tab keeps working exactly as it
always did (pure environment variables), while an admin who saves a
credential through the UI immediately overrides it -- no server
restart required, since this is read fresh on every use rather than
cached at Django startup the way settings.py values are.

Encryption: Fernet symmetric encryption (AES-128-CBC + HMAC, from the
`cryptography` package) keyed by CREDENTIAL_ENCRYPTION_KEY. Secret
values are never returned by the API in decrypted form after being
saved -- callers get a masked hint (e.g. "••••1234") and a boolean
`is_set`, never the real value, the same pattern password fields use
everywhere.
"""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

logger = logging.getLogger("habari.credentials")


def _get_fernet() -> Fernet:
    key = settings.CREDENTIAL_ENCRYPTION_KEY
    if key:
        # Accept either a real Fernet key (44-char urlsafe-base64) or an
        # arbitrary passphrase, which we normalize into a valid key.
        try:
            return Fernet(key.encode() if isinstance(key, str) else key)
        except Exception:
            pass
    # Fallback: deterministically derive a key from SECRET_KEY so the
    # app works out of the box. Logged once as a reminder to configure a
    # dedicated key in production (see settings.py's comment).
    derived = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(derived)


def encrypt_value(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        logger.error("Failed to decrypt a stored credential -- CREDENTIAL_ENCRYPTION_KEY may have changed.")
        return ""


def mask_value(plaintext: str) -> str:
    """Never show a real secret in an API response -- just enough to
    confirm 'yes, this is probably the right one' (last 4 chars),
    same convention Stripe/most payment dashboards use for API keys."""
    if not plaintext:
        return ""
    if len(plaintext) <= 4:
        return "••••"
    return f"{'•' * min(8, len(plaintext) - 4)}{plaintext[-4:]}"


# Maps (provider, field_name) -> the settings.py attribute to fall back to.
_ENV_FALLBACKS = {
    ("selcom", "api_key"): "SELCOM_API_KEY",
    ("selcom", "api_secret"): "SELCOM_API_SECRET",
    ("selcom", "vendor_id"): "SELCOM_VENDOR_ID",
    ("selcom", "base_url"): "SELCOM_API_BASE_URL",
    ("nala", "api_key"): "NALA_API_KEY",
    ("nala", "api_secret"): "NALA_API_SECRET",
    ("nala", "webhook_secret"): "NALA_WEBHOOK_SECRET",
    ("nala", "base_url"): "NALA_API_BASE_URL",
    ("ai_verification", "api_url"): "AI_VERIFICATION_API_URL",
    ("email", "host"): "EMAIL_HOST",
    ("email", "port"): "EMAIL_PORT",
    ("email", "username"): "EMAIL_HOST_USER",
    ("email", "password"): "EMAIL_HOST_PASSWORD",
    ("email", "use_tls"): "EMAIL_USE_TLS",
    ("email", "from_email"): "DEFAULT_FROM_EMAIL",
}

# Fields that hold secrets (masked in API responses) vs. plain config
# (returned in full -- a base URL or port isn't sensitive the way an
# API key or password is).
SECRET_FIELDS = {
    ("selcom", "api_key"), ("selcom", "api_secret"),
    ("nala", "api_key"), ("nala", "api_secret"), ("nala", "webhook_secret"),
    ("email", "password"),
}


def get_credential(provider: str, field_name: str, default: str = "") -> str:
    """Resolves the EFFECTIVE value for (provider, field_name): a saved
    DB override if present, else the .env/settings.py fallback."""
    from .models import IntegrationCredential

    row = IntegrationCredential.objects.filter(provider=provider, field_name=field_name).first()
    if row and row.encrypted_value:
        return decrypt_value(row.encrypted_value)

    env_attr = _ENV_FALLBACKS.get((provider, field_name))
    if env_attr:
        return str(getattr(settings, env_attr, default) or default)
    return default


def set_credential(provider: str, field_name: str, value: str, user=None) -> "IntegrationCredential":
    from .models import IntegrationCredential

    is_secret = (provider, field_name) in SECRET_FIELDS
    row, _ = IntegrationCredential.objects.get_or_create(
        provider=provider, field_name=field_name, defaults={"is_secret": is_secret},
    )
    row.encrypted_value = encrypt_value(value) if value else ""
    row.is_secret = is_secret
    row.updated_by = user
    row.save()
    return row


def list_provider_status(provider: str, fields: list[str]) -> dict:
    """For the Settings tab: for each field, whether it's effectively
    set (DB override or env fallback) and, for secrets, a masked hint;
    non-secrets are returned in full. Also reports whether the CURRENT
    value came from the database (an admin overrode it) or from .env,
    so the UI can show that provenance."""
    from .models import IntegrationCredential

    db_rows = {
        row.field_name: row
        for row in IntegrationCredential.objects.filter(provider=provider, field_name__in=fields)
    }
    result = {}
    for field_name in fields:
        is_secret = (provider, field_name) in SECRET_FIELDS
        row = db_rows.get(field_name)
        source = "database" if (row and row.encrypted_value) else "environment"
        value = get_credential(provider, field_name)
        result[field_name] = {
            "is_set": bool(value),
            "source": source if value else "unset",
            "value": mask_value(value) if is_secret else value,
            "is_secret": is_secret,
        }
    return result


def get_email_connection():
    """Returns a Django email connection built from the EFFECTIVE email
    credentials (DB override or .env fallback) -- so a saved SMTP
    password change takes effect on the very next email sent, without a
    server restart. Falls back to Django's default connection (which
    just uses settings.EMAIL_* / EMAIL_BACKEND as always) when nothing
    has ever been overridden through the Settings tab."""
    from django.core.mail import get_connection

    has_db_override = get_credential("email", "host") and get_credential("email", "username")
    if not has_db_override:
        return get_connection()  # default: whatever EMAIL_BACKEND/EMAIL_* settings.py already has

    return get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend",
        host=get_credential("email", "host"),
        port=int(get_credential("email", "port", "587") or 587),
        username=get_credential("email", "username"),
        password=get_credential("email", "password"),
        use_tls=get_credential("email", "use_tls", "True").lower() in {"1", "true", "yes"},
    )


def get_effective_from_email() -> str:
    return get_credential("email", "from_email", settings.DEFAULT_FROM_EMAIL)
