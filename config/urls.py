from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse
from django.db import connection
def health(request):
    try:
        with connection.cursor() as cursor:cursor.execute("SELECT 1")
        return JsonResponse({"status":"ok"})
    except Exception:return JsonResponse({"status":"unavailable"},status=503)
from django.contrib.auth import views as auth_views
urlpatterns=[path('health/',health,name='health'),path('admin/',admin.site.urls),path('accounts/login/',auth_views.LoginView.as_view(template_name='registration/login.html'),name='login'),path('accounts/logout/',auth_views.LogoutView.as_view(),name='logout'),path('',include('monitoring.ui_urls'))]
admin.site.site_header='Центр финансовой грамотности'
admin.site.site_title='Администрирование мониторинга'
admin.site.index_title='Справочники и настройки'
