"""The notes application as a WSGI callable.

Run with ``hinoki serve notes.wsgi:wsgi_app --interface wsgi`` (Kiso) or add
``--server granian``. Prefer ASGI (``notes.main:app``) when the deployment allows it.
"""

from hinoki.wsgi import WSGIAdapter

from .main import app

wsgi_app = WSGIAdapter(app)
