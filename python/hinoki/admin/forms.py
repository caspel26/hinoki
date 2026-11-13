"""Forms derived from model annotations.

Supported column types become inputs (text, textarea, number, checkbox, UUID,
datetime); anything else is shown read-only rather than guessed. Conversion
from form text is strict and reports one message per field.
"""

from __future__ import annotations

import dataclasses
import datetime
import typing
import uuid
from collections.abc import Mapping
from typing import Any

from hinoki.admin.templates import Raw, Templates, escape
from hinoki.orm import MARK, FieldMeta, Model
from hinoki.runtime.binding import to_float, to_int

TEXTAREA_ABOVE = 200
SHORT = 80


class FieldProblem(ValueError):
    """The submitted text cannot be this field's value."""


@dataclasses.dataclass(frozen=True, slots=True)
class FormField:
    name: str
    label: str
    kind: str  # text, textarea, number, checkbox, uuid, datetime or readonly
    required: bool
    nullable: bool
    has_default: bool
    max_length: int | None
    python_type: Any


def unwrap(annotation: Any) -> tuple[Any, bool]:
    """``(type, nullable)`` for ``T | None``."""
    args = typing.get_args(annotation)
    if args and type(None) in args:
        rest = [a for a in args if a is not type(None)]
        return (rest[0] if len(rest) == 1 else annotation), True
    return annotation, False


SIMPLE_KINDS: dict[Any, str] = {
    bool: "checkbox",
    int: "number",
    float: "number",
    uuid.UUID: "uuid",
    datetime.datetime: "datetime",
}


def kind_of(python_type: Any, max_length: int | None) -> str:
    if python_type is str:
        long = max_length is None or max_length > TEXTAREA_ABOVE
        return "textarea" if long else "text"
    return SIMPLE_KINDS.get(python_type, "readonly")


def fields_for(
    model: type[Model], skip: frozenset[str], creating: bool
) -> list[FormField]:
    """Form fields of ``model``. The primary key is a field only on creation and
    only when it has no default to supply it."""
    hints = typing.get_type_hints(model)
    found = []
    for item in dataclasses.fields(model):  # type: ignore[arg-type]
        meta: FieldMeta = item.metadata.get(MARK, FieldMeta())
        has_default = (
            item.default is not dataclasses.MISSING
            or item.default_factory is not dataclasses.MISSING
        )
        if item.name in skip or (
            meta.primary_key and not (creating and not has_default)
        ):
            continue
        python_type, nullable = unwrap(hints[item.name])
        found.append(
            FormField(
                item.name,
                item.name.replace("_", " ").capitalize(),
                kind_of(python_type, meta.max_length),
                not (has_default or nullable),
                nullable,
                has_default,
                meta.max_length,
                python_type,
            )
        )
    return found


def to_datetime(raw: str) -> datetime.datetime:
    try:
        value = datetime.datetime.fromisoformat(raw)
    except ValueError:
        raise FieldProblem("use a date and time such as 2026-11-13T09:30") from None
    return value if value.tzinfo else value.replace(tzinfo=datetime.UTC)


def to_uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise FieldProblem("not a valid UUID") from None


def to_number(field: FormField, raw: str) -> int | float:
    convert = to_int if field.python_type is int else to_float
    try:
        return convert(raw)
    except ValueError:
        word = "whole number" if field.python_type is int else "number"
        raise FieldProblem(f"enter a {word}") from None


def to_text(field: FormField, raw: str) -> str:
    if field.max_length is not None and len(raw) > field.max_length:
        raise FieldProblem(f"at most {field.max_length} characters")
    return raw


def convert_text(field: FormField, raw: str) -> Any:
    if field.kind == "number":
        return to_number(field, raw)
    if field.kind == "uuid":
        return to_uuid(raw)
    if field.kind == "datetime":
        return to_datetime(raw)
    return to_text(field, raw)


TEXT_KINDS = {"text", "textarea"}


def convert(field: FormField, raw: str | None) -> tuple[bool, Any]:
    """``(present, value)``: ``present`` is False when the field is left out so
    the model default applies. Raises ``FieldProblem``."""
    if field.kind == "checkbox":
        return True, raw is not None
    is_text = field.kind in TEXT_KINDS
    text = raw or "" if is_text else (raw or "").strip()
    if text == "":
        return empty(field, is_text)
    return True, convert_text(field, text)


def empty(field: FormField, is_text: bool) -> tuple[bool, Any]:
    """What an empty submission means for this field."""
    if is_text and not field.required:
        return True, None if field.nullable else ""
    if field.nullable:
        return True, None
    if field.has_default:
        return False, None
    raise FieldProblem("this field is required")


def collect(
    fields: list[FormField], form: Mapping[str, str]
) -> tuple[dict[str, Any], dict[str, str]]:
    """``(values, errors)`` from submitted text. Read-only fields are ignored."""
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for field in fields:
        if field.kind == "readonly":
            continue
        try:
            present, value = convert(field, form.get(field.name))
        except FieldProblem as exc:
            errors[field.name] = str(exc)
        else:
            if present:
                values[field.name] = value
    return values, errors


def show(value: Any) -> str:
    """Plain text for a cell or an input value."""
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        return value.isoformat(timespec="minutes")
    return str(value)


def attributes(field: FormField, error: str | None) -> str:
    parts = [f'id="f-{escape(field.name)}"', f'name="{escape(field.name)}"']
    if field.required and field.kind != "checkbox":
        parts.append("required")
    if field.max_length is not None:
        parts.append(f'maxlength="{field.max_length}"')
    if error:
        parts.append(f'aria-invalid="true" aria-describedby="e-{escape(field.name)}"')
    return " ".join(parts)


INPUT_TYPES: Mapping[str, str] = {
    "text": "text",
    "number": "text",
    "uuid": "text",
    "datetime": "datetime-local",
}


def control(field: FormField, value: Any, error: str | None) -> Raw:
    text = escape(show(value))
    attrs = attributes(field, error)
    if field.kind == "readonly":
        return Raw(
            f'<input id="f-{escape(field.name)}" value="{text}" readonly disabled>'
        )
    if field.kind == "checkbox":
        checked = " checked" if value else ""
        return Raw(f'<input type="checkbox" {attrs}{checked}>')
    if field.kind == "textarea":
        return Raw(f"<textarea {attrs}>{text}</textarea>")
    mode = ' inputmode="decimal"' if field.kind == "number" else ""
    return Raw(f'<input type="{INPUT_TYPES[field.kind]}"{mode} {attrs} value="{text}">')


def render_field(
    templates: Templates, field: FormField, value: Any, error: str | None
) -> Raw:
    notes = []
    if error:
        notes.append(
            f'<p class="error" id="e-{escape(field.name)}">{escape(error)}</p>'
        )
    if field.kind == "readonly":
        notes.append('<p class="help">This type is not editable here.</p>')
    return templates.render(
        "field",
        name=field.name,
        label=field.label,
        control=control(field, value, error),
        help=Raw("".join(notes)),
    )
