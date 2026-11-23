//! Templates through the public API: compile, verify, render, and the result
//! handed to the message renderer.

use std::sync::Arc;

use hinoki_mail::template_limits::{MAX_ITERATIONS, MAX_OUTPUT_BYTES};
use hinoki_mail::{
    mailboxes, render, Draft, Field, IdSource, Kind, Limits, MailError, Reason, RenderContext,
    Shape, Template, Value,
};
use time::OffsetDateTime;

struct Fixed;

impl IdSource for Fixed {
    fn fill(&self, buf: &mut [u8]) -> Result<(), MailError> {
        buf.fill(0xab);
        Ok(())
    }
}

fn mailbox(field: Field, address: &str) -> Vec<hinoki_mail::Mailbox> {
    mailboxes(field, vec![(address.to_owned(), None)]).unwrap()
}

fn draft(subject: String, text: String, html: String) -> Draft {
    Draft {
        sender: mailbox(Field::Sender, "noreply@example.com").remove(0),
        to: mailbox(Field::To, "ada@example.com"),
        cc: vec![],
        bcc: vec![],
        reply_to: vec![],
        subject,
        text: Some(text),
        html: Some(html),
        attachments: vec![],
        headers: vec![],
    }
}

fn text(value: &str) -> Value {
    Value::Str(value.to_owned())
}

fn context() -> Value {
    let step = |title: &str| {
        Value::map(vec![
            ("title".to_owned(), text(title)),
            ("url".to_owned(), text("https://example.com/start")),
        ])
    };
    Value::map(vec![
        ("name".to_owned(), text("Ada <Lovelace>")),
        (
            "steps".to_owned(),
            Value::List(vec![step("One"), step("Two & three")]),
        ),
    ])
}

fn shape() -> Shape {
    let step = Shape::Object(vec![
        ("title".to_owned(), Shape::Scalar),
        ("url".to_owned(), Shape::Scalar),
    ]);
    Shape::Object(vec![
        ("name".to_owned(), Shape::Scalar),
        ("steps".to_owned(), Shape::List(Box::new(step))),
    ])
}

const HTML: &str = "<h1>Welcome, {{ name }}</h1>\n<ol>\n{% for s in steps %}\n\
    <li><a href=\"{{ s.url }}\">{{ s.title }}</a></li>\n{% endfor %}\n</ol>\n";

#[test]
fn a_template_is_verified_rendered_and_accepted_by_the_message_renderer() {
    let template = Template::compile("welcome", "Welcome, {{ name }}", None, HTML).unwrap();
    template.verify(&shape()).unwrap();
    let out = template.render(&context()).unwrap();
    assert_eq!(out.subject, "Welcome, Ada <Lovelace>");
    assert!(out.html.contains("<h1>Welcome, Ada &lt;Lovelace&gt;</h1>"));
    assert!(out.html.contains("Two &amp; three"));
    assert_eq!(
        out.text,
        "Welcome, Ada <Lovelace>\n\n\
         - One (https://example.com/start)\n- Two & three (https://example.com/start)"
    );
    let ctx = RenderContext {
        message_id_domain: "mail.example.com".to_owned(),
        now: OffsetDateTime::from_unix_timestamp(1_700_000_000).unwrap(),
        ids: Arc::new(Fixed),
    };
    let rendered = render(
        draft(out.subject, out.text, out.html),
        &ctx,
        &Limits::default(),
    )
    .unwrap();
    let message = String::from_utf8(rendered.bytes).unwrap();
    assert!(message.contains("Content-Type: multipart/alternative"));
    assert!(message.contains("Subject: Welcome, Ada <Lovelace>\r\n"));
}

#[test]
fn a_hostile_subject_value_never_reaches_the_message_renderer() {
    let template = Template::compile("welcome", "Hi {{ name }}", None, "x").unwrap();
    let hostile = Value::map(vec![(
        "name".to_owned(),
        text("Ada\r\nBcc: victim@example.com"),
    )]);
    let error = template.render(&hostile).unwrap_err();
    assert_eq!(error.problems[0].reason, Reason::HeaderInjection);
    assert_eq!(MailError::from(&error).kind, Kind::Template);
}

#[test]
fn a_huge_value_in_a_loop_hits_the_output_cap_instead_of_running_away() {
    let template =
        Template::compile("t", "s", None, "{% for i in l %}{{ v }}{% endfor %}").unwrap();
    let value = "<".repeat(MAX_OUTPUT_BYTES / 8);
    let context = Value::map(vec![
        ("v".to_owned(), text(&value)),
        (
            "l".to_owned(),
            Value::List(vec![Value::Null; MAX_ITERATIONS]),
        ),
    ]);
    let error = template.render(&context).unwrap_err();
    assert_eq!(error.problems[0].reason, Reason::OutputTooLarge);
}

#[test]
fn deeply_nested_input_is_refused_by_the_compiler() {
    let html = format!(
        "{}{}",
        "{% if a %}".repeat(5000),
        "{% endif %}".repeat(5000)
    );
    let error = Template::compile("t", "s", None, &html).unwrap_err();
    assert_eq!(error.problems[0].reason, Reason::TooDeep);
}
