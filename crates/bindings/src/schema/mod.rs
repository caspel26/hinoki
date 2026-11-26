//! Schema validation and instance construction.
//!
//! Python compiles a schema's closed type model into a table of definitions
//! (see `hinoki.schema`); `CompiledSchema.validate` then walks the decoded JSON
//! once and either returns the finished instance or the ordered list of
//! `(path, code, message)` problems. Semantics, codes and messages follow the
//! pure-Python engine that is kept in `tests/oracle` and compared against this
//! one: strict (no coercion), unknown keys refused by default, at most 50
//! problems.
//!
//! The walk is written once, over a [`View`] of the input: [`PyView`] reads
//! the Python objects `decode_json` built (and builds the Python result);
//! [`JsonView`] reads the GIL-free [`Json`] tree and builds [`Validated`]
//! values. Codes, messages, the error cap, the depth limit and path joining
//! are the same code for both. The GIL-free side uses a lowered copy of the
//! schema ([`NativeSchema`]) that holds no Python object; a schema whose
//! constraints cannot be lowered exactly has none.

mod coerce;
mod compile;
mod internet;
mod model;
mod modelplan;
mod narrow;
mod scalars;
mod serialize;
#[cfg(test)]
mod tests;
mod typeddecode;
mod validate;
mod views;

pub use compile::{check_pattern, compile_schemas};
pub(crate) use compile::{class_fields, json_field_plan, json_field_plan_of};
pub use model::Compiled;
#[allow(unused_imports)]
pub(crate) use model::{
    Defer, Direct, FieldGuides, JsonFieldPlan, NativeLimit, NativeRules, NativeSchema, Number,
    Options, Problem, PyRules, Validated,
};
use model::{Definition, Kind, Node, MAX_DEPTH};
#[allow(unused_imports)]
pub use model::{SchemaInstanceFields, SchemaWireNames};
pub use modelplan::ModelPlan;
pub(crate) use narrow::{narrow, narrowed_parts};
pub use narrow::{Narrowed, ReturnGuide};
pub use scalars::wire_text_of;
pub(crate) use scalars::{parse_date, wire_text, DECIMAL_DIGITS_CEILING, SECRET_MASK};
pub use serialize::{dump_as, dump_for_storage, dump_instance, serialize_instance};
pub use typeddecode::{NativeInit, NativeMethod, Pinned};
use validate::is_composite;
use views::{compare_numbers, holds};
