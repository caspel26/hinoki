"""Forms derived from model annotations.

Supported column types become inputs (text, textarea, number, checkbox, UUID,
datetime, or a select for a foreign key); anything else is shown read-only
rather than guessed. Conversion from form text is strict and reports one
message per field. On edit, only fields the user actually changed are
converted and saved, so untouched values are never rewritten by a browser's
re-encoding (for example a datetime that a ``datetime-local`` input cannot show).
"""

from __future__ import annotations

import dataclasses
import datetime
import typing
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from hinoki.admin.templates import Raw, Templates, escape
from hinoki.orm import MARK, FieldMeta, Model
from hinoki.runtime.binding import to_float, to_int

TEXTAREA_ABOVE = 200
MAX_TEXT = 100_000
INT_MIN, INT_MAX = -(2**63), 2**63 - 1
SIMPLE_KINDS: dict[Any, str] = {
    bool: "checkbox",
    int: "number",
    float: "number",
    uuid.UUID: "uuid",
    datetime.datetime: "datetime",
}
TEXT_KINDS = {"text", "textarea"}
INPUT_TYPES = {
    "text": "text",
    "number": "text",
    "uuid": "text",
    "datetime": "datetime-local",
}


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
    references: type[Model] | None = None
    shown_only: bool = False  # rendered disabled, never accepted
    select: bool = False  # a foreign key rendered as a select


def unwrap(annotation: Any) -> tuple[Any, bool]:
    """``(type, nullable)`` for ``T | None``."""
    args = typing.get_args(annotation)
    if args and type(None) in args:
        rest = [a for a in args if a is not type(None)]
        return (rest[0] if len(rest) == 1 else annotation), True
    return annotation, False


def kind_of(python_type: Any, max_length: int | None) -> str:
    if python_type is str:
        long = max_length is None or max_length > TEXTAREA_ABOVE
        return "textarea" if long else "text"
    return SIMPLE_KINDS.get(python_type, "readonly")


