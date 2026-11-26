"""The Schema base class."""

from __future__ import annotations

import dataclasses
import inspect
import typing
from collections.abc import Iterable
from typing import Annotated, Any, ClassVar, Self, cast, dataclass_transform

from hinoki import core
from hinoki.computed import ComputedSpec, check_wire_names
from hinoki.core import dump_instance
from hinoki.errors import ErrorDetail, RegistrationError, ValidationError
from hinoki.fields import Field, FieldInfo
from hinoki.fieldtypes import FieldType, parse_annotation
from hinoki.schema.compile import (
    class_source,
    compile_native,
    derive_from_model,
    finish_model_projection,
    free_parameters,
    model_fields,
    model_plan,
    set_model_normalization_paths,
    uses_generated_init,
    with_link_ids,
)
from hinoki.schema.construct import (
    install_custom_init,
    install_generated_init,
    install_native_methods,
)
from hinoki.schema.decorators import (
    _CONFIG_UNSET,
    _SUPPLIED_FIELDS,
    _TRACKS_SUPPLIED_FIELDS,
    ForwardReference,
    _ConfigOmitted,
)
from hinoki.schema.hooks import (
    collect_before,
    collect_checks,
    collect_normalizers,
    graph_has_checks,
    normalize_selector,
    prepare_root,
    refuse_hooked_unions,
    run_checks,
)
from hinoki.schema.meta import (
    _TYPED_JSON,
    _VALIDATE,
    SchemaMeta,
    _SlotsHeld,
    native_replace,
    reset_inherited_entries,
    text_native,
)
from hinoki.schema.normalize import (
    _NORMALIZATION_QUEUE,
    apply_normalization_queue,
    begin_normalization_queue,
    defer_or_normalize,
)
from hinoki.schema.spec import (
    FieldSpec,
    merge_alias,
    register_computed,
    resolve_schema_config,
    validate_aliases,
    validate_normalizer_method,
    validate_reserved_names,
)
from hinoki.schema.validate import schema_instance_values
from hinoki.schemaconfig import SchemaConfig

if typing.TYPE_CHECKING:
    # A custom metaclass makes the type checkers stop validating class keywords
    # (`config=`), so they see the plain `type`; `SchemaMeta` is the runtime
    # metaclass.
    _SchemaMeta = type
else:
    _SchemaMeta = SchemaMeta


