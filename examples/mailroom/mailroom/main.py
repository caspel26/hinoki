"""The Mailroom application: settings, the routes and the registered templates.

Importing this module reads the settings (``MAILROOM_API_KEY`` is required) and
opens nothing; the template files are read when a worker starts.
"""

from hinoki import Hinoki

from .messages import templates
from .routes import build_routes
from .settings import MailroomSettings


def build_app(settings: MailroomSettings) -> Hinoki:
    """The application; the routes need the API key and the public base URL."""
    return Hinoki(
        settings=settings,
        title="Mailroom",
        include=build_routes(settings),
        email_templates=templates,
    )


app = build_app(MailroomSettings.load())
