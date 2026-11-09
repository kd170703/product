import os
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY','development-only-change-before-deployment')
DEBUG = os.environ.get('DJANGO_DEBUG','0') == '1'
ALLOWED_HOSTS = [v.strip() for v in os.environ.get('DJANGO_ALLOWED_HOSTS','localhost,127.0.0.1,[::1],testserver').split(',')]
CSRF_TRUSTED_ORIGINS = [v for v in os.environ.get('DJANGO_CSRF_TRUSTED_ORIGINS','').split(',') if v]
INSTALLED_APPS = ['django.contrib.admin','django.contrib.auth','django.contrib.contenttypes','django.contrib.sessions','django.contrib.messages','django.contrib.staticfiles','monitoring']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware','whitenoise.middleware.WhiteNoiseMiddleware','django.contrib.sessions.middleware.SessionMiddleware','django.middleware.common.CommonMiddleware','django.middleware.csrf.CsrfViewMiddleware','django.contrib.auth.middleware.AuthenticationMiddleware','django.contrib.messages.middleware.MessageMiddleware','django.middleware.clickjacking.XFrameOptionsMiddleware']
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{'BACKEND':'django.template.backends.django.DjangoTemplates','DIRS':[BASE_DIR/'templates'],'APP_DIRS':True,'OPTIONS':{'context_processors':['django.template.context_processors.request','django.contrib.auth.context_processors.auth','django.contrib.messages.context_processors.messages','monitoring.context_processors.navigation']}}]
WSGI_APPLICATION = 'config.wsgi.application'
DATABASES = {'default':{'ENGINE':'django.db.backends.postgresql','NAME':os.environ.get('POSTGRES_DB','product'),'USER':os.environ.get('POSTGRES_USER','product'),'PASSWORD':os.environ.get('POSTGRES_PASSWORD',''),'HOST':os.environ.get('POSTGRES_HOST','127.0.0.1'),'PORT':os.environ.get('POSTGRES_PORT','5432'),'CONN_MAX_AGE':60}}
LANGUAGE_CODE='ru-ru'
TIME_ZONE='Europe/Moscow'
USE_I18N=True
USE_TZ=True
STATIC_URL='/static/'
STATIC_ROOT=BASE_DIR/'staticfiles'
STATICFILES_DIRS=[BASE_DIR/'static']
MEDIA_ROOT=Path(os.environ.get('MEDIA_ROOT',str(BASE_DIR/'media')))
# Media deliberately has NO public URL. Downloads require application permissions.
MEDIA_URL='/protected-media/'
DEFAULT_AUTO_FIELD='django.db.models.BigAutoField'
LOGIN_URL='login'
LOGIN_REDIRECT_URL='monitoring:dashboard'
LOGOUT_REDIRECT_URL='login'
AUTH_PASSWORD_VALIDATORS=[{'NAME':'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},{'NAME':'django.contrib.auth.password_validation.MinimumLengthValidator'},{'NAME':'django.contrib.auth.password_validation.CommonPasswordValidator'},{'NAME':'django.contrib.auth.password_validation.NumericPasswordValidator'}]
OPENAI_API_KEY=os.environ.get('OPENAI_API_KEY','')
OPENAI_MODEL=os.environ.get('OPENAI_MODEL','gpt-4.1-mini')
IMPORT_WATCH_DIR=Path(os.environ.get('IMPORT_WATCH_DIR',str(BASE_DIR/'incoming')))
DATA_UPLOAD_MAX_MEMORY_SIZE=30*1024*1024
FILE_UPLOAD_MAX_MEMORY_SIZE=5*1024*1024
SESSION_COOKIE_HTTPONLY=True
SESSION_COOKIE_SAMESITE='Lax'
SECURE_CONTENT_TYPE_NOSNIFF=True
X_FRAME_OPTIONS='DENY'
SESSION_COOKIE_SECURE=os.environ.get('DJANGO_HTTPS','0')=='1'
CSRF_COOKIE_SECURE=SESSION_COOKIE_SECURE
SECURE_SSL_REDIRECT=SESSION_COOKIE_SECURE
SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO','https') if os.environ.get('TRUST_PROXY','0')=='1' else None

CSRF_FAILURE_VIEW='monitoring.ui_views.csrf_failure'
