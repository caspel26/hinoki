"""The messages Mailroom sends: a context ``Schema`` and a template file for each.

The template files are read, parsed and checked against these Schemas when a
worker starts, so a misspelled variable stops the start instead of failing on
the first sign-up.
"""

from pathlib import Path
from typing import Annotated

from hinoki import Field, Schema, mail

HERE = Path(__file__).parent / "templates"


class Account(Schema):
    name: Annotated[str, Field(max_length=80, examples=("Ada",))]
    trial_days: Annotated[int, Field(ge=0, examples=(14,))]


class Step(Schema):
    title: Annotated[str, Field(max_length=80, examples=("Create a project",))]
    url: Annotated[str, Field(format="url", examples=("https://example.com/start",))]


class Welcome(Schema):
    user: Account
    steps: Annotated[list[Step], Field(max_length=10)]


class Device(Schema):
    name: Annotated[str, Field(max_length=80, examples=("Laptop",))]


class ResetPassword(Schema):
    name: Annotated[str, Field(max_length=80, examples=("Ada",))]
    link: Annotated[
        str,
        Field(
            format="url",
            max_length=2048,
            examples=("https://example.com/reset?token=sample",),
        ),
    ]
    requested_from: Annotated[str | None, Field(examples=("Lisbon",))] = None
    devices: Annotated[list[Device], Field(max_length=10)]


WELCOME = mail.Template.file(HERE / "welcome.toml", context=Welcome)
RESET_PASSWORD = mail.Template.file(HERE / "reset_password.toml", context=ResetPassword)

templates: list[mail.TemplateBase] = [WELCOME, RESET_PASSWORD]
