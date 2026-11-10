"""Server settings; secrets come from environment variables or mounted files."""
import os
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def read_secret(name, *, required=False):
    """Read one secret without silently replacing a missing or unreadable value."""
    value = os.environ.get(name)
    file_name = os.environ.get(name + "_FILE")
    if value and file_name:
        raise ImproperlyConfigured(f"Задайте только {name} или {name}_FILE.")
    if file_name:
        try:
            value = Path(file_name).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise ImproperlyConfigured(f"Не удалось прочитать файл {name}_FILE.") from exc
    value = value or ""
    if required and not value.strip():
        raise ImproperlyConfigured(f"Не задан {name}. Укажите переменную или {name}_FILE.")
    return value


SECRET_KEY = read_secret("DJANGO_SECRET_KEY", required=True)
if SECRET_KEY in {"development-only-change-before-deployment", "replace-with-a-long-random-value"}:
    raise ImproperlyConfigured("Замените небезопасный DJANGO_SECRET_KEY случайным секретом.")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = [
    value.strip()
    for value in os.environ.get(
        "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver"
    ).split(",")
    if value.strip()
]
CSRF_TRUSTED_ORIGINS = [
    value.strip()
    for value in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if value.strip()
]
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "django_otp.plugins.otp_static",
    "axes",
    "monitoring",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "monitoring.security.TwoFactorMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "builtins": ["monitoring.templatetags.ui_icons"],
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "monitoring.context_processors.navigation",
            ],
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "product"),
        "USER": os.environ.get("POSTGRES_USER", "product"),
        "PASSWORD": read_secret("POSTGRES_PASSWORD"),
        "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}
LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", str(BASE_DIR / "media")))
# Files are downloaded through permission-checked views, never as public media.
MEDIA_URL = "/protected-media/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "monitoring:dashboard"
LOGOUT_REDIRECT_URL = "login"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

# Axes uses PostgreSQL, so counters survive container restarts and cover workers.
AXES_HANDLER = "monitoring.axes_handler.MinimalAxesHandler"
AXES_FAILURE_LIMIT = int(os.environ.get("DJANGO_AXES_FAILURE_LIMIT", "5"))
AXES_COOLOFF_TIME = timedelta(
    minutes=int(os.environ.get("DJANGO_AXES_COOLOFF_MINUTES", "30"))
)
AXES_LOCKOUT_PARAMETERS = ["username", "ip_address"]
AXES_RESET_ON_SUCCESS = True
AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT = False
AXES_DISABLE_ACCESS_LOG = True
AXES_ENABLE_ACCESS_FAILURE_LOG = False
AXES_VERBOSE = False
AXES_SENSITIVE_PARAMETERS = [
    "username", "ip_address", "user_agent", "otp_token", "token", "csrfmiddlewaretoken"
]
AXES_CLIENT_IP_CALLABLE = "monitoring.security.client_ip"
AXES_LOCKOUT_TEMPLATE = "registration/locked_out.html"
AXES_HTTP_RESPONSE_CODE = 429
AXES_ENABLE_RETRY_AFTER_HEADER = True

REQUIRE_TWO_FACTOR = os.environ.get("DJANGO_REQUIRE_2FA", "0") == "1"
OTP_TOTP_ISSUER = "Центр финансовой грамотности"
OTP_TOTP_THROTTLE_FACTOR = 2
OTP_STATIC_THROTTLE_FACTOR = 2

OPENAI_API_KEY = read_secret("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
# Production Compose explicitly sets this to 0; a key alone cannot opt it in there.
OPENAI_ENABLED = os.environ.get("OPENAI_ENABLED", "0") == "1"
IMPORT_WATCH_DIR = Path(os.environ.get("IMPORT_WATCH_DIR", str(BASE_DIR / "incoming")))
DATA_UPLOAD_MAX_MEMORY_SIZE = 30 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 8 * 60 * 60
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_SECURE = os.environ.get("DJANGO_HTTPS", "0") == "1"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SECURE_SSL_REDIRECT = SESSION_COOKIE_SECURE
# Docker checks the internal HTTP endpoint; no application data is exposed here.
SECURE_REDIRECT_EXEMPT = [r"^health/$"]
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if TRUST_PROXY else None
SECURE_HSTS_SECONDS = 31536000 if SESSION_COOKIE_SECURE else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
CSRF_FAILURE_VIEW = "monitoring.ui_views.csrf_failure"
