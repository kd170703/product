"""Local TOTP enrollment. QR codes and recovery codes never leave this server."""
import io
import secrets
import time
from base64 import b32encode

import qrcode
import qrcode.image.svg
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.signals import user_login_failed
from django.contrib.auth.views import LoginView
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET
from django_otp import login as otp_login, user_has_device, verify_token
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from .auth_forms import (
    PasswordConfirmationForm,
    RecoveryRegenerationForm,
    TokenConfirmationForm,
    TwoFactorAuthenticationForm,
)

SETUP_TTL_SECONDS = 10 * 60


class SecureLoginView(LoginView):
    template_name = "registration/login.html"
    authentication_form = TwoFactorAuthenticationForm

    def get_success_url(self):
        if settings.REQUIRE_TWO_FACTOR and not user_has_device(self.request.user):
            return reverse("account_security")
        return super().get_success_url()


def admin_login(request):
    # A single authentication endpoint means the built-in admin password form
    # can never create a session that bypasses second-factor verification.
    from django.contrib.auth.views import redirect_to_login
    return redirect_to_login(reverse("admin:index"), reverse("login"))


def _pending_device(request):
    started = request.session.get("otp_setup_started", 0)
    if not isinstance(started, (int, float)) or time.time() - started > SETUP_TTL_SECONDS:
        return None
    return TOTPDevice.objects.filter(
        pk=request.session.get("otp_setup_device"),
        user=request.user,
        confirmed=False,
    ).first()


def _password_valid(request, form):
    return authenticate(
        request=request,
        username=request.user.get_username(),
        password=form.cleaned_data["password"],
    ) is not None


@sensitive_variables("tokens")
def _replace_recovery_codes(user):
    device, _ = StaticDevice.objects.get_or_create(
        user=user, name="Резервные коды", defaults={"confirmed": True}
    )
    # Serialize concurrent regeneration and verification on the parent device.
    device = StaticDevice.objects.select_for_update().get(pk=device.pk)
    device.token_set.all().delete()
    tokens = [secrets.token_hex(8) for _ in range(10)]
    StaticToken.objects.bulk_create([StaticToken(device=device, token=token) for token in tokens])
    return tokens


def _render(request, **context):
    return render(
        request,
        "registration/security.html",
        {
            "enrolled": user_has_device(request.user),
            "required": settings.REQUIRE_TWO_FACTOR,
            "recovery_count": StaticToken.objects.filter(device__user=request.user).count(),
            **context,
        },
    )


@login_required
@never_cache
@sensitive_post_parameters("password", "token", "otp_token")
@sensitive_variables("recovery_codes")
def account_security(request):
    """Reauthenticate before enrollment or regeneration; no unauthenticated reset."""
    enrolled = user_has_device(request.user)
    pending = _pending_device(request) if not enrolled else None
    password_form = PasswordConfirmationForm()
    token_form = TokenConfirmationForm()
    recovery_form = RecoveryRegenerationForm()
    recovery_codes = None
    error = ""
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "start" and not enrolled:
            password_form = PasswordConfirmationForm(request.POST)
            if password_form.is_valid():
                if _password_valid(request, password_form):
                    with transaction.atomic():
                        # One serialized enrollment per user, even across tabs.
                        get_user_model().objects.select_for_update().get(pk=request.user.pk)
                        TOTPDevice.objects.filter(user=request.user, confirmed=False).delete()
                        pending = TOTPDevice.objects.create(
                            user=request.user, name="Приложение-аутентификатор", confirmed=False
                        )
                    request.session["otp_setup_device"] = pending.pk
                    request.session["otp_setup_started"] = time.time()
                    return redirect("account_security")
                error = "Не удалось подтвердить пароль."
        elif action == "confirm" and pending:
            token_form = TokenConfirmationForm(request.POST)
            if token_form.is_valid():
                # Serialize enrollment with reset/restart. Validation failures do
                # not raise/roll back, so device throttling is committed as well.
                with transaction.atomic():
                    get_user_model().objects.select_for_update().get(pk=request.user.pk)
                    pending = _pending_device(request)
                    verified = (
                        verify_token(request.user, pending.persistent_id, token_form.cleaned_data["token"])
                        if pending else None
                    )
                    if verified:
                        verified.confirmed = True
                        verified.save(update_fields=["confirmed"])
                        recovery_codes = _replace_recovery_codes(request.user)
                if verified:
                    otp_login(request, verified)
                    request.session.pop("otp_setup_device", None)
                    request.session.pop("otp_setup_started", None)
                    pending = None
                else:
                    error = "Код не принят. Проверьте время телефона; после ошибок подождите перед повтором."
        elif action == "regenerate" and enrolled and request.user.is_verified():
            recovery_form = RecoveryRegenerationForm(request.POST)
            if recovery_form.is_valid():
                if _password_valid(request, recovery_form):
                    model = StaticDevice if recovery_form.cleaned_data.get("otp_method") == "recovery" else TOTPDevice
                    device = model.objects.filter(user=request.user, confirmed=True).order_by("pk").first()
                    token = recovery_form.cleaned_data.get("otp_token", "").replace(" ", "").strip()
                    verified = verify_token(request.user, device.persistent_id, token) if device and token else None
                    if verified:
                        with transaction.atomic():
                            recovery_codes = _replace_recovery_codes(request.user)
                    else:
                        user_login_failed.send(
                            sender=account_security,
                            credentials={"username": request.user.get_username()},
                            request=request,
                        )
                        error = "Не принят код подтверждения. Для выпуска кодов нужен новый действующий код."
                else:
                    error = "Не удалось подтвердить пароль."
        else:
            error = "Действие недоступно. Начните настройку заново."
    base32_key = b32encode(pending.bin_key).decode("ascii") if pending else ""
    return _render(
        request,
        pending=pending,
        base32_key=base32_key,
        password_form=password_form,
        token_form=token_form,
        recovery_form=recovery_form,
        recovery_codes=recovery_codes,
        error=error,
    )


@login_required
@never_cache
@require_GET
def account_security_qr(request):
    pending = _pending_device(request)
    if pending is None or user_has_device(request.user):
        raise Http404
    image = qrcode.make(pending.config_url, image_factory=qrcode.image.svg.SvgPathImage)
    output = io.BytesIO()
    image.save(output)
    response = HttpResponse(output.getvalue(), content_type="image/svg+xml")
    response["Content-Security-Policy"] = "default-src 'none'; style-src 'none'; sandbox"
    return response
