from django.urls import path
from . import ui_views as views
from .downloads import download
app_name = 'monitoring'
urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('sections/<str:section>/', views.dashboard, name='section'),
    path('observations/new/', views.observation_create, name='observation_create'),
    path('observations/<int:pk>/', views.observation_detail, name='observation_detail'),
    path('indicators/new/', views.indicator_edit, name='indicator_create'),
    path('indicators/<int:pk>/edit/', views.indicator_edit, name='indicator_update'),
    path('sources/', views.sources, name='sources'),
    path('sources/new/', views.source_edit, name='source_create'),
    path('sources/<int:pk>/edit/', views.source_edit, name='source_update'),
    path('sources/<int:pk>/run/', views.enqueue_source, name='enqueue_source'),
    path('topics/new/', views.topic_edit, name='topic_create'),
    path('topics/<int:pk>/edit/', views.topic_edit, name='topic_update'),
    path('topics/aliases/new/', views.alias_create, name='alias_create'),
    path('issues/<int:pk>/resolve/', views.resolve_issue, name='resolve_issue'),
    path('imports/', views.imports, name='imports'),
    path('imports/<int:pk>/', views.import_detail, name='import_detail'),
    path('reports/', views.reports, name='reports'),
    path('gpt/', views.gpt, name='gpt'),
    path('download/<str:kind>/<int:pk>/<str:fmt>/', download, name='download'),
]
