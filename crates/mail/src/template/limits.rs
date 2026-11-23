//! Fixed limits of the template engine. They are constants, not settings.

pub const MAX_SUBJECT_SOURCE_BYTES: usize = 1024;
pub const MAX_BODY_SOURCE_BYTES: usize = 256 * 1024;
/// Checked by the caller before it reads a template file.
pub const MAX_FILE_BYTES: usize = 512 * 1024;
pub const MAX_NODES: usize = 10_000;
pub const MAX_DEPTH: usize = 16;
pub const MAX_PATH_SEGMENTS: usize = 8;
pub const MAX_IDENT_BYTES: usize = 64;
pub const MAX_SUBJECT_BYTES: usize = 998;
pub const MAX_OUTPUT_BYTES: usize = 1024 * 1024;
pub const MAX_ITERATIONS: usize = 10_000;
pub const MAX_CONTEXT_DEPTH: usize = 16;
pub const MAX_CONTEXT_NODES: usize = 100_000;
pub const MAX_CONTEXT_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_PROBLEMS: usize = 20;
pub const MAX_NAME_BYTES: usize = 64;
/// Keys of a map above which a sorted index is built.
pub const LINEAR_KEYS: usize = 16;
