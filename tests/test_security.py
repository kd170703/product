"""Exercise real PostgreSQL counters, OTP verification and authenticated routes."""
import io
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from axes.models import AccessAttempt, AccessLog
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.oath import totp
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from monitoring.security import client_ip
from monitoring.services.gpt import ConfigurationError, extract_records

PASSWORD = "Correct-horse-battery-704!"


def token_for(device):
    return str(totp(device.bin_key, step=device.step, t0=device.t0, digits=device.digits, drift=device.drift)).zfill(device.digits)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AuthenticationSecurityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("secure-viewer", password=PASSWORD)
        self.user.user_permissions.add(Permission.objects.get(codename="view_observation", content_type__app_label="monitoring"))
        self.login_url = reverse("login")
        self.security_url = reverse("account_security")

    def login(self, **extra):
        return self.client.post(self.login_url, {"username": self.user.username, "password": PASSWORD, **extra})

    def enroll(self):
        return TOTPDevice.objects.create(user=self.user, name="Приложение", confirmed=True)

    def test_ordinary_first_login_and_password_lockout_survive_new_client(self):
        self.assertEqual(self.login().status_code, 302)
        self.client.logout()
        for _ in range(settings.AXES_FAILURE_LIMIT):
            self.client.post(self.login_url, {"username": self.user.username, "password": "incorrect"})
        response = Client().post(self.login_url, {"username": self.user.username, "password": PASSWORD})
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response)
        self.assertContains(response, "Вход временно ограничен", status_code=429)
        self.assertTrue(AccessAttempt.objects.filter(username=self.user.username).exists())
        self.assertFalse(AccessLog.objects.exists())

    def test_failed_login_retains_only_counter_not_post_or_fingerprint(self):
        self.client.post(self.login_url + "?private=employee-name", {
            "username": self.user.username, "password": "secret-never-log", "otp_token": "987654",
            "unrelated": "employee-name",
        }, HTTP_USER_AGENT="a-very-detailed-fingerprint")
        attempt = AccessAttempt.objects.get(username=self.user.username)
        self.assertEqual(attempt.post_data, "")
        self.assertEqual(attempt.get_data, "")
        self.assertEqual(attempt.user_agent, "")
        self.assertEqual(attempt.http_accept, "")
        self.assertEqual(attempt.failures_since_start, 1)

    def test_enrolled_password_alone_fails_real_totp_login_succeeds_and_cannot_replay(self):
        device = self.enroll()
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        code = token_for(device)
        response = self.login(otp_token=code, otp_method="totp")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session[DEVICE_ID_SESSION_KEY], device.persistent_id)
        self.assertEqual(self.client.get(reverse("monitoring:dashboard")).status_code, 200)
        self.client.logout()
        self.assertEqual(self.login(otp_token=code, otp_method="totp").status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_recovery_code_is_single_use(self):
        self.enroll()
        recovery = StaticDevice.objects.create(user=self.user, name="Резервные коды")
        StaticToken.objects.create(device=recovery, token="12ab34cd56ef7890")
        self.assertEqual(self.login(otp_method="recovery", otp_token="12ab34cd56ef7890").status_code, 302)
        self.assertEqual(self.client.session[DEVICE_ID_SESSION_KEY], recovery.persistent_id)
        self.assertFalse(recovery.token_set.exists())
        self.client.logout()
        self.assertEqual(self.login(otp_method="recovery", otp_token="12ab34cd56ef7890").status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_password_only_existing_session_cannot_bypass_via_admin_export_or_api(self):
        self.client.force_login(self.user)
        self.enroll()
        for path in ["/", "/admin/", "/reports/", "/api/charts/", self.security_url]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertTrue(response.url.startswith(self.login_url + "?next="), path)
        self.assertEqual(self.client.get("/admin/login/").url.split("?")[0], self.login_url)

    @override_settings(REQUIRE_TWO_FACTOR=True)
    def test_mandatory_enrollment_blocks_application_until_confirmed(self):
        self.assertRedirects(self.login(), self.security_url, fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/"), self.security_url, fetch_redirect_response=False)
        self.assertContains(self.client.get(self.security_url), "Администратор сделал её обязательной")
        self.assertRedirects(self.client.get("/admin/"), self.security_url, fetch_redirect_response=False)
        self.assertEqual(self.client.get("/health/").status_code, 200)

    def test_setup_requires_password_confirmed_token_and_shows_codes_once(self):
        self.client.force_login(self.user)
        self.client.post(self.security_url, {"action": "start", "password": "wrong"})
        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())
        self.assertEqual(self.client.post(self.security_url, {"action": "start", "password": PASSWORD}).status_code, 302)
        device = TOTPDevice.objects.get(user=self.user, confirmed=False)
        response = self.client.get(reverse("account_security_qr"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertIn("no-store", response["Cache-Control"])
        response = self.client.post(self.security_url, {"action": "confirm", "token": token_for(device)})
        self.assertEqual(response.status_code, 200)
        device.refresh_from_db()
        self.assertTrue(device.confirmed)
        codes = list(StaticToken.objects.filter(device__user=self.user).values_list("token", flat=True))
        self.assertEqual(len(codes), 10)
        for code in codes:
            self.assertContains(response, code)
        follow_up = self.client.get(self.security_url)
        for code in codes:
            self.assertNotContains(follow_up, code)
        self.assertEqual(self.client.session[DEVICE_ID_SESSION_KEY], device.persistent_id)
        self.assertEqual(self.client.get(reverse("account_security_qr")).status_code, 404)

    def test_regenerating_recovery_codes_requires_password_and_new_code(self):
        device = self.enroll()
        recovery = StaticDevice.objects.create(user=self.user, name="Резервные коды")
        original_code = "bbaa445566778899"
        StaticToken.objects.create(device=recovery, token=original_code)
        self.assertEqual(self.login(otp_token=token_for(device), otp_method="totp").status_code, 302)
        self.client.post(self.security_url, {
            "action": "regenerate", "password": "wrong", "otp_method": "recovery", "otp_token": original_code,
        })
        self.assertTrue(StaticToken.objects.filter(device=recovery, token=original_code).exists())
        response = self.client.post(self.security_url, {
            "action": "regenerate", "password": PASSWORD, "otp_method": "recovery", "otp_token": original_code,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Сохраните резервные коды")
        codes = list(StaticToken.objects.filter(device=recovery).values_list("token", flat=True))
        self.assertEqual(len(codes), 10)
        self.assertNotIn(original_code, codes)
        self.assertEqual(len(set(codes)), 10)
        self.assertNotContains(self.client.get(self.security_url), codes[0])

    def test_expired_setup_and_other_user_cannot_get_qr_or_confirm(self):
        self.client.force_login(self.user)
        self.client.post(self.security_url, {"action": "start", "password": PASSWORD})
        device = TOTPDevice.objects.get(user=self.user, confirmed=False)
        session = self.client.session
        session["otp_setup_started"] = time.time() - 601
        session.save()
        self.assertEqual(self.client.get(reverse("account_security_qr")).status_code, 404)
        self.client.post(self.security_url, {"action": "confirm", "token": token_for(device)})
        device.refresh_from_db()
        self.assertFalse(device.confirmed)
        other = get_user_model().objects.create_user("other", password=PASSWORD)
        self.client.force_login(other)
        session = self.client.session
        session["otp_setup_device"] = device.pk
        session["otp_setup_started"] = time.time()
        session.save()
        self.assertEqual(self.client.get(reverse("account_security_qr")).status_code, 404)

    def test_security_changes_enforce_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(self.security_url, {"action": "start", "password": PASSWORD}).status_code, 403)

    def test_server_recovery_revokes_devices_and_existing_sessions(self):
        self.enroll()
        self.client.force_login(self.user)
        call_command("reset_two_factor", self.user.username, reason="Личность проверена администратором", stdout=io.StringIO())
        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())
        self.assertRedirects(self.client.get("/"), "/accounts/login/?next=/", fetch_redirect_response=False)

    def test_old_auth_records_are_cleaned_but_active_lockout_is_kept(self):
        self.client.post(self.login_url, {"username": self.user.username, "password": "wrong"})
        old = AccessAttempt.objects.create(username="old", ip_address="192.0.2.1", user_agent="", get_data="", post_data="", failures_since_start=5, attempt_time=timezone.now() - timedelta(days=2))
        AccessAttempt.objects.filter(pk=old.pk).update(attempt_time=timezone.now() - timedelta(days=2))
        pending = TOTPDevice.objects.create(user=self.user, name="old-setup", confirmed=False)
        TOTPDevice.objects.filter(pk=pending.pk).update(created_at=timezone.now() - timedelta(days=2))
        call_command("cleanup_auth", stdout=io.StringIO())
        self.assertFalse(AccessAttempt.objects.filter(pk=old.pk).exists())
        self.assertTrue(AccessAttempt.objects.filter(username=self.user.username).exists())
        self.assertFalse(TOTPDevice.objects.filter(pk=pending.pk).exists())


class ConfigurationSecurityTests(SimpleTestCase):
    def settings_process(self, extra=None):
        env = {key: value for key, value in os.environ.items() if key not in {"DJANGO_SECRET_KEY", "DJANGO_SECRET_KEY_FILE", "OPENAI_API_KEY", "OPENAI_API_KEY_FILE", "POSTGRES_PASSWORD_FILE"}}
        env.update(extra or {})
        return subprocess.run([sys.executable, "-c", "import config.settings"], cwd=settings.BASE_DIR, env=env, text=True, capture_output=True)

    def test_missing_or_legacy_secret_key_fails_closed(self):
        result = self.settings_process()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Не задан DJANGO_SECRET_KEY", result.stderr)
        result = self.settings_process({"DJANGO_SECRET_KEY": "development-only-change-before-deployment"})
        self.assertNotEqual(result.returncode, 0)

    @override_settings(SECURE_SSL_REDIRECT=True)
    def test_internal_health_endpoint_does_not_redirect_to_https(self):
        client = Client()
        # This endpoint remains database-aware and is the only HTTP exception.
        # SimpleTestCase cannot query DB, so only verify middleware routing here.
        with patch("config.urls.connection") as connection:
            response = client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get("/accounts/login/").status_code, 301)

    def test_secret_file_works_and_unreadable_file_fails(self):
        with tempfile.TemporaryDirectory() as root:
            secret = Path(root) / "django-key"
            secret.write_text("test-long-random-secret-value\n")
            self.assertEqual(self.settings_process({"DJANGO_SECRET_KEY_FILE": str(secret)}).returncode, 0)
            self.assertNotEqual(self.settings_process({"DJANGO_SECRET_KEY_FILE": str(secret) + "-missing"}).returncode, 0)

    @override_settings(OPENAI_ENABLED=False, OPENAI_API_KEY="present-but-not-opted-in")
    def test_external_api_is_not_called_without_explicit_opt_in(self):
        with patch("openai.OpenAI") as mocked:
            with self.assertRaises(ConfigurationError):
                extract_records("данные", "", [])
            mocked.assert_not_called()

    @override_settings(TRUST_PROXY=False)
    def test_untrusted_forwarded_address_is_ignored(self):
        request = type("Request", (), {"META": {"REMOTE_ADDR": "192.0.2.10", "HTTP_X_FORWARDED_FOR": "198.51.100.99"}})()
        self.assertEqual(client_ip(request), "192.0.2.10")

    @override_settings(TRUST_PROXY=True)
    def test_proxy_address_must_be_one_valid_ip_not_an_arbitrary_chain(self):
        request = type("Request", (), {"META": {"REMOTE_ADDR": "192.0.2.10", "HTTP_X_FORWARDED_FOR": "198.51.100.99"}})()
        self.assertEqual(client_ip(request), "198.51.100.99")
        request.META["HTTP_X_FORWARDED_FOR"] = "198.51.100.99, 203.0.113.5"
        self.assertEqual(client_ip(request), "192.0.2.10")
