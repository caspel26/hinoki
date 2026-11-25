"""Django settings of the benchmark service on Django Bolt."""

import os
from urllib.parse import urlparse

url = urlparse(os.environ["BENCH_DATABASE_URL"])
POOL = int(os.environ.get("BENCH_POOL", "8"))

SECRET_KEY = "benchmark-only-not-a-secret"
DEBUG = False
ALLOWED_HOSTS = ["*"]
USE_TZ = True
TIME_ZONE = (
    "Etc/UTC"  # equal to the server default, so Django never issues SET TimeZone
)
ROOT_URLCONF = "parityapp.urls"
BOLT_API = ["parityapp.api:api"]
BOLT_COMPRESSION = False
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
# Explicit LOGGING so django_bolt.logging.config.setup_django_logging() respects
# it instead of leaving Django's default django.server logger at INFO (which
# causes Bolt's Rust core to emit an access log record on every request).
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": "WARNING"},
        "django.server": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "django_bolt": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
}
# contrib.auth provides the user loader behind Bolt's ``get_current_user``.
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "parityapp",
    "django_bolt",
]
MIDDLEWARE: list[str] = []
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": url.path.lstrip("/"),
        "USER": url.username,
        "PASSWORD": url.password,
        "HOST": url.hostname,
        "PORT": url.port,
        "CONN_MAX_AGE": 0,  # the connection pool owns connection reuse
        "OPTIONS": {"pool": {"min_size": POOL, "max_size": POOL}},
    }
}
