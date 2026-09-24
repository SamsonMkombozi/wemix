from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.permissions import IsAdminOrAbove, IsModeratorOrAbove, IsSuperAdmin
from .models import (
    CompanyPayoutAccount,
    CompanyWithdrawalRequest,
    PayoutAccount,
    Wallet,
    WalletTransaction,
    WithdrawalRequest,
)
from .serializers import (
    CompanyPayoutAccountSerializer,
    CompanyWithdrawalRequestSerializer,
    CompanyWithdrawalReviewSerializer,
    PayoutAccountReviewSerializer,
    PayoutAccountSerializer,
    WalletSerializer,
    WalletTransactionSerializer,
    WithdrawalRequestSerializer,
    WithdrawalReviewSerializer,
)
from .services import get_or_create_user_wallet


class MyWalletView(APIView):
    """GET /api/wallet/me/ -- balance + recent ledger entries for the
    logged-in user. Creates the wallet on first access if it doesn't
    exist yet (e.g. a buyer who has never sold anything)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .services import release_matured_holds

        wallet = get_or_create_user_wallet(request.user)
        release_matured_holds(wallet)
        transactions = wallet.transactions.order_by("-created_at")[:50]
        return Response(
            {
                "wallet": WalletSerializer(wallet).data,
                "recent_transactions": WalletTransactionSerializer(transactions, many=True).data,
            }
        )


class WithdrawalRequestListCreateView(generics.ListCreateAPIView):
    """GET/POST /api/wallet/withdrawals/ -- the current user's own withdrawal requests."""

    serializer_class = WithdrawalRequestSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        wallet = get_or_create_user_wallet(self.request.user)
        return WithdrawalRequest.objects.filter(wallet=wallet).order_by("-created_at")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["wallet"] = get_or_create_user_wallet(self.request.user)
        return context

    def get_throttles(self):
        if self.request.method == "POST":
            self.throttle_scope = "withdrawal_submit"
            return [ScopedRateThrottle()]
        return super().get_throttles()


class WithdrawalRequestQueueView(generics.ListAPIView):
    """GET /api/wallet/withdrawals/queue/ -- moderator/admin view of pending payouts."""

    serializer_class = WithdrawalRequestSerializer
    permission_classes = [IsModeratorOrAbove]

    def get_queryset(self):
        return WithdrawalRequest.objects.filter(
            status=WithdrawalRequest.Status.REQUESTED
        ).select_related("wallet", "wallet__owner")


