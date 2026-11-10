"""Recovery is an explicit server operation, never a password-only web bypass."""
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice


class Command(BaseCommand):
    help = "Сбросить 2FA после проверки личности сотрудника и завершить его сеансы."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **options):
        reason = options["reason"].strip()
        if len(reason) < 10 or len(reason) > 500:
            raise CommandError("Укажите обоснование длиной от 10 до 500 символов.")
        try:
            user = get_user_model().objects.get(username=options["username"])
        except get_user_model().DoesNotExist as exc:
            raise CommandError("Учётная запись не найдена.") from exc
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=user.pk)
            TOTPDevice.objects.filter(user=user).delete()
            StaticDevice.objects.filter(user=user).delete()
            for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator():
                if session.get_decoded().get("_auth_user_id") == str(user.pk):
                    session.delete()
        # Admin must retain this operation in the organization's access/change log.
        self.stdout.write(
            f"{timezone.now().isoformat()} Сброс 2FA для {user.get_username()}: {reason}. "
            "Активные сеансы завершены. При обязательной 2FA потребуется новое подключение."
        )
