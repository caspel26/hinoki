"""Mailroom tests send through the memory backend: nothing leaves the process."""

import os

# ``mailroom.main`` builds the application on import, and the email backend has no
# default: choose the in-process one before anything imports it.
os.environ.setdefault("MAILROOM_EMAIL__BACKEND", "memory")
os.environ.setdefault("MAILROOM_EMAIL__DEFAULT_SENDER", "noreply@example.com")
os.environ.setdefault("MAILROOM_API_KEY", "test-key")  # synthetic
