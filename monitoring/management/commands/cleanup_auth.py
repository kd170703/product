from datetime import timedelta
from django.core.management.base import BaseCommand
from django.contrib.sessions.models import Session
from django.utils import timezone
from axes.models import AccessAttempt, AccessFailureLog, AccessLog
from django_otp.plugins.otp_totp.models import TOTPDevice


def cleanup_auth_records():
    """Keep only the short-lived data needed for throttling, not a permanent log."""
    cutoff = timezone.now() - timedelta(days=1)
    counts = []
    for model in (AccessAttempt, AccessFailureLog, AccessLog):
        count, _ = model.objects.filter(attempt_time__lt=cutoff).delete()
        counts.append(count)
    pending, _ = TOTPDevice.objects.filter(confirmed=False, created_at__lt=cutoff).delete()
    sessions, _ = Session.objects.filter(expire_date__lt=timezone.now()).delete()
    return sum(counts) + pending + sessions


class Command(BaseCommand):
    help = "Удалить записи защиты входа и незавершённую настройку 2FA старше суток."

    def handle(self, *args, **options):
        self.stdout.write(f"Удалено записей защиты входа: {cleanup_auth_records()}.")
