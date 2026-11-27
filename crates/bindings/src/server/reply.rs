use std::path::PathBuf;

use kiso::headers::Header;
use kiso::wsrun::Accepted;
use kiso::{Answer, FastAnswer, Streamed};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList};

pub type Reply = Answer;

/// What a request is finally answered with: the whole answer, or a head whose
/// body follows from a `ResponseSink`, or a file the connection opens itself.
pub(crate) enum Sent {
    Whole(Reply),
    Fast(Box<FastAnswer>),
    Streamed(Streamed),
    File(FileReply),
    /// The handshake was accepted: the connection switches to WebSocket.
    Upgrade(Box<Accepted>),
}

/// A file answer before the file is opened. The head carries no
/// `content-length`: the connection task sets it from the opened file.
pub(crate) struct FileReply {
    pub(crate) status: u16,
    pub(crate) headers: Vec<Header>,
    pub(crate) path: PathBuf,
}

pub(super) fn reply_to_py<'py>(py: Python<'py>, sent: &Sent) -> PyResult<Bound<'py, PyAny>> {
    match sent {
        Sent::Whole(reply) => answer_to_py(py, reply.status, &reply.headers, &reply.body),
        Sent::Fast(fast) => {
            let headers = PyList::empty(py);
            for (name, value) in &fast.headers {
                headers.append((
                    PyBytes::new(py, name.as_str().as_bytes()),
                    PyBytes::new(py, value.as_bytes()),
                ))?;
            }
            Ok((fast.status, headers, PyBytes::new(py, &fast.body))
                .into_pyobject(py)?
                .into_any())
        }
        Sent::Streamed(stream) => answer_to_py(py, stream.status, &stream.headers, &[]),
        Sent::File(file) => answer_to_py(py, file.status, &file.headers, &[]),
        Sent::Upgrade(accepted) => answer_to_py(py, 101, &accepted.headers, &[]),
    }
}

pub(super) fn answer_to_py<'py>(
    py: Python<'py>,
    status: u16,
    head: &[Header],
    body: &[u8],
) -> PyResult<Bound<'py, PyAny>> {
    let headers = PyList::empty(py);
    for (name, value) in head {
        headers.append((PyBytes::new(py, name), PyBytes::new(py, value)))?;
    }
    Ok((status, headers, PyBytes::new(py, body))
        .into_pyobject(py)?
        .into_any())
}
