"""Instance allocation, native protocol methods, and init wrappers."""

from __future__ import annotations

import dataclasses
import functools
import inspect
from typing import TYPE_CHECKING, Any, cast

from hinoki import core
from hinoki.schema.compile import direct_construction, uses_generated_init
from hinoki.schema.decorators import (
    _MODEL_CONSTRUCTOR,
    _SUPPLIED_FIELDS,
    _TRACKS_SUPPLIED_FIELDS,
    _TRUSTED_CONSTRUCTOR,
)
from hinoki.schema.meta import typed_decoder
from hinoki.schema.normalize import defer_or_normalize, validate_constructed

if TYPE_CHECKING:
    from hinoki.schema.base import Schema


def allocate_schema_instance(cls: type[Schema], values: dict[str, Any]) -> Schema:
    """Call a custom ``__new__`` as normal class construction would."""
    allocator = cls.__new__
    if allocator is object.__new__:
        return object.__new__(cls)
    return allocator(cls, **values)


def native_state_names(cls: type) -> list[str]:
    """Every slot an instance of ``cls`` can hold, base classes first."""
    names: list[str] = []
    for klass in reversed(cls.__mro__):
        declared = klass.__dict__.get("__slots__", ())
        for name in (declared,) if isinstance(declared, str) else declared:
            if name not in ("__dict__", "__weakref__") and name not in names:
                names.append(name)
    return names


def defined_by_user(cls: type, name: str) -> bool:
    """True when ``name`` comes from a class body, not from ``object`` or from
    the native method this module installs."""
    found = inspect.getattr_static(cls, name, None)
    if found is None or isinstance(found, core.NativeMethod):
        return False
    return getattr(found, "__objclass__", None) is not object


_PROTOCOL_METHODS = (
    ("__copy__", "copy"),
    ("__deepcopy__", "deepcopy"),
    ("__reduce__", "reduce"),
)


def install_native_methods(cls: type[Schema]) -> None:
    """``copy``, ``deepcopy`` and pickling run in Rust over the slots, except
    for what the class (or a base) defines itself."""
    state = native_state_names(cls)
    if any(
        defined_by_user(cls, name)
        for name in ("__reduce__", "__reduce_ex__", "__getstate__", "__setstate__")
    ):
        # Entries a base installed were built from its slots, not these.
        for name in ("__copy__", "__deepcopy__"):
            if isinstance(inspect.getattr_static(cls, name, None), core.NativeMethod):
                setattr(cls, name, None)
        if isinstance(inspect.getattr_static(cls, "__reduce__"), core.NativeMethod):
            cls.__reduce__ = object.__reduce__  # type: ignore[method-assign]
        return
    for name, kind in _PROTOCOL_METHODS:
        if not defined_by_user(cls, name):
            setattr(cls, name, core.NativeMethod(kind, state))


def install_generated_init(cls: type[Schema], generated_init: Any) -> None:
    """Install direct, wire-validated and model-conversion constructors."""

    def construct_validated(
        schema_cls: type[Schema], values: dict[str, Any], path_prefix: str = ""
    ) -> Schema:
        instance = allocate_schema_instance(schema_cls, values)
        if type(instance).__dict__.get(_TRACKS_SUPPLIED_FIELDS, False):
            object.__setattr__(instance, _SUPPLIED_FIELDS, frozenset(values))
        generated_init(instance, **values)
        if hasattr(schema_cls, "__post_init__"):
            schema_cls._validate_instance(
                instance, use_aliases=True, path_prefix=path_prefix
            )
        else:
            schema_cls._validate_instance(
                instance,
                skip_fields=set(values),
                use_aliases=True,
                path_prefix=path_prefix,
            )
        defer_or_normalize(instance, use_aliases=True, path_prefix=path_prefix)
        return instance

    def construct_from_model(
        schema_cls: type[Schema], values: dict[str, Any]
    ) -> Schema:
        instance = allocate_schema_instance(schema_cls, values)
        if type(instance).__dict__.get(_TRACKS_SUPPLIED_FIELDS, False):
            object.__setattr__(instance, _SUPPLIED_FIELDS, frozenset(values))
        generated_init(instance, **values)
        if not uses_generated_init(schema_cls):
            validate_constructed(instance, from_model=True)
        return instance

    @functools.wraps(generated_init)
    def tracked_init(self: Schema, **values: Any) -> None:
        if type(self) is cls and type(self).__dict__.get(
            _TRACKS_SUPPLIED_FIELDS, False
        ):
            object.__setattr__(self, _SUPPLIED_FIELDS, frozenset(values))
        generated_init(self, **values)
        if type(self) is cls:
            validate_constructed(self)

    setattr(cls, _TRUSTED_CONSTRUCTOR, classmethod(construct_validated))
    setattr(cls, _MODEL_CONSTRUCTOR, classmethod(construct_from_model))
    cls.__init__ = cast(  # type: ignore[method-assign]
        Any, core.NativeInit(tracked_init, lambda: native_init_parts(cls))
    )


def native_init_parts(cls: type[Schema]) -> core.CompiledSchema | None:
    """The compiled schema the native constructor of ``cls`` runs on, or
    ``None`` when the class keeps the Python constructor (a hook, a check, a
    normalizer, a custom initialisation, a field outside the native subset).
    Raises while the class cannot be resolved yet, so that the next
    construction asks again."""
    if (
        cls.is_generic()
        or not direct_construction(cls)
        or typed_decoder(cls) is None
        or not all(field.init for field in dataclasses.fields(cast(Any, cls)))
    ):
        return None
    return cls.native_schema()


def install_custom_init(cls: type[Schema], original_init: Any) -> None:
    """Install validated construction around an application-defined init."""

    def construct_validated(
        schema_cls: type[Schema], values: dict[str, Any], path_prefix: str = ""
    ) -> Schema:
        instance = allocate_schema_instance(schema_cls, values)
        original_init(instance, **values)
        schema_cls._validate_instance(
            instance, use_aliases=True, path_prefix=path_prefix
        )
        defer_or_normalize(instance, use_aliases=True, path_prefix=path_prefix)
        return instance

    def construct_from_model(
        schema_cls: type[Schema], values: dict[str, Any]
    ) -> Schema:
        instance = allocate_schema_instance(schema_cls, values)
        original_init(instance, **values)
        validate_constructed(instance, from_model=True)
        return instance

    @functools.wraps(original_init)
    def validated_init(self: Schema, *args: Any, **values: Any) -> None:
        original_init(self, *args, **values)
        if type(self) is cls:
            validate_constructed(self)

    setattr(cls, _TRUSTED_CONSTRUCTOR, classmethod(construct_validated))
    setattr(cls, _MODEL_CONSTRUCTOR, classmethod(construct_from_model))
    cls.__init__ = cast(Any, validated_init)  # type: ignore[method-assign]
