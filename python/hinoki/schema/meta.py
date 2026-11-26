"""SchemaMeta metaclass, slot helpers, and lazy native validation descriptors."""

from __future__ import annotations

import inspect
import types
import typing
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hinoki import core
from hinoki.errors import RegistrationError
from hinoki.schema.compile import graph_is_stock
from hinoki.schema.decorators import _CONFIG_UNSET, _ConfigOmitted
from hinoki.schema.hooks import (
    graph_has_before,
    graph_has_checks,
    graph_has_normalizers,
)
from hinoki.schemaconfig import SchemaConfig

if TYPE_CHECKING:
    from hinoki.schema.base import Schema

_SLOT_STASH = "__hinoki_slot_stash__"


def _is_class_var(annotation: object) -> bool:
    if isinstance(annotation, typing.ForwardRef):
        annotation = annotation.__forward_arg__
    if isinstance(annotation, str):
        head = annotation.replace(" ", "").split("[", 1)[0]
        return head.rsplit(".", 1)[-1] == "ClassVar"
    return annotation is ClassVar or typing.get_origin(annotation) is ClassVar


def _declared_annotations(namespace: dict[str, Any]) -> dict[str, Any]:
    declared = namespace.get("__annotations__")
    if declared is not None:
        return cast(dict[str, Any], declared)
    annotate = namespace.get("__annotate_func__") or namespace.get("__annotate__")
    if annotate is None:
        return {}
    import annotationlib  # type: ignore[import-not-found,unused-ignore]  # noqa: PLC0415 - Python 3.14 only, the lazy annotations

    return cast(
        dict[str, Any],
        annotationlib.call_annotate_function(annotate, annotationlib.Format.FORWARDREF),
    )


def _slot_names(namespace: dict[str, Any], bases: tuple[type, ...]) -> list[str]:
    """The fields a new schema class declares that can live in a slot: plain
    names no base class already defines."""
    return [
        name
        for name, annotation in _declared_annotations(namespace).items()
        if not name.startswith("_")
        and not _is_class_var(annotation)
        and not any(hasattr(base, name) for base in bases)
    ]


class SchemaMeta(type):
    """Gives each schema class a slot per declared field.

    Rust reads a JSON body straight into those slots. The class body is
    unchanged for the user: a default is put back in the class while the
    schema is being built, then the slot takes its place, as for a
    ``dataclass(slots=True)`` class (which cannot be used, since it creates a
    second class)."""

    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        slots: bool = True,
        config: SchemaConfig | _ConfigOmitted = _CONFIG_UNSET,
    ) -> SchemaMeta:
        if (
            slots
            and "__slots__" not in namespace
            and any(isinstance(base, SchemaMeta) for base in bases)
        ):
            names = _slot_names(namespace, bases)
            if names:
                namespace = dict(namespace)
                namespace[_SLOT_STASH] = {
                    field: namespace.pop(field) for field in names if field in namespace
                }
                namespace["__slots__"] = tuple(names)
        options: dict[str, object] = {}
        if config is not _CONFIG_UNSET:
            options["config"] = config
        try:
            return super().__new__(mcs, name, bases, namespace, **options)
        except TypeError as exc:
            if "instance lay-out conflict" not in str(exc):
                raise
            raise RegistrationError(
                f"{name}: more than one base schema declares fields; declare "
                "all but one with `class Base(Schema, slots=False)`"
            ) from exc

    def __init__(
        cls,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        slots: bool = True,  # noqa: ARG002 - read by `__new__`
        config: SchemaConfig | _ConfigOmitted = _CONFIG_UNSET,  # noqa: ARG002
    ) -> None:
        super().__init__(name, bases, namespace)


