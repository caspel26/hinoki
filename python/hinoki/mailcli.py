"""``hinoki mail``: check and preview the application's email templates.

    hinoki mail check                                   # the project's application
    hinoki mail preview welcome
    hinoki mail preview reset_password --context sample.json --part html

Both build the application the way ``hinoki check`` builds it: no lifespan runs,
no mailer is created and nothing is sent or written. ``check`` compiles every
registered template (reading file templates now) and reports each problem;
``preview`` renders one template from a JSON context, or from the ``examples`` of
its context ``Schema``, and prints the result.

Exit codes: 0 clean, 1 a template problem, 2 the application, the template or the
context cannot be loaded.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from uuid import UUID

from hinoki import jsontext
from hinoki.cli.check import USAGE_ERRORS, build_app
from hinoki.errors import ValidationError
from hinoki.fieldtypes import FieldType
from hinoki.mail import (
    EmailModule,
    RenderedTemplate,
    TemplateBase,
    TemplateError,
)
from hinoki.project import ProjectError
from hinoki.schema import Schema
from hinoki.terminal import shown_path

MAX_CONTEXT_BYTES = 1024 * 1024
PARTS = ("subject", "text", "html")


class MailCliError(Exception):
    """Something the command line cannot continue from (exit code 2)."""


def add_arguments(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    group = commands.add_parser("mail", help="check and preview email templates")
    actions = group.add_subparsers(dest="mail_action", required=True)
    check = actions.add_parser("check", help="compile every registered template")
    preview = actions.add_parser("preview", help="render one template")
    preview.add_argument("name", help="the template's name")
    for sub in (check, preview):
        sub.add_argument(
            "target",
            nargs="?",
            help="module:app, or a factory returning the app (default: project app)",
        )
        sub.add_argument("--format", choices=["text", "json"], default="text")
    preview.add_argument(
        "--context",
        metavar="FILE",
        help="a JSON object to render with ('-' reads standard input); default: "
        "the examples of the template's context Schema",
    )
    preview.add_argument(
        "--part", choices=PARTS, help="print only this part, without a header"
    )


def email_module(target: str) -> EmailModule:
    app = build_app(target)
    for module in app.modules:
        if isinstance(module, EmailModule):
            return module
    raise MailCliError("the application has no email module")


def read_context(source: str) -> object:
    """The JSON of ``--context`` (a file, or ``-`` for standard input)."""
    try:
        if source == "-":
            raw = sys.stdin.buffer.read(MAX_CONTEXT_BYTES + 1)
        else:
            with Path(source).open("rb") as stream:
                raw = stream.read(MAX_CONTEXT_BYTES + 1)
    except OSError as error:
        raise MailCliError(f"cannot read the context: {error.strerror}") from None
    if len(raw) > MAX_CONTEXT_BYTES:
        raise MailCliError(f"the context is larger than {MAX_CONTEXT_BYTES} bytes")
    try:
        return jsontext.loads(raw)
    except ValueError:
        raise MailCliError("the context is not valid JSON") from None


def wire(value: object) -> object:
    """An example as the wire carries it: ``examples=`` may hold Python values
    (a ``date``, a ``Decimal``, an enum member, a ``Schema``)."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date | time):
        return value.isoformat()
    if isinstance(value, Decimal | UUID):
        return str(value)
    return value.dump() if isinstance(value, Schema) else value


def sample_value(ftype: FieldType, where: str) -> object:
    """A wire value for ``ftype`` from its ``Field(examples=...)``, or generated
    from the structure of a nested schema or list."""
    if ftype.info.examples:
        return wire(ftype.info.examples[0])
    if ftype.kind == "schema" and ftype.schema is not None:
        return sample_of(ftype.schema, where)
    if ftype.kind == "list" and ftype.item is not None:
        count = max(1, ftype.info.min_length or 0)
        return [sample_value(ftype.item, f"{where}[]") for _ in range(count)]
    raise MailCliError(f"field '{where}' has no example; pass --context")


def sample_of(schema: type[Schema], where: str = "") -> dict[str, object]:
    prefix = f"{where}." if where else ""
    sample: dict[str, object] = {}
    for name, spec in schema.fields().items():
        if spec.ftype.info.examples or spec.required:
            sample[spec.wire_name] = sample_value(spec.ftype, f"{prefix}{name}")
    return sample


def validated(template: TemplateBase, data: object) -> object:
    schema = template.context_type
    if schema is None:
        if not isinstance(data, dict):
            raise MailCliError("the context must be a JSON object")
        return data
    try:
        return schema.validate(data)
    except ValidationError as error:
        lines = "; ".join(f"{item.path}: {item.message}" for item in error.details)
        raise MailCliError(f"the context does not match: {lines}") from None


def preview_context(template: TemplateBase, source: str | None) -> object:
    if source is not None:
        return validated(template, read_context(source))
    schema = template.context_type
    if schema is None:
        raise MailCliError("the template has no context Schema; pass --context")
    return validated(template, sample_of(schema))


def found(module: EmailModule, name: str) -> TemplateBase:
    for template in module.templates:
        if template.name == name:
            return template
    raise MailCliError(f"unknown template '{name}'")


def render_preview(template: TemplateBase, source: str | None) -> RenderedTemplate:
    context = preview_context(template, source)
    subject, text, html = template.load().render(template.context_tree(context))
    return RenderedTemplate(subject, text, html)


def preview_text(rendered: RenderedTemplate, part: str | None) -> str:
    if part == "subject":
        return rendered.subject
    if part == "text":
        return rendered.text
    if part == "html":
        return rendered.html
    return (
        f"Subject: {rendered.subject}\n"
        f"----- text -----\n{rendered.text}\n"
        f"----- html -----\n{rendered.html}"
    )


def run_preview(module: EmailModule, args: argparse.Namespace) -> int:
    if args.format == "json" and args.part is not None:
        raise MailCliError("--part cannot be combined with --format json")
    template = found(module, args.name)
    rendered = render_preview(template, args.context)
    if args.format == "json":
        document = {
            "subject": rendered.subject,
            "text": rendered.text,
            "html": rendered.html,
        }
        print(jsontext.dumps(document, indent=2))
    else:
        print(preview_text(rendered, args.part))
    return 0


def location(template: TemplateBase) -> str:
    return "inline" if template.path is None else shown_path(template.path)


def check_one(template: TemplateBase) -> TemplateError | None:
    try:
        template.load()
    except TemplateError as error:
        return error
    return None


def run_check(module: EmailModule, args: argparse.Namespace) -> int:
    rows: list[dict[str, object]] = []
    for template in module.templates:
        error = check_one(template)
        rows.append(
            {
                "name": template.name,
                "path": location(template),
                "ok": error is None,
                "error": None if error is None else str(error),
            }
        )
    if args.format == "json":
        print(jsontext.dumps({"templates": rows}, indent=2))
    else:
        for row in rows:
            if row["ok"]:
                print(f"ok  {row['name']}  ({row['path']})")
            else:
                print(row["error"])
    return 0 if all(row["ok"] for row in rows) else 1


def run(args: argparse.Namespace) -> int:
    try:
        module = email_module(args.target)
    except (MailCliError, ProjectError, *USAGE_ERRORS, SystemExit) as error:
        print(f"error: cannot load {args.target}: {error}", file=sys.stderr)
        return 2
    action = run_check if args.mail_action == "check" else run_preview
    try:
        return action(module, args)
    except TemplateError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except MailCliError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
