"""Django settings of the Django Ninja service: the Bolt settings without Bolt."""

import parity_data
from parity_dj_settings import (  # noqa: F401 - Django reads these module globals
    ALLOWED_HOSTS,
    DATABASES,
    DEBUG,
    DEFAULT_AUTO_FIELD,
    MIDDLEWARE,
    SECRET_KEY,
    TIME_ZONE,
    USE_TZ,
)

ROOT_URLCONF = "parity_ninja"
INSTALLED_APPS = ["django.contrib.contenttypes", "django.contrib.auth", "parityapp"]
# django-ninja-jwt validates the shared token: HS256, the user id in ``sub``, and
# no token-type or ``jti`` claim, which the other stacks' tokens do not carry.
NINJA_JWT = {
    "SIGNING_KEY": parity_data.SECRET,
    "ALGORITHM": parity_data.ALGORITHM,
    "USER_ID_CLAIM": "sub",
    "TOKEN_TYPE_CLAIM": None,
    "JTI_CLAIM": None,
}
