//! Limits on one message. The hard caps are fixed; the values are settings.

use crate::error::{ConfigError, Field, Kind, MailError};

pub const MAX_RECIPIENTS_CAP: usize = 1000;
pub const MAX_ATTACHMENTS_CAP: usize = 100;
pub const MIN_MESSAGE_BYTES: usize = 1024;
pub const MAX_MESSAGE_BYTES_CAP: usize = 50 * 1024 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Limits {
    /// Recipients across to, cc and bcc.
    pub max_recipients: usize,
    /// Size of the rendered message.
    pub max_message_bytes: usize,
    pub max_attachments: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_recipients: 100,
            max_message_bytes: 10 * 1024 * 1024,
            max_attachments: 20,
        }
    }
}

impl Limits {
    /// Refuse values outside the hard caps.
    pub fn validate(&self) -> Result<(), ConfigError> {
        if !(1..=MAX_RECIPIENTS_CAP).contains(&self.max_recipients) {
            return Err(ConfigError("max_recipients must be between 1 and 1000"));
        }
        if !(MIN_MESSAGE_BYTES..=MAX_MESSAGE_BYTES_CAP).contains(&self.max_message_bytes) {
            return Err(ConfigError(
                "max_message_bytes must be between 1 KiB and 50 MiB",
            ));
        }
        if self.max_attachments > MAX_ATTACHMENTS_CAP {
            return Err(ConfigError("max_attachments must be at most 100"));
        }
        Ok(())
    }

    /// Check the counts and the bytes of a message (attachments, text and HTML
    /// as they are, before any encoding) before its parts are copied or
    /// encoded.
    pub fn check_inputs(
        &self,
        recipients: usize,
        attachments: usize,
        attachment_bytes: usize,
        text_bytes: usize,
        html_bytes: usize,
    ) -> Result<(), MailError> {
        if recipients == 0 {
            return Err(MailError::at(Kind::InvalidMessage, Field::To, None));
        }
        if recipients > self.max_recipients {
            return Err(MailError::at(Kind::LimitExceeded, Field::To, None));
        }
        if attachments > self.max_attachments {
            return Err(MailError::at(Kind::LimitExceeded, Field::Attachments, None));
        }
        let written = text_bytes.saturating_add(html_bytes);
        if attachment_bytes.saturating_add(written) > self.max_message_bytes {
            let field = if attachment_bytes > written {
                Field::Attachments
            } else if text_bytes >= html_bytes {
                Field::Text
            } else {
                Field::Html
            };
            return Err(MailError::at(Kind::LimitExceeded, field, None));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_valid() {
        assert_eq!(Limits::default().validate(), Ok(()));
    }

    #[test]
    fn values_outside_the_hard_caps_are_refused() {
        let base = Limits::default();
        let bad = [
            Limits {
                max_recipients: 0,
                ..base
            },
            Limits {
                max_recipients: 1001,
                ..base
            },
            Limits {
                max_message_bytes: 1023,
                ..base
            },
            Limits {
                max_message_bytes: MAX_MESSAGE_BYTES_CAP + 1,
                ..base
            },
            Limits {
                max_attachments: 101,
                ..base
            },
        ];
        for limits in bad {
            assert!(limits.validate().is_err(), "{limits:?}");
        }
        let edges = Limits {
            max_recipients: 1000,
            max_message_bytes: 1024,
            max_attachments: 0,
        };
        assert_eq!(edges.validate(), Ok(()));
    }

    #[test]
    fn inputs_are_checked_against_each_limit() {
        let limits = Limits {
            max_recipients: 2,
            max_message_bytes: 2048,
            max_attachments: 1,
        };
        assert_eq!(limits.check_inputs(2, 1, 2048, 0, 0), Ok(()));
        let kind = |r, a, b| limits.check_inputs(r, a, b, 0, 0).unwrap_err();
        assert_eq!(kind(0, 0, 0).kind, Kind::InvalidMessage);
        assert_eq!(kind(3, 0, 0).kind, Kind::LimitExceeded);
        assert_eq!(kind(3, 0, 0).field, Some(Field::To));
        assert_eq!(kind(1, 2, 0).field, Some(Field::Attachments));
        assert_eq!(kind(1, 1, 2049).field, Some(Field::Attachments));
        let body = |text, html| limits.check_inputs(1, 0, 0, text, html).unwrap_err();
        assert_eq!(body(2049, 0).field, Some(Field::Text));
        assert_eq!(body(0, 2049).field, Some(Field::Html));
        assert_eq!(body(1025, 1025).kind, Kind::LimitExceeded);
        assert_eq!(limits.check_inputs(1, 1, 1024, 512, 512), Ok(()));
        assert_eq!(
            limits
                .check_inputs(1, 1, 1024, usize::MAX, usize::MAX)
                .unwrap_err()
                .kind,
            Kind::LimitExceeded
        );
    }
}
