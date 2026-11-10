from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.db import connection
from django.http import JsonResponse
from django.urls import include, path

from monitoring import auth_views as secure_auth


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return JsonResponse({"status": "ok"})
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)


urlpatterns = [
    path("health/", health, name="health"),
    path("admin/login/", secure_auth.admin_login),
    path("admin/", admin.site.urls),
    path("accounts/login/", secure_auth.SecureLoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/security/", secure_auth.account_security, name="account_security"),
    path("accounts/security/qr/", secure_auth.account_security_qr, name="account_security_qr"),
    path("", include("monitoring.ui_urls")),
]
admin.site.site_header = "Центр финансовой грамотности"
admin.site.site_title = "Администрирование мониторинга"
admin.site.index_title = "Справочники и настройки"
