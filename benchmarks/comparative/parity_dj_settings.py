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
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
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
