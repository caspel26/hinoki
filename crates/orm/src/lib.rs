//! Hinoki database engine (pure Rust). The Python bridge lives in `hinoki-bindings`.

#![forbid(unsafe_code)]

mod database;
mod error;
pub mod links;
mod mysql;
mod numeric;
mod pgrows;
pub mod query;
mod sqlite;
mod tls;
mod value;

pub use database::{Database, PageQueries, PgSession, Row, RowSet, Transaction};
pub use error::{DbError, ErrorKind};
pub use pgrows::{PgCell, PgRowRef, PgRows, RawRows};
pub use value::{
    parse_timestamp, timestamp_iso, timestamp_storage_text, write_timestamp_iso, Value,
};
