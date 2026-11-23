"""The Mailroom application: settings, the routes and the registered templates.

Importing this module reads the settings and opens nothing; the template files
are read when a worker starts.
"""

from hinoki import Hinoki

from .messages import templates
from .routes import routes
from .settings import MailroomSettings

app = Hinoki(
    settings=MailroomSettings,
    title="Mailroom",
    include=routes,
    email_templates=templates,
)
