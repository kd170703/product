"""Password throttling and session-wide enforcement of an enrolled second factor."""
from ipaddress import ip_address

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect
from django.urls import reverse
from django_otp import user_has_device


def client_ip(request):
    """Trust one address only when an isolated reverse proxy overwrites the header."""
    candidate = (
        request.META.get("HTTP_X_FORWARDED_FOR", "")
        if settings.TRUST_PROXY
        else request.META.get("REMOTE_ADDR", "")
    )
    try:
        return str(ip_address(candidate))
    except ValueError:
        # A list is deliberately rejected; Caddy supplies precisely the real peer.
        try:
            return str(ip_address(request.META.get("REMOTE_ADDR", "")))
        except ValueError:
            return None


class TwoFactorMiddleware:
    """No view, API, export or Django admin can bypass enrolled-device verification."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            login_url = reverse("login")
            security_url = reverse("account_security")
            allowed = {login_url, reverse("logout"), reverse("health")}
            security_paths = {security_url, reverse("account_security_qr")}
            verified = request.user.is_verified()
            enrolled = user_has_device(request.user)
            if not verified and request.path not in allowed:
                if enrolled:
                    # Even existing password-only sessions become insufficient as soon
                    # as the user enrolls a device. Never exempt the Django admin.
                    return redirect_to_login(request.get_full_path(), login_url)
                if settings.REQUIRE_TWO_FACTOR and request.path not in security_paths:
                    return redirect(security_url)
        return self.get_response(request)
