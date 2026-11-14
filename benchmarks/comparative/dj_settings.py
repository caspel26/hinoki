"""Django settings shared by the Ninja and Bolt services."""

import os
from urllib.parse import urlparse

url = urlparse(os.environ["BENCH_DATABASE_URL"])
POOL = int(os.environ.get("BENCH_POOL", "10"))

SECRET_KEY = "benchmark-only-not-a-secret"
DEBUG = False
ALLOWED_HOSTS = ["*"]
USE_TZ = True
TIME_ZONE = (
    "Etc/UTC"  # equal to the server default, so Django never issues SET TimeZone
)
ROOT_URLCONF = "ninja_app"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
INSTALLED_APPS = [
    "benchapp",
    *(["django_bolt"] if os.environ.get("BENCH_BOLT") else []),
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
