//! Rendering through the public API: structure, encoding and refusals.

use std::sync::Arc;

use hinoki_mail::{
    mailboxes, render, AttachmentPart, Draft, Field, IdSource, Kind, Limits, MailError,
    RenderContext,
};
use time::OffsetDateTime;

struct Fixed;

impl IdSource for Fixed {
    fn fill(&self, buf: &mut [u8]) -> Result<(), MailError> {
        buf.fill(0xab);
        Ok(())
    }
}

struct Failing;

impl IdSource for Failing {
    fn fill(&self, _: &mut [u8]) -> Result<(), MailError> {
        Err(MailError::new(Kind::Protocol))
    }
}

fn ctx_with(ids: Arc<dyn IdSource>) -> RenderContext {
    RenderContext {
        message_id_domain: "mail.example.com".to_owned(),
        now: OffsetDateTime::from_unix_timestamp(1_700_000_000).unwrap(),
        ids,
    }
}

fn ctx() -> RenderContext {
    ctx_with(Arc::new(Fixed))
}

fn list(field: Field, items: &[(&str, Option<&str>)]) -> Vec<hinoki_mail::Mailbox> {
    let items = items
        .iter()
        .map(|(a, n)| ((*a).to_owned(), n.map(str::to_owned)))
        .collect();
    mailboxes(field, items).unwrap()
}

fn draft() -> Draft {
    Draft {
        sender: list(Field::Sender, &[("from@example.com", None)]).remove(0),
        to: list(Field::To, &[("to@example.com", None)]),
        cc: vec![],
        bcc: vec![],
        reply_to: vec![],
        subject: "Hello".to_owned(),
        text: Some("Body".to_owned()),
        html: None,
        attachments: vec![],
        headers: vec![],
    }
}

fn text(draft: Draft) -> String {
    let rendered = render(draft, &ctx(), &Limits::default()).unwrap();
    String::from_utf8(rendered.bytes).unwrap()
}

fn refused(draft: Draft) -> MailError {
    render(draft, &ctx(), &Limits::default()).unwrap_err()
}

#[test]
fn a_plain_message_is_rendered_exactly() {
    let rendered = render(draft(), &ctx(), &Limits::default()).unwrap();
    let id = format!("<{}@mail.example.com>", "ab".repeat(16));
    assert_eq!(rendered.message_id, id);
    let expected = format!(
        "Date: Tue, 14 Nov 2023 22:13:20 +0000\r\n\
         Message-ID: {id}\r\n\
         From: from@example.com\r\n\
         To: to@example.com\r\n\
         Subject: Hello\r\n\
         MIME-Version: 1.0\r\n\
         Content-Type: text/plain; charset=utf-8\r\n\
         Content-Transfer-Encoding: quoted-printable\r\n\
         \r\n\
         Body\r\n"
    );
    assert_eq!(String::from_utf8(rendered.bytes).unwrap(), expected);
    assert_eq!(rendered.envelope.from.as_str(), "from@example.com");
}

#[test]
fn text_and_html_make_an_alternative() {
    let mut d = draft();
    d.html = Some("<p>Body</p>".to_owned());
    let out = text(d);
    let boundary = "ab".repeat(12);
    assert!(out.contains("Content-Type: multipart/alternative; boundary=\""));
    assert!(out.contains(&boundary));
    let plain = out.find("text/plain").unwrap();
    let html = out.find("text/html").unwrap();
    assert!(plain < html);
    assert!(out.trim_end().ends_with("--"));
}

#[test]
fn attachments_make_a_mixed_message_with_base64() {
    let mut d = draft();
    d.html = Some("<p>x</p>".to_owned());
    d.attachments = vec![AttachmentPart {
        filename: "report.txt".to_owned(),
        content_type: "text/plain".to_owned(),
        content: b"hello attachment".to_vec(),
    }];
    let out = text(d);
    assert!(out.contains("multipart/mixed"));
    assert!(out.contains("multipart/alternative"));
    assert!(out.contains("Content-Transfer-Encoding: base64"));
    assert!(out.contains("filename=\"report.txt\""));
    assert!(out.contains("aGVsbG8gYXR0YWNobWVudA=="));
}

