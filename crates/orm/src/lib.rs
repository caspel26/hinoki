//! Hinoki database engine (pure Rust). The Python bridge lives in `hinoki-bindings`.

mod database;
mod error;
pub mod links;
mod mysql;
mod numeric;
pub mod query;
mod sqlite;
mod tls;
mod value;

pub use database::{Database, PageQueries, Row, RowSet, Transaction};
pub use error::{DbError, ErrorKind};
pub use value::{
    parse_timestamp, timestamp_iso, timestamp_storage_text, write_timestamp_iso, Value,
};
