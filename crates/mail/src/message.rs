//! The message a caller describes ([`Draft`]) and its rendered form
//! ([`Rendered`]): validated headers, MIME structure and the SMTP envelope.

use std::sync::Arc;

use time::format_description::well_known::Rfc2822;
use time::{OffsetDateTime, UtcOffset};

use crate::address::{parse_addr_spec, Address, Mailbox};
use crate::error::{Field, Kind, MailError};
use crate::header::{
    fold, has_control_char, is_reserved, mailbox_atoms, unstructured, valid_header_name,
};
use crate::limits::Limits;
use crate::mime::{
    base64_lines, boundary, filename_params, normalize_crlf, quoted_printable, rfc2231_value,
    valid_content_type, MAX_ENCODED_FILENAME,
};

const MAX_HEADERS: usize = 32;
const MAX_SUBJECT: usize = 1000;
const MAX_HEADER_VALUE: usize = 2000;
const MAX_NAME: usize = 256;
const MAX_FILENAME: usize = 255;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AttachmentPart {
    pub filename: String,
    pub content_type: String,
    pub content: Vec<u8>,
}

/// What a caller wants to send. Every address is already validated; the rest is
/// checked by [`render`].
#[derive(Clone, Debug)]
pub struct Draft {
    pub sender: Mailbox,
    pub to: Vec<Mailbox>,
    pub cc: Vec<Mailbox>,
    pub bcc: Vec<Mailbox>,
    pub reply_to: Vec<Mailbox>,
    pub subject: String,
    pub text: Option<String>,
    pub html: Option<String>,
    pub attachments: Vec<AttachmentPart>,
    pub headers: Vec<(String, String)>,
}

/// The SMTP envelope: the sender and the deduplicated recipients (to, cc and
/// bcc, in that order).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Envelope {
    pub from: Address,
    pub to: Vec<Address>,
    /// For each recipient, the field and position of the first mailbox it came
    /// from, so a refusal can name it.
    pub origins: Vec<(Field, usize)>,
}

#[derive(Clone, Debug)]
pub struct Rendered {
    pub envelope: Envelope,
    /// `<hex@domain>` as written in the `Message-ID` header.
    pub message_id: String,
    /// The message: 7-bit ASCII, CRLF line ends, no line over 998 octets.
    pub bytes: Vec<u8>,
}

/// A source of random bytes for message ids and boundaries.
pub trait IdSource: Send + Sync {
    fn fill(&self, buf: &mut [u8]) -> Result<(), MailError>;
}

pub struct SystemIds;

impl IdSource for SystemIds {
    fn fill(&self, buf: &mut [u8]) -> Result<(), MailError> {
        getrandom::fill(buf).map_err(|_| MailError::new(Kind::Protocol))
    }
}

#[derive(Clone)]
pub struct RenderContext {
    pub message_id_domain: String,
    pub now: OffsetDateTime,
    pub ids: Arc<dyn IdSource>,
}

/// Validate the addresses of `items` (address, display name) of `field`.
pub fn mailboxes(
    field: Field,
    items: Vec<(String, Option<String>)>,
) -> Result<Vec<Mailbox>, MailError> {
    items
        .into_iter()
        .enumerate()
        .map(|(index, (email, name))| {
            let address =
                parse_addr_spec(&email).map_err(|kind| MailError::at(kind, field, Some(index)))?;
            Ok(Mailbox { address, name })
        })
        .collect()
}

fn check_list(list: &[Mailbox], field: Field) -> Result<(), MailError> {
    for (index, mailbox) in list.iter().enumerate() {
        let Some(name) = &mailbox.name else { continue };
        if has_control_char(name) {
            return Err(MailError::at(Kind::InvalidHeader, field, Some(index)));
        }
        if name.chars().count() > MAX_NAME {
            return Err(MailError::at(Kind::LimitExceeded, field, Some(index)));
        }
    }
    Ok(())
}

fn check_headers(headers: &[(String, String)]) -> Result<(), MailError> {
    if headers.len() > MAX_HEADERS {
        return Err(MailError::at(Kind::LimitExceeded, Field::Headers, None));
    }
    for (index, (name, value)) in headers.iter().enumerate() {
        let at = |kind| MailError::at(kind, Field::Headers, Some(index));
        if !valid_header_name(name) || is_reserved(name) || has_control_char(value) {
            return Err(at(Kind::InvalidHeader));
        }
        if value.chars().count() > MAX_HEADER_VALUE {
            return Err(at(Kind::LimitExceeded));
        }
    }
    Ok(())
}

