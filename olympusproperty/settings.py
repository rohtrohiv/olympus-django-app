from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env
load_dotenv(BASE_DIR / ".env")

# ======================
# CORE SETTINGS
# ======================
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("DJANGO_SECRET_KEY is not set")

DEBUG = os.getenv("DJANGO_DEBUG", "False") == "True"

ENV = os.getenv("DJANGO_ENV", "production")

ALLOWED_HOSTS = [host for host in os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if host]

# CSRF_TRUSTED_ORIGINS = os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")

CSRF_TRUSTED_ORIGINS = [
    origin for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if origin
]


if ENV == "production":
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    USE_X_FORWARDED_HOST = True
else:
    # test / local
    CSRF_COOKIE_SECURE = False
    SESSION_COOKIE_SECURE = False
    SECURE_PROXY_SSL_HEADER = None
    USE_X_FORWARDED_HOST = False

# ======================
# AZURE AD SSO
# ======================
AZURE_AD_TENANT_ID = os.getenv("AZURE_AD_TENANT_ID")
AZURE_AD_CLIENT_ID = os.getenv("AZURE_AD_CLIENT_ID")
AZURE_AD_CLIENT_SECRET = os.getenv("AZURE_AD_CLIENT_SECRET")
AZURE_AD_REDIRECT_URI = os.getenv("AZURE_AD_REDIRECT_URI")
print(f"REDIRECT URI: {AZURE_AD_REDIRECT_URI}")
AZURE_AD_AUDIENCE = os.getenv("AZURE_AD_AUDIENCE", AZURE_AD_CLIENT_ID)
AZURE_AD_MIRROR_GROUPS = os.getenv("AZURE_AD_MIRROR_GROUPS", "False").lower() in {"1", "true", "t", "yes", "on"}
AZURE_AD_USERNAME_CLAIM = os.getenv("AZURE_AD_USERNAME_CLAIM", "upn")
AZURE_AD_CLAIM_FIRST_NAME = os.getenv("AZURE_AD_CLAIM_FIRST_NAME", "given_name")
AZURE_AD_CLAIM_LAST_NAME = os.getenv("AZURE_AD_CLAIM_LAST_NAME", "family_name")
AZURE_AD_CLAIM_EMAIL = os.getenv("AZURE_AD_CLAIM_EMAIL", "email")
AZURE_AD_AUTHORITY = os.getenv("AZURE_AD_AUTHORITY") or (
    f"https://login.microsoftonline.com/{AZURE_AD_TENANT_ID}" if AZURE_AD_TENANT_ID else None
)
ENABLE_AZURE_SSO = bool(AZURE_AD_TENANT_ID and AZURE_AD_CLIENT_ID)
# ======================
# APPLICATIONS
# ======================
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'dashboard',
]

if ENABLE_AZURE_SSO:
    INSTALLED_APPS.append('django_auth_adfs')

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
]

if ENABLE_AZURE_SSO:
    AUTHENTICATION_BACKENDS.insert(0, 'django_auth_adfs.backend.AdfsAuthCodeBackend')

if ENABLE_AZURE_SSO:
    AUTH_ADFS = {
        # REQUIRED
        # "SERVER": "login.microsoftonline.com",
        "TENANT_ID": AZURE_AD_TENANT_ID,

        # Azure App Registration
        "CLIENT_ID": AZURE_AD_CLIENT_ID,
        "CLIENT_SECRET": AZURE_AD_CLIENT_SECRET,

        # REQUIRED (fixes your original error)
        "RELYING_PARTY_ID": AZURE_AD_CLIENT_ID,
        "AUDIENCE": AZURE_AD_AUDIENCE,

        "MIRROR_GROUPS": AZURE_AD_MIRROR_GROUPS,
        "USERNAME_CLAIM": AZURE_AD_USERNAME_CLAIM,

        "CLAIM_MAPPING": {
            "first_name": AZURE_AD_CLAIM_FIRST_NAME,
            "last_name": AZURE_AD_CLAIM_LAST_NAME,
            "email": AZURE_AD_CLAIM_EMAIL,
        },
    }

    # if AZURE_AD_REDIRECT_URI:
    #     AUTH_ADFS["REDIRECT_URI"] = AZURE_AD_REDIRECT_URI
else:
    AUTH_ADFS = {}


LOGIN_URL = 'django_auth_adfs:login' if ENABLE_AZURE_SSO else 'login'
LOGIN_REDIRECT_URL = os.getenv('DJANGO_LOGIN_REDIRECT_URL', '/')
LOGOUT_REDIRECT_URL = os.getenv('DJANGO_LOGOUT_REDIRECT_URL', '/')

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'olympusproperty.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'dashboard.context_processors.auth_settings',
            ],
        },
    },
]

WSGI_APPLICATION = 'olympusproperty.wsgi.application'

# ======================
# DATABASE
# ======================
DATABASES = {
    'default': {
        'ENGINE': os.getenv("DB_ENGINE"),
        'NAME': os.getenv("DB_NAME"),
        'USER': os.getenv("DB_USER"),
        'PASSWORD': os.getenv("DB_PASSWORD"),
        'HOST': os.getenv("DB_HOST"),
        'PORT': os.getenv("DB_PORT"),
        'OPTIONS': {
            'options': f"-c search_path={os.getenv('DB_SCHEMA', 'public')}"
        },
        'CONN_MAX_AGE': int(os.getenv("DB_CONN_MAX_AGE", 600)),
    }
}

# ======================
# PASSWORD VALIDATION
# ======================
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ======================
# I18N
# ======================
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# ======================
# STATIC FILES
# ======================
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ======================
# CACHING
# ======================
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'financial-reporting-cache',
        'OPTIONS': {'MAX_ENTRIES': 1000}
    }
}
