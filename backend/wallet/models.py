from decimal import Decimal

from django.conf import settings
from django.db import models

from core.models import BaseModel
from payments.models import Order


class Wallet(BaseModel):
    """
    Every user gets exactly one wallet. A separate singleton PlatformWallet
    row (owner is null, is_platform=True) accumulates commission revenue.
    Balances are only ever changed by creating a WalletTransaction row and
    updating `balance` inside the same DB transaction -- never edited
    directly -- so the ledger and the balance can be reconciled.
    """

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallet", null=True, blank=True
    )
    is_platform = models.BooleanField(default=False, db_index=True)
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    pending_balance = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"),
        help_text="Funds credited but held (e.g. pending withdrawal review).",
    )
    currency = models.CharField(max_length=3, default="TZS")
    is_frozen = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["is_platform"],
                condition=models.Q(is_platform=True),
                name="unique_platform_wallet",
            )
        ]

    def __str__(self):
        return "Platform Wallet" if self.is_platform else f"Wallet({self.owner_id})"


class WalletTransaction(BaseModel):
    """Immutable ledger entry. Sum of entries for a wallet must equal its
    current balance -- reconciled by a periodic integrity check job."""

    class EntryType(models.TextChoices):
        SALE_CREDIT = "sale_credit", "Sale credit (seller earning)"
        COMMISSION_CREDIT = "commission_credit", "Commission credit (platform)"
        WITHDRAWAL_DEBIT = "withdrawal_debit", "Withdrawal debit"
        REFUND_DEBIT = "refund_debit", "Refund debit"
        REFUND_REVERSAL_CREDIT = "refund_reversal_credit", "Refund reversal credit"
        ADJUSTMENT = "adjustment", "Manual adjustment"

    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="transactions")
    entry_type = models.CharField(max_length=30, choices=EntryType.choices, db_index=True)
    amount = models.DecimalField(
        max_digits=14, decimal_places=2, help_text="Positive = credit, negative = debit."
    )
    balance_after = models.DecimalField(max_digits=14, decimal_places=2)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="wallet_entries")
    reference = models.CharField(max_length=100, blank=True)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["wallet", "entry_type"])]


class PayoutAccount(BaseModel):
    """
    A structured, verifiable payout destination -- replaces the previous
    approach of accepting a raw {'msisdn': '...'} JSON blob directly on
    each WithdrawalRequest with no validation. A seller registers a
    payout account once, a moderator verifies it once (same
    approve/reject pattern as KYC), and every subsequent withdrawal just
    references it -- matching how Stripe Connect and similar platforms
    handle payout destinations.

    destination_details on WithdrawalRequest is kept (not removed) for
    backward compatibility with existing rows created before this model
    existed; new withdrawal requests should reference payout_account
    instead.
    """

    class AccountType(models.TextChoices):
        MOBILE_MONEY = "mobile_money", "Mobile Money"
        BANK_ACCOUNT = "bank_account", "Bank Account"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending verification"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payout_accounts"
    )
    account_type = models.CharField(max_length=20, choices=AccountType.choices)
    provider = models.CharField(
        max_length=100, blank=True,
        help_text="e.g. M-Pesa, Airtel Money, Tigo Pesa, CRDB Bank, NMB Bank.",
    )
    account_number = models.CharField(max_length=100, help_text="Phone number or bank account number.")
    account_name = models.CharField(max_length=255, help_text="Name the account is registered under.")
    is_default = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payout_account_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-is_default", "-created_at"]
        indexes = [models.Index(fields=["user", "status"])]

    def __str__(self):
        return f"{self.get_account_type_display()} ({self.account_number}) for {self.user_id}"


class WithdrawalRequest(BaseModel):
    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        APPROVED = "approved", "Approved"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        REJECTED = "rejected", "Rejected"
        FAILED = "failed", "Failed"

    class Destination(models.TextChoices):
        MOBILE_MONEY = "mobile_money", "Mobile Money"
        BANK_ACCOUNT = "bank_account", "Bank Account"

    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="withdrawal_requests")
    payout_account = models.ForeignKey(
        PayoutAccount, on_delete=models.PROTECT, null=True, blank=True, related_name="withdrawal_requests",
        help_text="Preferred over destination_details for new requests; kept nullable for backward "
                   "compatibility with withdrawal requests created before this model existed.",
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    destination_type = models.CharField(max_length=20, choices=Destination.choices)
    destination_details = models.JSONField(
        default=dict, blank=True,
        help_text="Legacy freeform destination info. New requests should use payout_account instead.",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REQUESTED, db_index=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="withdrawal_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True)
    payout_reference = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["wallet", "status"])]
