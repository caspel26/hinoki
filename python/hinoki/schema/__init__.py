"""Schemas: typed, immutable input/output models with strict validation."""

from __future__ import annotations

import sys
import types
from typing import Any

from hinoki.fields import UNSET, Field, Unset
from hinoki.schema import compile as _compile
from hinoki.schema import hooks as _hooks
from hinoki.schema import meta as _meta
from hinoki.schema import normalize as _normalize
from hinoki.schema import validate as _validate
from hinoki.schema.base import Schema
from hinoki.schema.compile import (
    direct_construction as direct_construction,
)
from hinoki.schema.compile import (
    free_parameters as free_parameters,
)
from hinoki.schema.compile import (
    from_row as from_row,
)
from hinoki.schema.compile import (
    plain as plain,
)
from hinoki.schema.compile import (
    uses_generated_init as uses_generated_init,
)
from hinoki.schema.compile import (
    walk_model_schema as walk_model_schema,
)
from hinoki.schema.compile import (
    walk_model_value as walk_model_value,
)
from hinoki.schema.construct import (
    allocate_schema_instance as allocate_schema_instance,
)
from hinoki.schema.decorators import (
    _INSTANCE_MISSING as _INSTANCE_MISSING,
)
from hinoki.schema.decorators import (
    _INVALID_INSTANCE_VALUE as _INVALID_INSTANCE_VALUE,
)
from hinoki.schema.decorators import (
    _MODEL_CONSTRUCTOR as _MODEL_CONSTRUCTOR,
)
from hinoki.schema.decorators import (
    _SUPPLIED_FIELDS as _SUPPLIED_FIELDS,
)
from hinoki.schema.decorators import (
    _TRACKS_SUPPLIED_FIELDS as _TRACKS_SUPPLIED_FIELDS,
)
from hinoki.schema.decorators import (
    _TRUSTED_CONSTRUCTOR as _TRUSTED_CONSTRUCTOR,
)
from hinoki.schema.decorators import (
    ForwardReference as ForwardReference,
)
from hinoki.schema.decorators import (
    Invalid,
    before_validate,
    field_check,
    field_normalize,
    object_check,
)
from hinoki.schema.decorators import (
    stop_awaitable as stop_awaitable,
)
from hinoki.schema.encoding import (
    Dumpable as Dumpable,
)
from hinoki.schema.encoding import (
    encode as encode,
)
from hinoki.schema.encoding import (
    encode_items as encode_items,
)
from hinoki.schema.encoding import (
    member_key as member_key,
)
from hinoki.schema.encoding import (
    sorted_items as sorted_items,
)
from hinoki.schema.hooks import (
    chosen_columns as chosen_columns,
)
from hinoki.schema.hooks import (
    graph_has_before as graph_has_before,
)
from hinoki.schema.hooks import (
    graph_has_checks as graph_has_checks,
)
from hinoki.schema.hooks import (
    graph_has_normalizers as graph_has_normalizers,
)
from hinoki.schema.hooks import (
    nested_schemas as nested_schemas,
)
from hinoki.schema.hooks import (
    normalize_selector as normalize_selector,
)
from hinoki.schema.hooks import (
    prepare_value as prepare_value,
)
from hinoki.schema.hooks import (
    run_checks as run_checks,
)
from hinoki.schema.hooks import (
    type_reaches_before as type_reaches_before,
)
from hinoki.schema.hooks import (
    value_check_problems as value_check_problems,
)
from hinoki.schema.meta import (
    SchemaMeta as SchemaMeta,
)
from hinoki.schema.meta import (
    _declared_annotations as _declared_annotations,
)
from hinoki.schema.meta import (
    _is_class_var as _is_class_var,
)
from hinoki.schema.meta import (
    native_replace as native_replace,
)
from hinoki.schema.meta import (
    native_validator as native_validator,
)
from hinoki.schema.meta import (
    text_native as text_native,
)
from hinoki.schema.normalize import (
    _NORMALIZATION_QUEUE as _NORMALIZATION_QUEUE,
)
from hinoki.schema.normalize import (
    prepare_normalized_values as prepare_normalized_values,
)
from hinoki.schema.spec import (
    FieldSpec,
)
from hinoki.schema.spec import (
    annotation_is_str as annotation_is_str,
)
from hinoki.schema.spec import (
    merge_alias as merge_alias,
)
from hinoki.schema.spec import (
    resolve_schema_config as resolve_schema_config,
)
from hinoki.schema.spec import (
    validate_aliases as validate_aliases,
)
from hinoki.schema.spec import (
    validate_normalizer_method as validate_normalizer_method,
)
from hinoki.schema.spec import (
    validate_reserved_names as validate_reserved_names,
)
from hinoki.schema.validate import (
    _MAX_INSTANCE_DEPTH as _MAX_INSTANCE_DEPTH,
)
from hinoki.schema.validate import (
    fixed_instance_value as fixed_instance_value,
)
from hinoki.schema.validate import (
    instance_error as instance_error,
)
from hinoki.schema.validate import (
    instance_value as instance_value,
)
from hinoki.schema.validate import (
    list_instance_value as list_instance_value,
)
from hinoki.schema.validate import (
    member_accepts as member_accepts,
)
from hinoki.schema.validate import (
    nested_instance_value as nested_instance_value,
)
from hinoki.schema.validate import (
    schema_instance_values as schema_instance_values,
)
from hinoki.schema.validate import (
    tagged_union_instance_value as tagged_union_instance_value,
)
from hinoki.schema.validate import (
    type_accepts as type_accepts,
)
from hinoki.schema.validate import (
    typed_entries as typed_entries,
)
from hinoki.schema.validate import (
    typed_instance_value as typed_instance_value,
)
from hinoki.schema.validate import (
    variant_accepts as variant_accepts,
)
from hinoki.schemaconfig import SchemaConfig, SchemaExtra
from hinoki.schemafast import instance_accepts as instance_accepts

__all__ = [
    "UNSET",
    "Field",
    "FieldSpec",
    "Invalid",
    "Schema",
    "SchemaConfig",
    "SchemaExtra",
    "Unset",
    "before_validate",
    "field_check",
    "field_normalize",
    "object_check",
]

# Keep monkeypatch.setattr(hinoki.schema, name, ...) working across submodules.
_FORWARD_ATTRS: dict[str, tuple[types.ModuleType, ...]] = {
    "instance_accepts": (_normalize,),
    "instance_value": (_validate,),
    "native_validator": (_meta,),
    "nested_schemas": (_hooks, _compile),
    "value_check_problems": (_hooks,),
}


class _SchemaModule(types.ModuleType):
    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        for target in _FORWARD_ATTRS.get(name, ()):
            setattr(target, name, value)


sys.modules[__name__].__class__ = _SchemaModule