def fields_for(
    model: type[Model],
    skip: frozenset[str] = frozenset(),
    readonly: frozenset[str] = frozenset(),
    *,
    creating: bool,
) -> list[FormField]:
    """Form fields of ``model``. The primary key is a field only on creation and
    only when it has no default to supply it; ``skip`` fields do not appear and
    ``readonly`` ones are shown but never accepted."""
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
        kind = kind_of(python_type, meta.max_length)
        found.append(
            FormField(
                item.name,
                item.name.replace("_", " ").capitalize(),
                kind,
                not (has_default or nullable),
                nullable,
                has_default,
                meta.max_length,
                python_type,
                meta.references,
                shown_only=item.name in readonly or kind == "readonly",
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
        value = convert(raw)
    except ValueError:
        word = "whole number" if field.python_type is int else "number"
        raise FieldProblem(f"enter a {word}") from None
    if isinstance(value, int) and not INT_MIN <= value <= INT_MAX:
        raise FieldProblem("the number is out of range")
    return value


def to_text(field: FormField, raw: str) -> str:
    if "\x00" in raw:
        raise FieldProblem("remove the NUL character")
    limit = field.max_length or MAX_TEXT
    if len(raw) > limit:
        raise FieldProblem(f"at most {limit} characters")
    return raw


def convert_text(field: FormField, raw: str) -> Any:
    if field.kind == "number":
        return to_number(field, raw)
    if field.kind == "uuid":
        return to_uuid(raw)
    if field.kind == "datetime":
        return to_datetime(raw)
    return to_text(field, raw)


def empty(field: FormField, is_text: bool) -> tuple[bool, Any]:
    """What an empty submission means for this field."""
    if is_text and not field.required:
        return True, None if field.nullable else ""
    if field.nullable:
        return True, None
    if field.has_default:
        return False, None
    raise FieldProblem("this field is required")


def convert(field: FormField, raw: str | None) -> tuple[bool, Any]:
    """``(present, value)``: ``present`` is False when the field is left out so
    the model default applies. Raises ``FieldProblem``."""
    if field.kind == "checkbox":
        return True, raw is not None
    is_text = field.kind in TEXT_KINDS
    text = (raw or "").replace("\r\n", "\n") if is_text else (raw or "").strip()
    if text == "":
        return empty(field, is_text)
    return True, convert_text(field, text)


def input_text(value: Any) -> str:
    """The text a browser input holds for ``value`` (no offsets, no precision loss
    a ``datetime-local`` control cannot represent)."""
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        moment = value.astimezone(datetime.UTC).replace(tzinfo=None)
        return moment.isoformat(timespec="seconds")
    return str(value)


def unchanged(field: FormField, raw: str | None, original: Any) -> bool:
    """The submitted text is exactly what the form showed for the stored value."""
    if field.kind == "checkbox":
        return (raw is not None) == bool(original)
    shown = input_text(original)
    sent = (
        (raw or "").replace("\r\n", "\n")
        if field.kind in TEXT_KINDS
        else (raw or "").strip()
    )
    return sent == shown.replace("\r\n", "\n") or (
        field.kind == "datetime" and (raw or "").strip() == shown
    )


def collect(
    fields: Sequence[FormField],
    form: Mapping[str, str],
    original: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """``(values, errors)`` from submitted text. Read-only fields are ignored and,
    when ``original`` (the stored values) is given, so are unchanged fields."""
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for field in fields:
        if field.shown_only:
            continue
        raw = form.get(field.name)
        if original is not None and unchanged(field, raw, original.get(field.name)):
            continue
        try:
            present, value = convert(field, raw)
        except FieldProblem as exc:
            errors[field.name] = str(exc)
        else:
            if present:
                values[field.name] = value
    return values, errors


def attributes(field: FormField, error: str | None) -> str:
    name = escape(field.name)
    parts = [f'id="f-{name}"', f'name="{name}"']
    if field.required and field.kind != "checkbox" and not field.shown_only:
        parts.append("required")
    if field.max_length is not None:
        parts.append(f'maxlength="{field.max_length}"')
    if field.shown_only:
        parts.append("disabled")
    if error:
        parts.append(f'aria-invalid="true" aria-describedby="e-{name}"')
    return " ".join(parts)


def select_control(
    field: FormField, value: Any, attrs: str, options: Sequence[tuple[str, str]]
) -> Raw:
    chosen = input_text(value)
    rows = []
    if field.nullable or not chosen:
        rows.append('<option value="">—</option>')
    for key, label in options:
        mark = " selected" if key == chosen else ""
        rows.append(f'<option value="{escape(key)}"{mark}>{escape(label)}</option>')
    if chosen and all(key != chosen for key, _ in options):
        rows.append(
            f'<option value="{escape(chosen)}" selected>{escape(chosen)}</option>'
        )
    return Raw(f"<select {attrs}>{''.join(rows)}</select>")


def plain_control(field: FormField, value: Any, attrs: str) -> Raw:
    """Inputs that are not selects."""
    text = escape(input_text(value))
    if field.kind == "readonly":
        return Raw(f'<input {attrs} value="{text}">')
    if field.kind == "checkbox":
        checked = " checked" if value else ""
        return Raw(f'<input type="checkbox" {attrs}{checked}>')
    multiline = isinstance(value, str) and ("\n" in value or "\r" in value)
    if field.kind == "textarea" or multiline:
        # The HTML parser drops one leading newline inside <textarea>: add one so
        # a value that itself starts with a newline round-trips.
        return Raw(f"<textarea {attrs}>\n{text}</textarea>")
    mode = ' inputmode="decimal"' if field.kind == "number" else ""
    step = ' step="1"' if field.kind == "datetime" else ""
    kind = INPUT_TYPES[field.kind]
    return Raw(f'<input type="{kind}"{mode}{step} {attrs} value="{text}">')


def control(
    field: FormField,
    value: Any,
    error: str | None,
    options: Sequence[tuple[str, str]] | None = None,
) -> Raw:
    attrs = attributes(field, error)
    if field.select and options is not None:
        return select_control(field, value, attrs, options)
    return plain_control(field, value, attrs)


def render_field(
    templates: Templates,
    field: FormField,
    value: Any,
    error: str | None,
    options: Sequence[tuple[str, str]] | None = None,
) -> Raw:
    notes = []
    if error:
        notes.append(
            f'<p class="hn-error" id="e-{escape(field.name)}">{escape(error)}</p>'
        )
    if field.shown_only:
        notes.append('<p class="hn-help">Not editable here.</p>')
    return templates.render(
        "field",
        name=field.name,
        label=field.label,
        control=control(field, value, error, options),
        help=Raw("".join(notes)),
    )
