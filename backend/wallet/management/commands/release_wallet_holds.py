from django.core.management.base import BaseCommand
from django.utils import timezone

from wallet.models import Wallet, WalletHold
from wallet.services import release_matured_holds


class Command(BaseCommand):
    help = (
        "Releases every matured payout hold across all wallets, moving held "
        "seller earnings into the available-for-withdrawal balance. The "
        "same release also happens lazily whenever a wallet is viewed or a "
        "withdrawal is created/completed -- this command is for operators "
        "who'd rather run it on a schedule (e.g. hourly via cron) instead "
        "of relying on that lazy path alone."
    )

    def handle(self, *args, **options):
        matured_wallet_ids = (
            WalletHold.objects.filter(released=False, matures_at__lte=timezone.now())
            .values_list("wallet_id", flat=True)
            .distinct()
        )
        count = 0
        for wallet in Wallet.objects.filter(pk__in=list(matured_wallet_ids)):
            release_matured_holds(wallet)
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Released matured holds on {count} wallet(s)."))
