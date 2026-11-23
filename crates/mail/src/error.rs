//! Errors of the mail crate. Nothing here carries an address, a message part, a
//! credential or text a server sent: only a kind, an optional reply code and the
//! position of the offending input.

use std::fmt;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Kind {
    InvalidAddress,
    InvalidHeader,
    InvalidMessage,
    LimitExceeded,
    Closed,
    Busy,
    Connect,
    Timeout,
    Tls,
    Auth,
    Rejected,
    Temporary,
    Protocol,
    Template,
}

impl Kind {
    pub const ALL: [Kind; 14] = [
        Kind::InvalidAddress,
        Kind::InvalidHeader,
        Kind::InvalidMessage,
        Kind::LimitExceeded,
        Kind::Closed,
        Kind::Busy,
        Kind::Connect,
        Kind::Timeout,
        Kind::Tls,
        Kind::Auth,
        Kind::Rejected,
        Kind::Temporary,
        Kind::Protocol,
        Kind::Template,
    ];

    /// The stable snake_case name (the value of `EmailErrorKind`).
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::InvalidAddress => "invalid_address",
            Kind::InvalidHeader => "invalid_header",
            Kind::InvalidMessage => "invalid_message",
            Kind::LimitExceeded => "limit_exceeded",
            Kind::Closed => "closed",
            Kind::Busy => "busy",
            Kind::Connect => "connect",
            Kind::Timeout => "timeout",
            Kind::Tls => "tls",
            Kind::Auth => "auth",
            Kind::Rejected => "rejected",
            Kind::Temporary => "temporary",
            Kind::Protocol => "protocol",
            Kind::Template => "template",
        }
    }
}

/// The part of the message an error is about.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Field {
    Sender,
    To,
    Cc,
    Bcc,
    ReplyTo,
    Subject,
    Headers,
    Attachments,
    Text,
    Html,
}

impl Field {
    /// The name of the matching `Message` attribute.
    pub fn as_str(self) -> &'static str {
        match self {
            Field::Sender => "sender",
            Field::To => "to",
            Field::Cc => "cc",
            Field::Bcc => "bcc",
            Field::ReplyTo => "reply_to",
            Field::Subject => "subject",
            Field::Headers => "headers",
            Field::Attachments => "attachments",
            Field::Text => "text",
            Field::Html => "html",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MailError {
    pub kind: Kind,
    /// The SMTP reply code, when the server sent one.
    pub code: Option<u16>,
    /// The enhanced status (`5.1.1`) of that reply: digits and dots only.
    pub enhanced: Option<String>,
    /// Delivery attempts made (0 before any).
    pub attempts: u32,
    /// The end of the message was written and no reply came: it may have been
    /// accepted.
    pub possibly_sent: bool,
    pub field: Option<Field>,
    pub index: Option<u32>,
}

impl MailError {
    pub fn new(kind: Kind) -> Self {
        Self {
            kind,
            code: None,
            enhanced: None,
            attempts: 0,
            possibly_sent: false,
            field: None,
            index: None,
        }
    }

    pub fn at(kind: Kind, field: Field, index: Option<usize>) -> Self {
        Self {
            field: Some(field),
            index: index.and_then(|i| u32::try_from(i).ok()),
            ..Self::new(kind)
        }
    }

    pub(crate) fn with_reply(kind: Kind, code: u16, enhanced: Option<String>) -> Self {
        Self {
            code: Some(code),
            enhanced,
            ..Self::new(kind)
        }
    }
}

impl fmt::Display for MailError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let field = self.field.map_or("message", Field::as_str);
        match self.kind {
            Kind::InvalidAddress => match self.index {
                Some(index) => write!(f, "invalid email address in {field}[{index}]"),
                None => write!(f, "invalid email address in {field}"),
            },
            Kind::InvalidHeader => write!(f, "invalid header value in {field}"),
            Kind::InvalidMessage => write!(f, "invalid message: {field}"),
            Kind::LimitExceeded => write!(f, "message exceeds the {field} limit"),
            Kind::Closed => f.write_str("the mailer is closed"),
            Kind::Busy => f.write_str("no SMTP connection available"),
            Kind::Connect => f.write_str("could not connect to the SMTP server"),
            Kind::Timeout => f.write_str("SMTP operation timed out"),
            Kind::Tls => f.write_str("TLS negotiation with the SMTP server failed"),
            Kind::Auth => f.write_str("SMTP authentication failed"),
            Kind::Rejected => write_code(f, "the SMTP server rejected the message", self.code),
            Kind::Temporary => write_code(f, "the SMTP server deferred the message", self.code),
            Kind::Protocol => f.write_str("unexpected SMTP server response"),
            Kind::Template => f.write_str("invalid template"),
        }
    }
}

fn write_code(f: &mut fmt::Formatter<'_>, text: &str, code: Option<u16>) -> fmt::Result {
    match code {
        Some(code) => write!(f, "{text} (code {code})"),
        None => f.write_str(text),
    }
}

impl std::error::Error for MailError {}

/// A setting the mailer cannot be built from. The text is fixed and names the
/// setting, never its value.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigError(pub(crate) &'static str);

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.0)
    }
}

impl std::error::Error for ConfigError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn display_is_a_fixed_sentence_with_position_and_code_only() {
        let error = MailError::at(Kind::InvalidAddress, Field::Cc, Some(2));
        assert_eq!(error.to_string(), "invalid email address in cc[2]");
        let error = MailError::at(Kind::LimitExceeded, Field::Attachments, None);
        assert_eq!(error.to_string(), "message exceeds the attachments limit");
        let error = MailError::with_reply(Kind::Rejected, 550, Some("5.1.1".to_owned()));
        assert_eq!(
            error.to_string(),
            "the SMTP server rejected the message (code 550)"
        );
        assert_eq!(
            MailError::new(Kind::Temporary).to_string(),
            "the SMTP server deferred the message"
        );
    }

    #[test]
    fn every_kind_has_a_distinct_name_and_a_sentence() {
        let mut names: Vec<_> = Kind::ALL.iter().map(|kind| kind.as_str()).collect();
        names.sort_unstable();
        names.dedup();
        assert_eq!(names.len(), Kind::ALL.len());
        for kind in Kind::ALL {
            let text = MailError::new(kind).to_string();
            assert!(!text.is_empty());
        }
    }

    #[test]
    fn field_names_match_the_message_attributes() {
        let names: Vec<_> = [
            Field::Sender,
            Field::To,
            Field::Cc,
            Field::Bcc,
            Field::ReplyTo,
            Field::Subject,
            Field::Headers,
            Field::Attachments,
            Field::Text,
            Field::Html,
        ]
        .into_iter()
        .map(Field::as_str)
        .collect();
        assert_eq!(
            names,
            [
                "sender",
                "to",
                "cc",
                "bcc",
                "reply_to",
                "subject",
                "headers",
                "attachments",
                "text",
                "html"
            ]
        );
    }

    #[test]
    fn config_error_prints_its_fixed_text() {
        assert_eq!(ConfigError("bad setting").to_string(), "bad setting");
    }
}
