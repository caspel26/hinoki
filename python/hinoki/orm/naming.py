"""Deterministic defaults and physical-table collision rules for Models."""

from __future__ import annotations

import re
import unicodedata
from typing import ClassVar, Protocol

from hinoki.errors import RegistrationError

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
MIGRATION_LEDGER = "hinoki_migrations"


class _TableOwner(Protocol):
    __table__: ClassVar[str]
    __table_is_implicit__: ClassVar[bool]


def _words(name: str) -> list[str]:
    words: list[str] = []
    current = ""
    for index, character in enumerate(name):
        if character == "_":
            if current:
                words.append(current)
                current = ""
            continue
        if current and _word_boundary(name, index):
            words.append(current)
            current = ""
        current += character
    if current:
        words.append(current)
    return words


def _word_boundary(name: str, index: int) -> bool:
    previous = name[index - 1] if index else ""
    character = name[index]
    following = name[index + 1] if index + 1 < len(name) else ""
    lower_or_digit_to_upper = (
        previous.islower() or previous.isdigit()
    ) and character.isupper()
    acronym_to_word = previous.isupper() and character.isupper() and following.islower()
    return lower_or_digit_to_upper or acronym_to_word


def _plural_word(word: str, *, acronym: bool = False) -> str:
    if acronym and word.isascii() and word.isalpha() and word.isupper():
        return f"{word}s"
    if word.isascii() and word.isalpha():
        lower = word.lower()
        if len(lower) > 1 and lower.endswith("y") and lower[-2] not in "aeiou":
            return f"{word[:-1]}ies"
        if lower.endswith(("s", "x", "z", "ch", "sh")):
            return f"{word}es"
    return f"{word}s"


def _human_singular(words: list[str]) -> str:
    rendered = [word if word.isupper() else word.lower() for word in words]
    if rendered and not words[0].isupper():
        rendered[0] = rendered[0][:1].upper() + rendered[0][1:]
    return " ".join(rendered)


def _human_plural(label: str) -> str:
    head, separator, tail = label.rpartition(" ")
    word = tail if separator else label
    return f"{head}{separator}{_plural_word(word, acronym=word.isupper())}"


def _validate_options(class_name: str, table: str, label: str, plural: str) -> None:
    for option, value in (("table", table), ("label", label), ("plural", plural)):
        if type(value) is not str:
            raise RegistrationError(f"{class_name}: {option}= must be a string")


def _resolve_display_names(
    class_name: str, words: list[str], label: str, plural: str
) -> tuple[str, str]:
    if not words and (not label or not plural):
        raise RegistrationError(
            f"{class_name}: cannot derive names from this class name; pass "
            "label= and plural= explicitly, and name the table with "
            "@db.table(...) or table="
        )
    singular = label or _human_singular(words)
    plural_name = plural or _human_plural(singular)
    for option, value in (("label", singular), ("plural", plural_name)):
        invalid = not value.strip() or any(
            unicodedata.category(character) == "Cc" for character in value
        )
        if invalid:
            raise RegistrationError(
                f"{class_name}: {option}= must be non-empty and contain "
                "no control characters"
            )
    return singular, plural_name


_APP_LABEL = re.compile(r"[a-z][a-z0-9_]*\Z")


def app_label(module: str) -> str:
    """The app a model module belongs to: the package that holds its ``models``
    module (``shop.apps.billing.models`` is ``billing``, ``shop.models.invoice``
    is ``shop``); without a ``models`` segment, the module's own last name."""
    parts = [part for part in module.split(".") if part]
    if not parts:
        return ""
    position = next(
        (i for i, part in enumerate(parts) if part.lower() == "models"), None
    )
    chosen = parts[position - 1] if position else parts[-1]
    return re.sub(r"\W", "_", chosen).strip("_").lower()


def _derived_table(class_name: str, words: list[str], app: str) -> tuple[str, str]:
    """The table name derived from the app and the plural class name
    (``<app>_<models>``) and, when that name cannot be used, the reason. The
    reason is reported only if no ``@db.table`` or ``table=`` replaces the name
    (see ``Model.check_table``)."""
    if not words or not app:
        return "", (
            f"{class_name}: cannot derive a table name from this class name; "
            "name the table with @db.table(...) or table="
        )
    table_words = [word.lower() for word in words]
    table_words[-1] = _plural_word(table_words[-1])
    table = "_".join([app, *table_words])
    return table, _derived_table_problem(class_name, table)


def _derived_table_problem(class_name: str, table: str) -> str:
    if not _IDENTIFIER.fullmatch(table):
        reason = "is not a portable SQL identifier"
    elif table.casefold() == MIGRATION_LEDGER:
        reason = "is owned by Hinoki migrations"
    elif table.casefold().startswith("sqlite_"):
        reason = "uses SQLite's reserved sqlite_ prefix"
    else:
        return ""
    return (
        f"{class_name}: derived table {table!r} {reason}; name the table "
        "with @db.table(...) or table="
    )


def resolve_model_names(
    class_name: str,
    table: str,
    label: str,
    plural: str,
    *,
    abstract: bool,
    module: str = "",
    app: str = "",
) -> tuple[str, str, str, bool, str]:
    """Return resolved names, whether the table came from the class name and,
    for a derived table name that cannot be used, the deferred error. The
    table is ``<app>_<models>`` (the model name in the plural); ``app`` overrides
    the app taken from ``module``."""
    _validate_options(class_name, table, label, plural)
    if app and not _APP_LABEL.fullmatch(app):
        raise RegistrationError(
            f"{class_name}: app={app!r} must be lowercase letters, digits and "
            "underscores, starting with a letter"
        )
    if abstract and table:
        raise RegistrationError(
            f"{class_name}: an abstract model has no table; remove table=... "
            "or abstract=True"
        )
    words = _words(class_name)
    singular, plural_name = _resolve_display_names(class_name, words, label, plural)
    if abstract:
        return "", singular, plural_name, False, ""
    if table:
        return table, singular, plural_name, False, ""
    derived, problem = _derived_table(class_name, words, app or app_label(module))
    return derived, singular, plural_name, True, problem


def table_names_conflict(left: type[_TableOwner], right: type[_TableOwner]) -> bool:
    """Whether portable table ownership makes two model classes ambiguous."""
    left_table = left.__table__
    right_table = right.__table__
    if left_table == right_table:
        return True
    return left_table.casefold() == right_table.casefold() and (
        left.__table_is_implicit__ or right.__table_is_implicit__
    )
