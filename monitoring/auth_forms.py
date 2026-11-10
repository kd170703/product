"""A password and, once enrolled, one explicitly chosen TOTP/recovery device."""
from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.signals import user_login_failed
from django_otp import user_has_device, verify_token
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice


class TwoFactorAuthenticationForm(AuthenticationForm):
    otp_method = forms.ChoiceField(
        label="Способ подтверждения",
        choices=[("totp", "Код из приложения"), ("recovery", "Резервный код")],
        required=False,
    )
    otp_token = forms.CharField(
        label="Код подтверждения",
        required=False,
        max_length=32,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code"}),
    )

    def clean(self):
        cleaned = super().clean()
        user = self.get_user()
        if user is None or not user_has_device(user):
            return cleaned
        model = StaticDevice if cleaned.get("otp_method") == "recovery" else TOTPDevice
        device = model.objects.filter(user=user, confirmed=True).order_by("pk").first()
        token = cleaned.get("otp_token", "").replace(" ", "").strip()
        verified = verify_token(user, device.persistent_id, token) if device and token else None
        if verified is None:
            user_login_failed.send(
                sender=type(self),
                credentials={"username": user.get_username()},
                request=self.request,
            )
            raise forms.ValidationError(
                "Для этой учётной записи нужен действующий код из приложения "
                "или неиспользованный резервный код.",
                code="invalid_otp",
            )
        user.otp_device = verified
        return cleaned


class PasswordConfirmationForm(forms.Form):
    password = forms.CharField(
        label="Текущий пароль",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )


class TokenConfirmationForm(forms.Form):
    token = forms.CharField(
        label="Код из приложения",
        max_length=8,
        min_length=6,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}),
    )


class RecoveryRegenerationForm(PasswordConfirmationForm):
    otp_method = TwoFactorAuthenticationForm.base_fields["otp_method"]
    otp_token = TwoFactorAuthenticationForm.base_fields["otp_token"]