#[test]
fn bcc_is_in_the_envelope_but_never_in_the_message() {
    let mut d = draft();
    d.cc = list(Field::Cc, &[("cc@example.com", None)]);
    d.bcc = list(
        Field::Bcc,
        &[("secret@example.com", None), ("to@example.com", None)],
    );
    let rendered = render(d, &ctx(), &Limits::default()).unwrap();
    let body = String::from_utf8(rendered.bytes).unwrap();
    assert!(!body.contains("secret@example.com"));
    assert!(!body.to_lowercase().contains("bcc"));
    let to: Vec<&str> = rendered.envelope.to.iter().map(|a| a.as_str()).collect();
    assert_eq!(
        to,
        ["to@example.com", "cc@example.com", "secret@example.com"]
    );
    assert_eq!(
        rendered.envelope.origins,
        [(Field::To, 0), (Field::Cc, 0), (Field::Bcc, 0)]
    );
}

#[test]
fn the_output_is_ascii_with_short_lines_whatever_the_input() {
    let mut d = draft();
    d.sender = list(
        Field::Sender,
        &[("from@example.com", Some("Zoë, \"Z\" Müller"))],
    )
    .remove(0);
    d.to = list(Field::To, &[("to@example.com", Some("Ünïcode Name"))]);
    d.subject = "Grüße — ".repeat(40);
    d.text = Some(format!("{}\nsecond line é\n\n.dot", "long ".repeat(500)));
    d.html = Some("<p>é</p>".repeat(300));
    d.headers = vec![("X-Note".to_owned(), "café".to_owned())];
    d.attachments = vec![AttachmentPart {
        filename: "résumé 日本.pdf".to_owned(),
        content_type: "application/pdf".to_owned(),
        content: vec![7; 5000],
    }];
    let out = text(d);
    assert!(out.is_ascii());
    assert!(out.ends_with("\r\n"));
    assert!(!out.replace("\r\n", "").contains(['\r', '\n']));
    assert!(out.split("\r\n").all(|line| line.len() <= 998));
    assert!(out.contains("=?utf-8?"));
    assert!(out.contains("filename*"));
}

#[test]
fn a_display_name_with_specials_is_quoted() {
    let mut d = draft();
    d.to = list(Field::To, &[("to@example.com", Some("Doe, John"))]);
    assert!(text(d).contains("To: \"Doe, John\" <to@example.com>"));
}

#[test]
fn line_breaks_cannot_be_smuggled_into_any_header() {
    let evil = "x\r\nBcc: attacker@example.com";
    let mut cases: Vec<(Draft, Field)> = Vec::new();
    let mut d = draft();
    d.subject = evil.to_owned();
    cases.push((d, Field::Subject));
    let mut d = draft();
    d.to = list(Field::To, &[("to@example.com", Some(evil))]);
    cases.push((d, Field::To));
    let mut d = draft();
    d.sender = list(Field::Sender, &[("from@example.com", Some("a\nb"))]).remove(0);
    cases.push((d, Field::Sender));
    let mut d = draft();
    d.reply_to = list(Field::ReplyTo, &[("r@example.com", Some("a\rb"))]);
    cases.push((d, Field::ReplyTo));
    let mut d = draft();
    d.headers = vec![("X-A".to_owned(), evil.to_owned())];
    cases.push((d, Field::Headers));
    let mut d = draft();
    d.headers = vec![("X-A\r\nBcc".to_owned(), "v".to_owned())];
    cases.push((d, Field::Headers));
    for (d, field) in cases {
        let error = refused(d);
        assert_eq!(error.kind, Kind::InvalidHeader);
        assert_eq!(error.field, Some(field));
    }
}

#[test]
fn reserved_and_malformed_extra_headers_are_refused() {
    for name in [
        "Subject",
        "from",
        "Message-ID",
        "Content-Type",
        "bcc",
        "",
        "X Bad",
        "X:Bad",
    ] {
        let mut d = draft();
        d.headers = vec![(name.to_owned(), "v".to_owned())];
        let error = refused(d);
        assert_eq!(error.kind, Kind::InvalidHeader, "{name}");
        assert_eq!(error.index, Some(0));
    }
}

#[test]
fn a_message_needs_a_recipient_and_a_body() {
    let mut d = draft();
    d.to.clear();
    assert_eq!(refused(d).kind, Kind::InvalidMessage);
    let mut d = draft();
    d.text = None;
    let error = refused(d);
    assert_eq!(
        (error.kind, error.field),
        (Kind::InvalidMessage, Some(Field::Text))
    );
    let mut d = draft();
    d.html = Some("a\0b".to_owned());
    let error = refused(d);
    assert_eq!(
        (error.kind, error.field),
        (Kind::InvalidMessage, Some(Field::Html))
    );
}

