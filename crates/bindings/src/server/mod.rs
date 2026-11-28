//! Glue between `kiso` and Python: the request classifier (`Core`, held
//! by the Python `ServerCore`), the request handed to the pipeline
//! (`PreparedRequest`) and the handle a test holds to see the answer
//! (`Submission`).
//!
//! Python objects exist only on the calling thread: `PreparedRequest` holds Rust
//! data and a oneshot sender, so it can be dropped on any thread without the GIL.
//! `Core` is shared with the tokio side behind an `Arc` and holds no Python
//! object; `kiso.rs` drives `Core::classify` from the connection layer, and
//! the tests call `submit` the same way without a socket.

mod core;
mod prepared;
mod reply;
mod routes;

use pyo3::prelude::*;

pub use self::core::{Core, ServerCore};
pub use self::prepared::{BodyReadError, PreparedRequest, Submission};
#[allow(unused_imports)]
pub(crate) use self::prepared::{NativeBody, NativeBodyError, NativeFailure, NativeRows};
#[allow(unused_imports)]
pub use self::reply::Reply;
pub(crate) use self::reply::{FileReply, Sent};
pub(crate) use self::routes::Outcome;
pub use self::routes::{Asked, Head, RequestHeaders, RequestMethod, RequestUri};

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<ServerCore>()?;
    m.add_class::<PreparedRequest>()?;
    m.add_class::<Submission>()?;
    m.add("BodyReadError", m.py().get_type::<BodyReadError>())?;
    Ok(())
}