fn valid_filename(name: &str) -> bool {
    let length = name.chars().count();
    (1..=MAX_FILENAME).contains(&length)
        && !name
            .chars()
            .any(|ch| ch.is_control() || matches!(ch, '/' | '\\'))
        && rfc2231_value(name).len() <= MAX_ENCODED_FILENAME
}

fn check_attachments(attachments: &[AttachmentPart]) -> Result<(), MailError> {
    for (index, part) in attachments.iter().enumerate() {
        if !valid_filename(&part.filename) || !valid_content_type(&part.content_type) {
            return Err(MailError::at(
                Kind::InvalidHeader,
                Field::Attachments,
                Some(index),
            ));
        }
    }
    Ok(())
}

fn check_bodies(draft: &Draft) -> Result<(), MailError> {
    if draft.text.is_none() && draft.html.is_none() {
        return Err(MailError::at(Kind::InvalidMessage, Field::Text, None));
    }
    for (body, field) in [(&draft.text, Field::Text), (&draft.html, Field::Html)] {
        if body.as_deref().is_some_and(|text| text.contains('\0')) {
            return Err(MailError::at(Kind::InvalidMessage, field, None));
        }
    }
    Ok(())
}

fn check_subject(subject: &str) -> Result<(), MailError> {
    if has_control_char(subject) {
        return Err(MailError::at(Kind::InvalidHeader, Field::Subject, None));
    }
    if subject.chars().count() > MAX_SUBJECT {
        return Err(MailError::at(Kind::LimitExceeded, Field::Subject, None));
    }
    Ok(())
}

/// The field a size failure is blamed on: the attachments when they are most of
/// the message, otherwise the body.
fn size_field(draft: &Draft) -> Field {
    let attached: usize = draft.attachments.iter().map(|a| a.content.len()).sum();
    let written =
        draft.text.as_ref().map_or(0, String::len) + draft.html.as_ref().map_or(0, String::len);
    if attached > written {
        Field::Attachments
    } else if draft.text.is_some() {
        Field::Text
    } else {
        Field::Html
    }
}

/// Refuse a message whose smallest possible encoding is over the limit, before
/// anything is encoded.
fn check_estimate(draft: &Draft, limits: &Limits) -> Result<(), MailError> {
    let attached: usize = draft
        .attachments
        .iter()
        .map(|a| a.content.len() / 3 * 4)
        .sum();
    let written =
        draft.text.as_ref().map_or(0, String::len) + draft.html.as_ref().map_or(0, String::len);
    if attached.saturating_add(written) > limits.max_message_bytes {
        return Err(MailError::at(Kind::LimitExceeded, size_field(draft), None));
    }
    Ok(())
}

fn validate(draft: &Draft, limits: &Limits) -> Result<(), MailError> {
    let recipients = draft.to.len() + draft.cc.len() + draft.bcc.len();
    let attached: usize = draft.attachments.iter().map(|a| a.content.len()).sum();
    limits.check_inputs(
        recipients,
        draft.attachments.len(),
        attached,
        draft.text.as_ref().map_or(0, String::len),
        draft.html.as_ref().map_or(0, String::len),
    )?;
    check_list(std::slice::from_ref(&draft.sender), Field::Sender)?;
    for (list, field) in [
        (&draft.to, Field::To),
        (&draft.cc, Field::Cc),
        (&draft.reply_to, Field::ReplyTo),
        (&draft.bcc, Field::Bcc),
    ] {
        check_list(list, field)?;
    }
    check_subject(&draft.subject)?;
    check_bodies(draft)?;
    check_headers(&draft.headers)?;
    check_attachments(&draft.attachments)?;
    check_estimate(draft, limits)
}

fn envelope_of(draft: &Draft) -> Envelope {
    let mut to: Vec<Address> = Vec::new();
    let mut origins = Vec::new();
    for (list, field) in [
        (&draft.to, Field::To),
        (&draft.cc, Field::Cc),
        (&draft.bcc, Field::Bcc),
    ] {
        for (index, mailbox) in list.iter().enumerate() {
            if !to.contains(&mailbox.address) {
                to.push(mailbox.address.clone());
                origins.push((field, index));
            }
        }
    }
    Envelope {
        from: draft.sender.address.clone(),
        to,
        origins,
    }
}

/// A MIME part: header lines (each ending in CRLF) and a body without a final
/// line break.
struct Part {
    headers: String,
    body: String,
}