#[test]
fn limits_are_enforced_before_encoding() {
    let limits = Limits {
        max_recipients: 2,
        max_message_bytes: 2048,
        max_attachments: 1,
    };
    let go = |d: Draft| render(d, &ctx(), &limits).unwrap_err();
    let mut d = draft();
    d.cc = list(
        Field::Cc,
        &[("a@example.com", None), ("b@example.com", None)],
    );
    let error = go(d);
    assert_eq!(
        (error.kind, error.field),
        (Kind::LimitExceeded, Some(Field::To))
    );
    let mut d = draft();
    d.text = Some("x".repeat(4096));
    assert_eq!(go(d).kind, Kind::LimitExceeded);
    let mut d = draft();
    d.attachments = vec![
        AttachmentPart {
            filename: "a".to_owned(),
            content_type: "text/plain".to_owned(),
            content: vec![],
        };
        2
    ];
    let error = go(d);
    assert_eq!(
        (error.kind, error.field),
        (Kind::LimitExceeded, Some(Field::Attachments))
    );
    let mut d = draft();
    d.subject = "s".repeat(1001);
    assert_eq!(go(d).field, Some(Field::Subject));
    let mut d = draft();
    d.headers = vec![("X-A".to_owned(), "v".to_owned()); 33];
    assert_eq!(go(d).field, Some(Field::Headers));
    let mut d = draft();
    d.headers = vec![("X-A".to_owned(), "v".repeat(2001))];
    assert_eq!(go(d).kind, Kind::LimitExceeded);
    let mut d = draft();
    d.to = list(Field::To, &[("to@example.com", Some(&"n".repeat(257)))]);
    assert_eq!(go(d).kind, Kind::LimitExceeded);
}

#[test]
fn the_rendered_size_is_checked_after_encoding() {
    let limits = Limits {
        max_recipients: 10,
        max_message_bytes: 4096,
        max_attachments: 5,
    };
    let mut d = draft();
    // Quoted-printable triples every non-ASCII byte: under the estimate, over
    // the limit once encoded.
    d.text = Some("é".repeat(1500));
    let error = render(d, &ctx(), &limits).unwrap_err();
    assert_eq!(error.kind, Kind::LimitExceeded);
}

#[test]
fn bad_attachment_names_and_types_are_refused() {
    for (name, content_type) in [
        ("", "text/plain"),
        ("a/b", "text/plain"),
        ("a\\b", "text/plain"),
        ("a\nb", "text/plain"),
        ("ok.txt", "text/plain\r\nX: y"),
        ("ok.txt", "plain"),
    ] {
        let mut d = draft();
        d.attachments = vec![AttachmentPart {
            filename: name.to_owned(),
            content_type: content_type.to_owned(),
            content: vec![1],
        }];
        let error = refused(d);
        assert_eq!(
            (error.kind, error.field, error.index),
            (Kind::InvalidHeader, Some(Field::Attachments), Some(0)),
            "{name:?} {content_type:?}"
        );
    }
}

#[test]
fn addresses_are_validated_with_their_position() {
    let bad = [
        "plain",
        "a@b",
        "\"quoted\"@example.com",
        "a b@example.com",
        "a@exa mple.com",
        "a@[127.0.0.1]",
        "a@@example.com",
        ".a@example.com",
        "a..b@example.com",
        "a\r\n@example.com",
        "ü@example.com",
        "",
    ];
    for address in bad {
        let error = mailboxes(
            Field::Cc,
            vec![
                ("ok@example.com".to_owned(), None),
                (address.to_owned(), None),
            ],
        )
        .unwrap_err();
        assert_eq!(
            (error.kind, error.field, error.index),
            (Kind::InvalidAddress, Some(Field::Cc), Some(1)),
            "{address:?}"
        );
    }
    let long_local = format!("{}@example.com", "a".repeat(65));
    assert!(mailboxes(Field::To, vec![(long_local, None)]).is_err());
}

#[test]
fn internationalized_domains_become_punycode() {
    let boxes = mailboxes(Field::To, vec![("User@Bücher.Example".to_owned(), None)]).unwrap();
    assert_eq!(boxes[0].address.as_str(), "User@xn--bcher-kva.example");
    assert_eq!(boxes[0].address.domain(), "xn--bcher-kva.example");
}

#[test]
fn a_failing_random_source_fails_the_render() {
    let error = render(draft(), &ctx_with(Arc::new(Failing)), &Limits::default()).unwrap_err();
    assert_eq!(error.kind, Kind::Protocol);
}

#[test]
fn errors_never_show_input() {
    let mut d = draft();
    d.subject = "secret-subject\n".to_owned();
    let error = refused(d);
    let shown = format!("{error} {error:?}");
    assert!(!shown.contains("secret"));
    assert!(!shown.contains("example.com"));
}