class _SlotsHeld:
    """Takes the slot members out of a class being built and puts them back."""

    def __init__(self, cls: type) -> None:
        self.cls = cls
        self.members: dict[str, Any] = {}

    def __enter__(self) -> None:
        cls = self.cls
        stash = cls.__dict__.get(_SLOT_STASH)
        if stash is None:
            return
        for name in cls.__dict__["__slots__"]:
            self.members[name] = cls.__dict__[name]
            delattr(cls, name)
            if name in stash:
                setattr(cls, name, stash[name])
        delattr(cls, _SLOT_STASH)

    def __exit__(self, *_: object) -> None:
        for name, member in self.members.items():
            type.__setattr__(self.cls, name, member)


def _no_typed_json(_raw: bytes) -> None:
    return None


class _TypedJson:
    """``Schema.validate_json``: on first use for a class, the class gets the
    Rust decoder itself as its attribute (a builtin method, so a call costs no
    Python frame), or a function that always steps aside when a hook or check
    has to run on the ordinary path."""

    def __get__(
        self, instance: object, owner: type[Schema]
    ) -> Callable[[bytes], object | None]:
        try:
            decode = typed_decoder(owner) or _no_typed_json
        except Exception:
            return _no_typed_json
        if owner.__dict__.get("validate_json", self) is self:
            type.__setattr__(owner, "validate_json", core.Pinned(owner, decode, self))
        return decode


class _Validate:
    """``Schema.validate``: on first use for a class with no before hook, check
    or normalizer on its graph, the class gets the Rust validator itself as its
    attribute (a builtin method: the instance is built and every problem raised
    without a Python frame). Any other class keeps the ordinary method."""

    def __get__(self, instance: object, owner: type[Schema]) -> Callable[[object], Any]:
        from hinoki.schema.base import (  # noqa: PLC0415 - circular dependency with base
            Schema,
        )

        method = types.MethodType(Schema._validate_ordinary.__func__, owner)  # type: ignore[attr-defined]
        try:
            native = native_validator(owner)
        except Exception:
            return method
        found = method if native is None else native
        if owner.__dict__.get("validate", self) is self:
            type.__setattr__(owner, "validate", core.Pinned(owner, found, self))
        return found


def typed_decoder(cls: type[Schema]) -> Callable[[bytes], object | None] | None:
    """The one-pass Rust body decoder of ``cls``, or ``None`` when a before
    hook, a check or a custom validation hook on its graph has to run on the
    ordinary path."""
    if (
        graph_has_before(cls)
        or graph_has_checks(cls)
        or not graph_is_stock(cls)
        or not isinstance(
            inspect.getattr_static(cls, "validate"), core.Pinned | _Validate
        )
    ):
        return None
    return cls.native_schema().decode_typed


def native_validator(cls: type[Schema]) -> Callable[[object], Any] | None:
    """The Rust ``build`` of ``cls``, when nothing has to run around it."""
    if typed_decoder(cls) is None or graph_has_normalizers(cls):
        return None
    return cls.native_schema().build


def text_native(cls: type[Schema]) -> bool:
    """Whether ``validate_text`` can go straight to Rust for ``cls``: the same
    condition as the native ``validate``. Decided once per class."""
    if "__text_native__" not in cls.__dict__:
        try:
            ready = native_validator(cls) is not None
        except Exception:
            return False
        type.__setattr__(cls, "__text_native__", ready)
    return bool(cls.__dict__["__text_native__"])


def native_replace(instance: Schema, changes: dict[str, Any]) -> Schema | None:
    """The copy Rust builds for ``replace``, or ``None`` to use the Python path."""
    try:
        found = type(instance).native_schema().replace(instance, changes)
    except AttributeError:
        return None
    return cast("Schema | None", found)


def reset_inherited_entries(cls: type[Schema]) -> None:
    """Give ``cls`` its own lazy entries, so that it never inherits the entry a
    parent installs for itself later (a user's own ``validate`` is left alone)."""
    for name, fresh in (("validate_json", _TYPED_JSON), ("validate", _VALIDATE)):
        if name not in cls.__dict__ and isinstance(
            inspect.getattr_static(cls, name), core.Pinned | _TypedJson | _Validate
        ):
            type.__setattr__(cls, name, fresh)


_TYPED_JSON = _TypedJson()
_VALIDATE = _Validate()
