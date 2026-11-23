"""Notices written as strings, the way a database table of them would be.

``NOTICES`` stands in for that table. A notice has no context ``Schema``, so its
template takes a ``dict`` and only its syntax is checked when it is built; the
variables are checked when it is rendered.
"""

from hinoki import mail

NOTICES: dict[str, tuple[str, str]] = {
    "maintenance": (
        "Maintenance on {{ day }}",
        "<p>Mailroom is read-only on {{ day }} from {{ from }} to {{ until }}.</p>",
    ),
}


def notice_template(slug: str) -> mail.Template[mail.Context]:
    """The template of a stored notice; ``KeyError`` when there is none."""
    subject, html = NOTICES[slug]
    return mail.Template(name=f"notice-{slug}", subject=subject, html=html)