class WithdrawalRequestReviewView(APIView):
    """POST /api/wallet/withdrawals/<id>/review/ -- moderator approves/
    rejects/completes a withdrawal. Completing it actually debits the
    seller's wallet via the ledger."""

    permission_classes = [IsModeratorOrAbove]

    def post(self, request, pk=None):
        withdrawal = get_object_or_404(WithdrawalRequest, pk=pk)
        serializer = WithdrawalReviewSerializer(
            data=request.data, context={"withdrawal": withdrawal, "request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(WithdrawalRequestSerializer(withdrawal).data)


class PayoutAccountListCreateView(generics.ListCreateAPIView):
    """GET/POST /api/wallet/payout-accounts/ -- the current user's own payout accounts."""

    serializer_class = PayoutAccountSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return PayoutAccount.objects.filter(user=self.request.user)


class PayoutAccountDetailView(generics.RetrieveDestroyAPIView):
    """GET/DELETE /api/wallet/payout-accounts/<id>/ -- view or remove one
    of your own payout accounts. Deleting one that already has withdrawal
    requests referencing it is blocked (on_delete=PROTECT on that FK) --
    the history has to stay intact."""

    serializer_class = PayoutAccountSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return PayoutAccount.objects.filter(user=self.request.user)


class PayoutAccountSetDefaultView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk=None):
        account = get_object_or_404(PayoutAccount, pk=pk, user=request.user)
        PayoutAccount.objects.filter(user=request.user, is_default=True).update(is_default=False)
        account.is_default = True
        account.save(update_fields=["is_default", "updated_at"])
        return Response(PayoutAccountSerializer(account).data)


class PayoutAccountQueueView(generics.ListAPIView):
    """GET /api/wallet/payout-accounts/queue/ -- pending payout account
    verifications. Restricted to super_admin, same as reviewing one (see
    PayoutAccountReviewView) -- approving where a seller's money goes is a
    stricter gate than ordinary moderation actions."""

    serializer_class = PayoutAccountSerializer
    permission_classes = [IsSuperAdmin]

    def get_queryset(self):
        return PayoutAccount.objects.filter(status=PayoutAccount.Status.PENDING).select_related("user")


class PayoutAccountReviewView(APIView):
    """POST /api/wallet/payout-accounts/<id>/review/ -- verifies/rejects a
    seller's payout destination. Deliberately restricted to super_admin
    (not IsModeratorOrAbove, unlike most other review actions) -- this
    decides where real money is allowed to go, the same reasoning
    CompanyWithdrawalReviewView already uses for company treasury payouts."""

    permission_classes = [IsSuperAdmin]

    def post(self, request, pk=None):
        account = get_object_or_404(PayoutAccount, pk=pk)
        serializer = PayoutAccountReviewSerializer(data=request.data, context={"account": account, "request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(PayoutAccountSerializer(account).data)


class PlatformWalletView(APIView):
    """GET /api/wallet/platform/ -- moderator/admin only. The platform's
    own commission wallet: current balance (all-time accumulated
    commission, minus nothing -- commission never gets withdrawn out
    of this the way seller wallets do) plus recent transaction entries
    and a 30-day commission total, for the moderation dashboard's
    Transactions tab."""

    permission_classes = [IsModeratorOrAbove]

    def get(self, request):
        from datetime import timedelta

        from django.utils import timezone

        from .services import get_or_create_platform_wallet

        platform_wallet = get_or_create_platform_wallet()
        thirty_days_ago = timezone.now() - timedelta(days=30)

        recent_entries = WalletTransaction.objects.filter(wallet=platform_wallet).order_by("-created_at")[:50]
        commission_last_30_days = WalletTransaction.objects.filter(
            wallet=platform_wallet, entry_type=WalletTransaction.EntryType.COMMISSION_CREDIT, created_at__gte=thirty_days_ago,
        ).count()

        return Response({
            "wallet": WalletSerializer(platform_wallet).data,
            "commission_entries_last_30_days": commission_last_30_days,
            "recent_entries": WalletTransactionSerializer(recent_entries, many=True).data,
        })


class CompanyPayoutAccountListCreateView(generics.ListCreateAPIView):
    """GET/POST /api/wallet/company/payout-accounts/ -- the business's own
    bank/mobile-money accounts that commission revenue can be withdrawn
    to. Admin-managed (adding one doesn't need super_admin -- only
    approving a withdrawal against it does)."""

    serializer_class = CompanyPayoutAccountSerializer
    permission_classes = [IsAdminOrAbove]
    queryset = CompanyPayoutAccount.objects.all()


class CompanyWithdrawalRequestListCreateView(generics.ListCreateAPIView):
    """GET/POST /api/wallet/company/withdrawals/ -- any admin can request
    a treasury withdrawal; approval is gated separately (see the review
    view below)."""

    serializer_class = CompanyWithdrawalRequestSerializer
    permission_classes = [IsAdminOrAbove]
    queryset = CompanyWithdrawalRequest.objects.select_related("payout_account", "requested_by").all()


class CompanyWithdrawalReviewView(APIView):
    """POST /api/wallet/company/withdrawals/<id>/review/ -- super_admin
    only. 'Only company owners may approve' from the business
    requirement maps to the platform's super_admin role -- the tier
    above regular admin."""

    permission_classes = [IsSuperAdmin]

    def post(self, request, pk=None):
        withdrawal = get_object_or_404(CompanyWithdrawalRequest, pk=pk)
        serializer = CompanyWithdrawalReviewSerializer(
            data=request.data, context={"withdrawal": withdrawal, "request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(CompanyWithdrawalRequestSerializer(withdrawal).data)