@dataclass_transform(
    kw_only_default=True, frozen_default=True, field_specifiers=(Field,)
)
class Schema(metaclass=_SchemaMeta):
    """Base class for schemas.

    ``class PostCreate(Schema): title: str`` becomes a frozen, keyword-only
    dataclass. ``PostCreate.validate(data)`` checks decoded JSON strictly:
    no coercion, unknown keys refused by default, and every problem reported
    with its path.
    """

    __hinoki_schema__: ClassVar[bool] = True
    __slots__ = (_SUPPLIED_FIELDS, "__dict__", "__weakref__")
    __specs__: ClassVar[dict[str, FieldSpec] | None] = None
    __assigned_field_info__: ClassVar[dict[str, FieldInfo]] = {}
    __native__: ClassVar[core.CompiledSchema | None] = None
    __native_dump__: ClassVar[bool] = True
    __checks__: ClassVar[tuple[tuple[str, tuple[str, ...], str], ...]] = ()
    __normalizers__: ClassVar[tuple[tuple[str, str], ...]] = ()
    __before__: ClassVar[tuple[str, ...]] = ()
    __declared_forms__: ClassVar[tuple[tuple[str, FieldInfo], ...] | None] = None
    __float_fields__: ClassVar[tuple[tuple[str, FieldType], ...] | None] = None
    __instance_plan__: ClassVar[tuple[tuple[str, str, FieldType, bool], ...] | None] = (
        None
    )
    __graph_normalizers__: ClassVar[bool | None] = None
    __graph_checks__: ClassVar[bool | None] = None
    __graph_before__: ClassVar[bool | None] = None
    __source__: ClassVar[dict[str, Any]] = {}
    __derivation__: ClassVar[tuple[Any, ...] | None] = None
    __derived_model__: ClassVar[type | None] = None
    __schema_config__: ClassVar[SchemaConfig] = SchemaConfig()
    __computed_specs__: ClassVar[dict[str, ComputedSpec]] = {}
    __computed_checked__: ClassVar[bool] = False

    def __init_subclass__(
        cls,
        config: SchemaConfig | _ConfigOmitted = _CONFIG_UNSET,
        **kwargs: object,
    ) -> None:
        super().__init_subclass__(**kwargs)
        with _SlotsHeld(cls):
            cls._build_schema(config)

    @classmethod
    def _build_schema(cls, config: SchemaConfig | _ConfigOmitted) -> None:
        resolved_config = resolve_schema_config(cls, config)
        custom_init = "__init__" in cls.__dict__
        cls.__source__ = class_source(cls)
        cls.__assigned_field_info__ = {
            name: value
            for name in inspect.get_annotations(cls)
            if isinstance((value := cls.__dict__.get(name)), FieldInfo)
        }
        plan = cls.__dict__.get("__derivation__")
        if plan is not None:
            cls.__derived_model__ = cast(type, plan[0])
            derive_from_model(cls, *plan)
        cls.__checks__ = collect_checks(cls)
        cls.__normalizers__ = collect_normalizers(cls)
        cls.__before__ = collect_before(cls)
        annotations = inspect.get_annotations(cls)
        validate_reserved_names(cls, annotations)
        register_computed(cls, annotations)
        cls.__schema_config__ = resolved_config
        for name in annotations:
            cls.apply_default(name)
        dataclasses.dataclass(frozen=True, kw_only=True)(cls)
        reset_inherited_entries(cls)
        install_native_methods(cls)
        setattr(cls, _TRACKS_SUPPLIED_FIELDS, not custom_init)
        if custom_init:
            install_custom_init(cls, cls.__init__)
        else:
            install_generated_init(cls, cls.__init__)
        try:
            cls.__specs__ = None if cls.is_generic() else cls.build_specs()
        except ForwardReference:
            cls.__specs__ = None  # resolved on first use (recursive schemas)
        else:
            if not cls.is_generic():
                cls.computed_fields()
                refuse_hooked_unions(cls)

    # Frozen fields live in slots: restore them the way the generated
    # constructor writes them.
    __setstate__ = core.NativeMethod("setstate", [])

    @classmethod
    def is_generic(cls) -> bool:
        """True while type parameters are still open (``Envelope``)."""
        return bool(getattr(cls, "__parameters__", ()))

    def __class_getitem__(cls, args: object) -> Any:
        if not cls.is_generic():
            raise TypeError(f"{cls.__name__} is not a generic schema")
        given = args if isinstance(args, tuple) else (args,)
        if any(free_parameters(a) for a in given):
            return super().__class_getitem__(args)  # type: ignore[misc]
        from hinoki.generics import parameterize  # noqa: PLC0415 - generics uses us

        return parameterize(cls, given)

    @classmethod
    def computed_fields(cls) -> dict[str, ComputedSpec]:
        """The read-only values this schema adds to its output, by name."""
        specs = cls.__computed_specs__
        if specs and not cls.__dict__.get("__computed_checked__"):
            check_wire_names(cls, specs, {s.wire_name for s in cls.specs().values()})
            cls.__computed_checked__ = True
        return dict(specs)

    @classmethod
    def apply_default(cls, name: str) -> None:
        """Turn a ``Field(default_factory=...)`` attribute into a dataclass default."""
        attr = cls.__dict__.get(name)
        if isinstance(attr, FieldInfo):
            if attr.constraints():
                raise RegistrationError(
                    f"{cls.__name__}.{name}: constraints in Field(...) belong in "
                    "Annotated[type, Field(...)], not in the default"
                )
            if attr.default_factory is None:
                delattr(cls, name)
            else:
                setattr(
                    cls, name, dataclasses.field(default_factory=attr.default_factory)
                )

    @classmethod
    def specs(cls) -> dict[str, FieldSpec]:
        """Field descriptions, resolving forward references on first use."""
        cached = cls.__dict__.get("__specs__")
        if cached is None:
            if cls.is_generic():
                raise RegistrationError(
                    f"{cls.__name__} is generic: parameterize it first, "
                    f"for example {cls.__name__}[int]"
                )
            cached = cls.build_specs(final=True)
            cls.__specs__ = cached
        return cached

    @classmethod
    def build_specs(cls, final: bool = False) -> dict[str, FieldSpec]:
        try:
            hints = typing.get_type_hints(cls, include_extras=True)
        except NameError as exc:
            if not final:
                raise ForwardReference from exc
            raise RegistrationError(f"{cls.__name__}: {exc}") from exc
        specs: dict[str, FieldSpec] = {}
        assigned_info = cls.__dict__.get("__assigned_field_info__", {})
        derived = model_fields(cls)
        for item in dataclasses.fields(cast(Any, cls)):
            assigned = assigned_info.get(item.name)
            annotation = hints[item.name]
            if isinstance(assigned, FieldInfo):
                annotation = with_assigned_notes(annotation, assigned)
            ftype = parse_annotation(
                annotation,
                f"{cls.__name__}.{item.name}",
                input_bounds=item.name not in derived,
            )
            alias = merge_alias(
                cls.__name__,
                item.name,
                ftype.info.alias,
                assigned.alias if isinstance(assigned, FieldInfo) else None,
            )
            required = (
                item.default is dataclasses.MISSING
                and item.default_factory is dataclasses.MISSING
            )
            specs[item.name] = FieldSpec(item.name, alias, ftype, required)
        validate_aliases(cls, specs)
        cls.validate_normalizers(specs)
        return specs

    @classmethod
    def validate_normalizers(cls, specs: dict[str, FieldSpec]) -> None:
        """Check that registered normalizers have a precise typed target."""
        by_field: dict[str, str] = {}
        for field_name, method_name in cls.__normalizers__:
            spec = specs.get(field_name)
            if spec is None:
                raise RegistrationError(
                    f"{cls.__name__}.{method_name}: unknown field {field_name!r}"
                )
            if spec.ftype.kind != "str":
                raise RegistrationError(
                    f"{cls.__name__}.{field_name}: field normalizers require str fields"
                )
            if field_name in by_field:
                raise RegistrationError(
                    f"{cls.__name__}.{field_name}: only one field normalizer is allowed"
                )
            by_field[field_name] = method_name
            validate_normalizer_method(cls, method_name)
        cls.__normalizers__ = tuple(
            (name, by_field[name]) for name in specs if name in by_field
        )

    @classmethod
    def native_schema(cls) -> core.CompiledSchema:
        """The Rust validator for this schema and everything it refers to,
        compiled on first use and cached on the class."""
        cached = cls.__dict__.get("__native__")
        if cached is None:
            cached = compile_native(cls)
            cls.__native__ = cached
            # Whether the class uses the standard dump (so Rust may serialise its
            # fields directly) or overrides it (then dump() decides).
            cls.__native_dump__ = cls.dump is Schema.dump
        return cached

    if typing.TYPE_CHECKING:

        @classmethod
        def validate_json(cls, raw: bytes) -> Self | None:
            """The instance a JSON body describes, built by Rust in one pass,
            or ``None`` when the ordinary ``decode_json`` and ``validate``
            must run on the same bytes (invalid input, or a schema with hooks
            or checks)."""
            ...

    else:
        validate_json = _TYPED_JSON
        validate = _VALIDATE

    if typing.TYPE_CHECKING:

        @classmethod
        def validate(cls, data: object) -> Self:
            """Build an instance from decoded JSON or raise ``ValidationError``
            (validated in Rust)."""
            ...

    @classmethod
    def _validate_ordinary(cls, data: object) -> Self:
        return cls._validate_prepared(prepare_root(cls, data))

    @classmethod
    def _validate_prepared(cls, data: object) -> Self:
        queue, token, owns_queue = begin_normalization_queue(cls)
        try:
            instance, problems = cls.native_schema().validate(data)
            if problems:
                raise ValidationError([ErrorDetail(*problem) for problem in problems])
            if owns_queue and queue is not None:
                if token is not None:
                    _NORMALIZATION_QUEUE.reset(token)
                    token = None
                apply_normalization_queue(queue)
            if graph_has_checks(cls) and (queue is None or owns_queue):
                found = run_checks(instance, "")
                if found:
                    raise ValidationError(found)
            return cast(Self, instance)
        finally:
            if token is not None:
                _NORMALIZATION_QUEUE.reset(token)

    @classmethod
    def validate_text(cls, data: object) -> Self:
        """Like ``validate`` for data that is all text, such as form fields
        (``{"count": ["2"]}``) or a parsed XML tree: text is first converted, in
        Rust, to the ``int``, ``float`` and ``bool`` the fields declare. Anything
        that does not convert is left for ``validate`` to refuse. Before hooks see
        the raw text (each form value is a list of strings) and run first."""
        if text_native(cls):
            return cast(Self, cls.native_schema().build_text(data))
        prepared = prepare_root(cls, data)
        return cls._validate_prepared(cls.native_schema().coerce_text(prepared))

    @classmethod
    def _validate_instance(
        cls,
        instance: Schema,
        *,
        allow_naive_datetime: bool = False,
        skip_fields: set[str] | None = None,
        use_aliases: bool = False,
        path_prefix: str = "",
    ) -> None:
        """Validate final Python values on a manually constructed instance."""
        values = schema_instance_values(
            cls, instance, use_aliases=use_aliases, path=path_prefix
        )
        _, problems = cls.native_schema().validate(
            values,
            construct=False,
            use_aliases=use_aliases,
            allow_naive_datetime=allow_naive_datetime,
            skip_fields=sorted(skip_fields or ()),
        )
        if problems:
            raise ValidationError(
                [
                    ErrorDetail(
                        f"{path_prefix}.{path}"
                        if path_prefix and path
                        else path_prefix or path,
                        code,
                        message,
                    )
                    for path, code, message in problems
                ]
            )

    @classmethod
    def fields(cls) -> dict[str, FieldSpec]:
        """Immutable description shared by binding, docs and rules."""
        return dict(cls.specs())

    def to_dict(self, *, exclude: Iterable[str] = ()) -> dict[str, Any]:
        """The field values as Python objects (UUID, datetime stay what they are),
        nested schemas converted too. Fields left ``UNSET`` are omitted, so the
        result is exactly "what the client sent" for a partial update."""
        return dump_instance(self, False, list(exclude), False)

    def replace(self, **changes: Any) -> Self:
        """A copy with ``changes`` (Python field names) applied. Every value is
        validated again, normalised and checked like a new instance, so the copy
        is as trustworthy as any other; fields that were never supplied stay
        unsupplied for ``dump(exclude_unset=True)``."""
        found = native_replace(self, changes)
        if found is not None:
            return cast(Self, found)
        specs = self.specs()
        unknown = [name for name in changes if name not in specs]
        if unknown:
            raise TypeError(
                f"{type(self).__name__}.replace() got an unknown field {unknown[0]!r}"
            )
        supplied = getattr(self, _SUPPLIED_FIELDS, None)
        kept = {
            name: getattr(self, name)
            for name in specs
            if supplied is None or name in supplied
        }
        return type(self)(**{**kept, **changes})

    @classmethod
    def from_models(cls, rows: Iterable[object], **extra: Any) -> list[Self]:
        """Build a list from a synchronous iterable of model rows.

        Each row follows ``from_model`` exactly. The iterable is consumed once,
        eagerly; this reads already-loaded attributes and never performs I/O.
        """
        return [cls.from_model(row, **extra) for row in rows]

    @classmethod
    def from_model(cls, row: object, **extra: Any) -> Self:
        """Build this schema from a model instance: every field is read from the
        attribute of the same name, ``extra`` supplies or overrides values (for
        computed fields). Nested models become nested schemas. A field the row
        lacks is an error, and an unloaded relation still raises
        ``RelationNotLoaded``: nothing here does I/O."""
        if extra:
            unknown = set(extra) - set(cls.specs())
            if unknown:
                raise TypeError(f"{cls.__name__} has no fields {sorted(unknown)}")
        queue, token, owns_queue = begin_normalization_queue(cls)
        links = getattr(type(row), "__links__", None)
        if links:
            extra = with_link_ids(cls, row, links, extra)
        try:
            instance = cast(Self, model_plan(cls).build(row, extra))
            if owns_queue and queue is not None:
                set_model_normalization_paths(instance, queue)
            generated = uses_generated_init(cls)
            if generated:
                cls._validate_instance(instance, allow_naive_datetime=True)
                defer_or_normalize(instance, use_aliases=False)
            if token is not None:
                _NORMALIZATION_QUEUE.reset(token)
                token = None
            finish_model_projection(cls, instance, queue, owns_queue, generated)
            return instance
        finally:
            if token is not None:
                _NORMALIZATION_QUEUE.reset(token)

    def dump(
        self,
        *,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] = (),
        exclude_unset: bool = False,
        by_alias: bool = True,
    ) -> dict[str, Any]:
        """Return JSON-compatible data with optional projection controls.

        Selectors use Python field names. ``exclude_unset`` omits defaulted
        fields not supplied at construction. HTTP responses use default options.
        """
        if (
            include is None
            and type(exclude) is tuple
            and not exclude
            and exclude_unset is False
            and by_alias is True
        ):
            return dump_instance(self, True, by_alias=True)
        if type(exclude_unset) is not bool or type(by_alias) is not bool:
            raise TypeError("exclude_unset and by_alias must be bool")
        known: dict[str, object] = (
            {**self.specs(), **self.computed_fields()}
            if include is not None or exclude
            else {}
        )
        selected_include = normalize_selector(include, "include", known)
        selected_exclude = normalize_selector(exclude, "exclude", known) or []
        if (
            selected_include is None
            and not selected_exclude
            and not exclude_unset
            and by_alias
        ):
            return dump_instance(self, True, by_alias=True)
        return core.serialize_instance(
            self,
            selected_include,
            selected_exclude,
            exclude_unset,
            by_alias,
        )


def with_assigned_notes(annotation: object, assigned: FieldInfo) -> object:
    """The annotation with what a ``name: T = Field(...)`` default declares
    besides its alias: the discriminator and the documentation notes. The
    alias is merged separately, so a conflicting one is reported by name."""
    if assigned.discriminator is None and not assigned.notes():
        return annotation
    declared = FieldInfo(
        discriminator=assigned.discriminator,
        description=assigned.description,
        examples=assigned.examples,
        deprecated=assigned.deprecated,
    )
    return Annotated[annotation, declared]
