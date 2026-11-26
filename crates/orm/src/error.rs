//! Database errors. Messages never contain SQL text, credentials or row values.

use std::fmt;

use tokio_postgres::error::SqlState;
use tokio_rusqlite::rusqlite;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    Connect,
    Pool,
    Unique,
    ForeignKey,
    NotNull,
    Check,
    Type,
    Other,
}

#[derive(Debug, Clone)]
pub struct DbError {
    pub kind: ErrorKind,
    pub message: String,
    pub constraint: Option<String>,
    pub table: Option<String>,
    pub sqlstate: Option<String>,
}

impl DbError {
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            constraint: None,
            table: None,
            sqlstate: None,
        }
    }

    /// Client-side failures. Parameter encoding errors keep their message: it
    /// names types, never values.
    fn from_client(err: &tokio_postgres::Error) -> Self {
        let source = std::error::Error::source(err).map(ToString::to_string);
        let text = match source {
            Some(inner) => format!("{err}: {inner}"),
            None => err.to_string(),
        };
        if text.starts_with("error serializing parameter")
            || text.contains("invalid number of parameters")
        {
            return Self::new(ErrorKind::Type, text);
        }
        Self::new(ErrorKind::Other, "database request failed")
    }

    pub(crate) fn from_driver(err: &tokio_postgres::Error) -> Self {
        let Some(db) = err.as_db_error() else {
            return Self::from_client(err);
        };
        let code = db.code();
        let (kind, message) = if *code == SqlState::UNIQUE_VIOLATION {
            (ErrorKind::Unique, "unique constraint violated")
        } else if *code == SqlState::FOREIGN_KEY_VIOLATION {
            (ErrorKind::ForeignKey, "foreign key constraint violated")
        } else if *code == SqlState::NOT_NULL_VIOLATION {
            (ErrorKind::NotNull, "not-null constraint violated")
        } else if *code == SqlState::CHECK_VIOLATION {
            (ErrorKind::Check, "check constraint violated")
        } else {
            (ErrorKind::Other, "database error")
        };
        Self {
            kind,
            message: message.to_owned(),
            constraint: db.constraint().map(str::to_owned),
            table: db.table().map(str::to_owned),
            sqlstate: Some(code.code().to_owned()),
        }
    }

    pub(crate) fn from_sqlite(err: rusqlite::Error) -> Self {
        use rusqlite::Error::SqliteFailure;
        let extended_code = match &err {
            SqliteFailure(code, _) => code.extended_code,
            _ => {
                let kind = if matches!(err, rusqlite::Error::ToSqlConversionFailure(_)) {
                    ErrorKind::Type
                } else {
                    ErrorKind::Other
                };
                return Self::new(kind, "database request failed");
            }
        };
        let kind = match extended_code {
            rusqlite::ffi::SQLITE_CONSTRAINT_PRIMARYKEY
            | rusqlite::ffi::SQLITE_CONSTRAINT_UNIQUE => ErrorKind::Unique,
            rusqlite::ffi::SQLITE_CONSTRAINT_FOREIGNKEY => ErrorKind::ForeignKey,
            rusqlite::ffi::SQLITE_CONSTRAINT_NOTNULL => ErrorKind::NotNull,
            rusqlite::ffi::SQLITE_CONSTRAINT_CHECK => ErrorKind::Check,
            _ => ErrorKind::Other,
        };
        Self {
            kind,
            message: match kind {
                ErrorKind::Unique => "unique constraint violated",
                ErrorKind::ForeignKey => "foreign key constraint violated",
                ErrorKind::NotNull => "not-null constraint violated",
                ErrorKind::Check => "check constraint violated",
                _ => "database request failed",
            }
            .to_owned(),
            constraint: None,
            table: None,
            sqlstate: Some(format!("SQLITE_{extended_code}")),
        }
    }
}

impl fmt::Display for DbError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for DbError {}

pub(crate) fn lease_released() -> DbError {
    DbError::new(
        ErrorKind::Other,
        "database connection lease already released",
    )
}
