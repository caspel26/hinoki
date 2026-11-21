//! Server-sent events framing (WHATWG HTML, "Server-sent events").
//!
//! [`frame`] turns one event into the bytes of the wire format and refuses a
//! field that would let its value break out of its line: `event` and `id` may
//! not hold a CR, LF or NUL. `data` and comments may hold line breaks (`\n`,
//! `\r\n` or `\r`); each line becomes its own field line, so a payload can never
//! forge another field or end the event early.

use std::fmt;

use bytes::Bytes;

/// What a keep-alive frame sends: a comment, which clients ignore.
pub const KEEPALIVE: &[u8] = b": \n\n";

/// One event. Every field is optional but at least one must be set.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct Event<'a> {
    pub event: Option<&'a str>,
    pub id: Option<&'a str>,
    /// The reconnection delay the client should use, in milliseconds.
    pub retry: Option<u64>,
    pub data: Option<&'a str>,
    pub comment: Option<&'a str>,
}

/// Why an event cannot be framed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FrameError {
    /// The named field holds a CR, LF or NUL.
    Forbidden(&'static str),
    /// No field is set.
    Empty,
}

impl fmt::Display for FrameError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Forbidden(field) => {
                write!(f, "the {field} of an event cannot hold CR, LF or NUL")
            }
            Self::Empty => f.write_str("an event needs at least one field"),
        }
    }
}

impl std::error::Error for FrameError {}

/// The lines of `text`, split at `\n`, `\r\n` or `\r`; the empty text is one
/// empty line.
fn lines(text: &str) -> impl Iterator<Item = &str> {
    let mut rest = Some(text);
    std::iter::from_fn(move || {
        let current = rest?;
        match current.find(['\r', '\n']) {
            Some(at) => {
                let skip = if current[at..].starts_with("\r\n") {
                    2
                } else {
                    1
                };
                rest = Some(&current[at + skip..]);
                Some(&current[..at])
            }
            None => {
                rest = None;
                Some(current)
            }
        }
    })
}

fn single_line(field: &'static str, value: &str) -> Result<(), FrameError> {
    if value.contains(['\r', '\n', '\0']) {
        Err(FrameError::Forbidden(field))
    } else {
        Ok(())
    }
}

fn push_lines(out: &mut Vec<u8>, name: &str, text: &str) {
    for line in lines(text) {
        out.extend_from_slice(name.as_bytes());
        out.extend_from_slice(b": ");
        out.extend_from_slice(line.as_bytes());
        out.push(b'\n');
    }
}

/// The wire bytes of one event, ending in the blank line that dispatches it.
pub fn frame(event: &Event<'_>) -> Result<Bytes, FrameError> {
    let Event {
        event: name,
        id,
        retry,
        data,
        comment,
    } = *event;
    if name.is_none() && id.is_none() && retry.is_none() && data.is_none() && comment.is_none() {
        return Err(FrameError::Empty);
    }
    let mut out = Vec::new();
    if let Some(comment) = comment {
        push_lines(&mut out, "", comment);
    }
    if let Some(name) = name {
        single_line("event", name)?;
        push_lines(&mut out, "event", name);
    }
    if let Some(id) = id {
        single_line("id", id)?;
        push_lines(&mut out, "id", id);
    }
    if let Some(retry) = retry {
        out.extend_from_slice(format!("retry: {retry}\n").as_bytes());
    }
    if let Some(data) = data {
        push_lines(&mut out, "data", data);
    }
    out.push(b'\n');
    Ok(Bytes::from(out))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn data(text: &str) -> Event<'_> {
        Event {
            data: Some(text),
            ..Event::default()
        }
    }

    fn framed(event: &Event<'_>) -> String {
        String::from_utf8(frame(event).unwrap().to_vec()).unwrap()
    }

    #[test]
    fn multi_line_data_becomes_one_field_line_per_line() {
        assert_eq!(framed(&data("a\nb")), "data: a\ndata: b\n\n");
        assert_eq!(framed(&data("a\r\nb")), "data: a\ndata: b\n\n");
        assert_eq!(framed(&data("a\rb")), "data: a\ndata: b\n\n");
        assert_eq!(framed(&data("a\n")), "data: a\ndata: \n\n");
        assert_eq!(
            framed(&data("a\n\r\rb")),
            "data: a\ndata: \ndata: \ndata: b\n\n"
        );
    }

    #[test]
    fn a_payload_cannot_forge_a_field() {
        let framed = framed(&data("x\nevent: admin\n\nevent: y"));
        assert!(framed
            .lines()
            .all(|line| line.is_empty() || line.starts_with("data: ")));
        assert_eq!(framed.matches("\n\n").count(), 1);
    }

    #[test]
    fn empty_data_is_one_empty_data_line() {
        assert_eq!(framed(&data("")), "data: \n\n");
    }

    #[test]
    fn every_field_in_order() {
        let event = Event {
            event: Some("tick"),
            id: Some("7"),
            retry: Some(2500),
            data: Some("go"),
            comment: Some("note"),
        };
        assert_eq!(
            framed(&event),
            ": note\nevent: tick\nid: 7\nretry: 2500\ndata: go\n\n"
        );
    }

    #[test]
    fn comments_are_split_per_line_too() {
        let event = Event {
            comment: Some("a\nb"),
            ..Event::default()
        };
        assert_eq!(framed(&event), ": a\n: b\n\n");
    }

    #[test]
    fn event_and_id_refuse_line_breaks_and_nul() {
        for bad in ["a\nb", "a\rb", "a\0b"] {
            let event = Event {
                event: Some(bad),
                data: Some("x"),
                ..Event::default()
            };
            assert_eq!(frame(&event), Err(FrameError::Forbidden("event")));
            let id = Event {
                id: Some(bad),
                data: Some("x"),
                ..Event::default()
            };
            assert_eq!(frame(&id), Err(FrameError::Forbidden("id")));
        }
        assert!(frame(&Event {
            id: Some(""),
            ..Event::default()
        })
        .is_ok());
    }

    #[test]
    fn an_event_without_fields_is_refused() {
        assert_eq!(frame(&Event::default()), Err(FrameError::Empty));
    }

    #[test]
    fn retry_alone_is_an_event() {
        let event = Event {
            retry: Some(0),
            ..Event::default()
        };
        assert_eq!(framed(&event), "retry: 0\n\n");
    }

    #[test]
    fn the_keep_alive_is_a_comment() {
        assert_eq!(KEEPALIVE, b": \n\n");
    }
}