impl Part {
    fn text(content_type: &str, text: &str) -> Self {
        Self {
            headers: format!(
                "Content-Type: {content_type}; charset=utf-8\r\n\
                 Content-Transfer-Encoding: quoted-printable\r\n"
            ),
            body: quoted_printable(&normalize_crlf(text)),
        }
    }

    fn attachment(part: &AttachmentPart) -> Self {
        let mut atoms = vec!["attachment;".to_owned()];
        let params = filename_params(&part.filename);
        let last = params.len() - 1;
        for (index, param) in params.into_iter().enumerate() {
            atoms.push(if index < last {
                format!("{param};")
            } else {
                param
            });
        }
        let body = base64_lines(&part.content);
        Self {
            headers: format!(
                "Content-Type: {}\r\nContent-Transfer-Encoding: base64\r\n{}",
                part.content_type,
                fold("Content-Disposition", &atoms)
            ),
            body: body.trim_end_matches("\r\n").to_owned(),
        }
    }

    fn multipart(subtype: &str, ids: &dyn IdSource, parts: Vec<Part>) -> Result<Self, MailError> {
        let mut random = [0u8; 12];
        ids.fill(&mut random)?;
        let boundary = boundary(&random);
        let mut body = String::new();
        for part in &parts {
            body.push_str(&format!(
                "--{boundary}\r\n{}\r\n{}\r\n",
                part.headers, part.body
            ));
        }
        body.push_str(&format!("--{boundary}--"));
        Ok(Self {
            headers: format!("Content-Type: multipart/{subtype}; boundary=\"{boundary}\"\r\n"),
            body,
        })
    }
}

fn content_part(draft: &Draft, ids: &dyn IdSource) -> Result<Part, MailError> {
    let mut bodies = Vec::new();
    if let Some(text) = &draft.text {
        bodies.push(Part::text("text/plain", text));
    }
    if let Some(html) = &draft.html {
        bodies.push(Part::text("text/html", html));
    }
    let body = if bodies.len() == 2 {
        Part::multipart("alternative", ids, bodies)?
    } else {
        bodies.remove(0)
    };
    if draft.attachments.is_empty() {
        return Ok(body);
    }
    let mut parts = vec![body];
    parts.extend(draft.attachments.iter().map(Part::attachment));
    Part::multipart("mixed", ids, parts)
}

fn address_header(name: &str, list: &[Mailbox]) -> String {
    let atoms: Vec<String> = list
        .iter()
        .enumerate()
        .flat_map(|(index, mailbox)| mailbox_atoms(mailbox, index + 1 < list.len()))
        .collect();
    fold(name, &atoms)
}

fn message_id(ctx: &RenderContext) -> Result<String, MailError> {
    let mut random = [0u8; 16];
    ctx.ids.fill(&mut random)?;
    let hex: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
    Ok(format!("<{hex}@{}>", ctx.message_id_domain))
}

fn header_block(draft: &Draft, ctx: &RenderContext, id: &str) -> Result<String, MailError> {
    let date = ctx
        .now
        .to_offset(UtcOffset::UTC)
        .format(&Rfc2822)
        .map_err(|_| MailError::new(Kind::InvalidMessage))?;
    let mut out = format!("Date: {date}\r\nMessage-ID: {id}\r\n");
    out.push_str(&address_header("From", std::slice::from_ref(&draft.sender)));
    for (name, list) in [
        ("To", &draft.to),
        ("Cc", &draft.cc),
        ("Reply-To", &draft.reply_to),
    ] {
        if !list.is_empty() {
            out.push_str(&address_header(name, list));
        }
    }
    out.push_str(&fold("Subject", &unstructured(&draft.subject)));
    for (name, value) in &draft.headers {
        out.push_str(&fold(name, &unstructured(value)));
    }
    out.push_str("MIME-Version: 1.0\r\n");
    Ok(out)
}

/// Validate `draft` against `limits` and render it.
pub fn render(draft: Draft, ctx: &RenderContext, limits: &Limits) -> Result<Rendered, MailError> {
    validate(&draft, limits)?;
    let id = message_id(ctx)?;
    let content = content_part(&draft, ctx.ids.as_ref())?;
    let mut text = header_block(&draft, ctx, &id)?;
    text.push_str(&content.headers);
    text.push_str("\r\n");
    text.push_str(&content.body);
    text.push_str("\r\n");
    if text.len() > limits.max_message_bytes {
        return Err(MailError::at(Kind::LimitExceeded, size_field(&draft), None));
    }
    Ok(Rendered {
        envelope: envelope_of(&draft),
        message_id: id,
        bytes: text.into_bytes(),
    })
}
