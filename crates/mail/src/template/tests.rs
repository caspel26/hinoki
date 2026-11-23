use super::limits::*;
use super::*;

fn s(text: &str) -> Value {
    Value::Str(text.to_owned())
}

fn map(entries: Vec<(&str, Value)>) -> Value {
    Value::map(
        entries
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}

fn compile(subject: &str, text: Option<&str>, html: &str) -> Template {
    Template::compile("t", subject, text, html).unwrap()
}

fn html_of(html: &str, context: &Value) -> Result<String, TemplateError> {
    compile("s", None, html).render(context).map(|r| r.html)
}

fn first(error: &TemplateError) -> &Problem {
    &error.problems[0]
}

#[test]
fn renders_the_three_parts_and_derives_text_when_absent() {
    let template = compile("Hi {{ name }}", None, "<p>Hello <b>{{ name }}</b></p>");
    let rendered = template.render(&map(vec![("name", s("Ada"))])).unwrap();
    assert_eq!(rendered.subject, "Hi Ada");
    assert_eq!(rendered.html, "<p>Hello <b>Ada</b></p>");
    assert_eq!(rendered.text, "Hello Ada");
    assert_eq!(template.name(), "t");
}

#[test]
fn an_explicit_text_part_is_rendered_as_it_is() {
    let template = compile("s", Some("a < {{ v }}\n"), "<p>{{ v }}</p>");
    let rendered = template.render(&map(vec![("v", s("<&>"))])).unwrap();
    assert_eq!(rendered.text, "a < <&>\n");
    assert_eq!(rendered.html, "<p>&lt;&amp;&gt;</p>");
}

#[test]
fn only_inserted_values_are_escaped_in_html() {
    let context = map(vec![("v", s(r#"<script>"x" & 'y'</script>"#))]);
    assert_eq!(
        html_of(r#"<a title="{{ v }}">{{ v }} & <i></a>"#, &context).unwrap(),
        "<a title=\"&lt;script&gt;&quot;x&quot; &amp; &#x27;y&#x27;&lt;/script&gt;\">\
         &lt;script&gt;&quot;x&quot; &amp; &#x27;y&#x27;&lt;/script&gt; & <i></a>"
    );
}

#[test]
fn the_subject_and_text_insert_values_unescaped() {
    let template = compile("{{ v }}", Some("{{ v }}"), "x");
    let rendered = template.render(&map(vec![("v", s("<&>"))])).unwrap();
    assert_eq!(
        (rendered.subject.as_str(), rendered.text.as_str()),
        ("<&>", "<&>")
    );
}

#[test]
fn scalars_are_written_in_their_documented_form() {
    let context = map(vec![
        ("n", Value::Int(-7)),
        ("f", Value::Float(1.0)),
        ("g", Value::Float(0.1)),
        ("t", Value::Bool(true)),
        ("z", Value::Null),
    ]);
    assert_eq!(
        html_of("{{ n }}|{{ f }}|{{ g }}|{{ t }}|{{ z }}|", &context).unwrap(),
        "-7|1.0|0.1|true||"
    );
}

#[test]
fn a_path_through_null_is_empty_and_a_missing_key_is_an_error() {
    let context = map(vec![
        ("user", Value::Null),
        ("a", map(vec![("b", Value::Null)])),
    ]);
    assert_eq!(
        html_of("[{{ user.name.first }}][{{ a.b.c }}]", &context).unwrap(),
        "[][]"
    );
    let error = html_of("x\n  {{ a.nope }}", &context).unwrap_err();
    let problem = first(&error);
    assert_eq!(problem.reason, Reason::MissingVariable);
    assert_eq!(problem.variable.as_deref(), Some("a.nope"));
    assert_eq!(problem.part, Some(Part::Html));
    assert_eq!(problem.at, Some(Pos { line: 2, column: 6 }));
    assert_eq!(
        error.to_string(),
        "template 't': missing variable 'a.nope' in html at line 2, column 6"
    );
    let error = html_of("{{ nothing }}", &context).unwrap_err();
    assert_eq!(first(&error).reason, Reason::MissingVariable);
}

#[test]
fn traversing_a_scalar_or_printing_a_container_is_an_error() {
    let context = map(vec![
        ("n", Value::Int(1)),
        ("l", Value::List(vec![])),
        ("m", map(vec![])),
    ]);
    for (src, reason) in [
        ("{{ n.x }}", Reason::NotAnObject),
        ("{{ l }}", Reason::NotText),
        ("{{ m }}", Reason::NotText),
        ("{% for i in n %}{% endfor %}", Reason::NotAList),
        ("{% for i in m %}{% endfor %}", Reason::NotAList),
        ("{{ l.x }}", Reason::NotAnObject),
    ] {
        let error = html_of(src, &context).unwrap_err();
        assert_eq!(first(&error).reason, reason, "{src}");
    }
}

#[test]
fn a_context_that_is_not_a_map_is_refused() {
    let error = compile("s", None, "x").render(&Value::Null).unwrap_err();
    assert_eq!(first(&error).reason, Reason::NotAnObject);
}

#[test]
fn if_takes_the_branch_by_truthiness() {
    let src = "{% if v %}yes{% else %}no{% endif %}";
    for (value, expected) in [
        (Value::Null, "no"),
        (Value::Bool(false), "no"),
        (Value::Int(0), "no"),
        (Value::Float(-0.0), "no"),
        (s(""), "no"),
        (Value::List(vec![]), "no"),
        (map(vec![]), "no"),
        (Value::Bool(true), "yes"),
        (Value::Int(2), "yes"),
        (s("0"), "yes"),
        (Value::List(vec![Value::Null]), "yes"),
    ] {
        assert_eq!(
            html_of(src, &map(vec![("v", value.clone())])).unwrap(),
            expected,
            "{value:?}"
        );
    }
    assert_eq!(
        html_of("{% if v %}yes{% endif %}", &map(vec![("v", Value::Null)])).unwrap(),
        ""
    );
    let error = html_of("{% if v %}{% endif %}", &map(vec![])).unwrap_err();
    assert_eq!(first(&error).reason, Reason::MissingVariable);
}

#[test]
fn for_walks_lists_and_treats_null_as_empty() {
    let items = Value::List(vec![s("a"), s("<b>"), s("c")]);
    let context = map(vec![("items", items), ("none", Value::Null)]);
    assert_eq!(
        html_of("{% for i in items %}[{{ i }}]{% endfor %}", &context).unwrap(),
        "[a][&lt;b&gt;][c]"
    );
    assert_eq!(
        html_of("{% for i in none %}x{% endfor %}", &context).unwrap(),
        ""
    );
}

#[test]
fn nested_loops_see_both_variables_and_objects_in_lists() {
    let rows = Value::List(vec![
        map(vec![
            ("n", s("r1")),
            ("cells", Value::List(vec![Value::Int(1), Value::Int(2)])),
        ]),
        map(vec![("n", s("r2")), ("cells", Value::List(vec![]))]),
    ]);
    let src = "{% for r in rows %}{{ r.n }}:{% for c in r.cells %}{{ c }}{{ r.n }},{% endfor %};{% endfor %}";
    assert_eq!(
        html_of(src, &map(vec![("rows", rows)])).unwrap(),
        "r1:1r1,2r1,;r2:;"
    );
}

#[test]
fn a_loop_variable_cannot_hide_a_field_of_the_context() {
    let context = map(vec![("x", Value::List(vec![Value::Int(1)]))]);
    let error = html_of("{% for x in x %}{% endfor %}", &context).unwrap_err();
    assert_eq!(first(&error).reason, Reason::ShadowedName);
    assert_eq!(first(&error).variable.as_deref(), Some("x"));
}

#[test]
fn loop_iterations_are_counted_across_loops_and_parts() {
    let items = Value::List(vec![Value::Null; MAX_ITERATIONS]);
    let context = map(vec![("items", items)]);
    let one = "{% for i in items %}{% endfor %}";
    assert!(html_of(one, &context).is_ok());
    let error = html_of(&format!("{one}{one}"), &context).unwrap_err();
    assert_eq!(first(&error).reason, Reason::TooManyIterations);
    let template = compile(
        "{% for i in items %}{% endfor %}",
        Some("{% for i in items %}{% endfor %}"),
        "x",
    );
    let error = template.render(&context).unwrap_err();
    assert_eq!(first(&error).reason, Reason::TooManyIterations);
    assert_eq!(first(&error).part, Some(Part::Text));
    let nested = Value::List(vec![Value::List(vec![Value::Null; 101]); 100]);
    let error = html_of(
        "{% for a in rows %}{% for b in a %}{% endfor %}{% endfor %}",
        &map(vec![("rows", nested)]),
    )
    .unwrap_err();
    assert_eq!(first(&error).reason, Reason::TooManyIterations);
}

#[test]
fn the_output_is_bounded_in_every_part() {
    let big = s(&"x".repeat(MAX_OUTPUT_BYTES / 2 + 1));
    let context = map(vec![("big", big)]);
    let error = html_of("{{ big }}{{ big }}", &context).unwrap_err();
    assert_eq!(first(&error).reason, Reason::OutputTooLarge);
    assert!(html_of("{{ big }}", &context).is_ok());
    let error = compile("s", Some("{{ big }}{{ big }}"), "x")
        .render(&context)
        .unwrap_err();
    assert_eq!(
        (first(&error).reason, first(&error).part),
        (Reason::OutputTooLarge, Some(Part::Text))
    );
    let quotes = map(vec![("q", s(&"\"".repeat(MAX_OUTPUT_BYTES / 2)))]);
    let error = html_of("{{ q }}", &quotes).unwrap_err();
    assert_eq!(first(&error).reason, Reason::OutputTooLarge);
    let literal = "a".repeat(MAX_BODY_SOURCE_BYTES - 100);
    let repeated = format!("{{% for i in l %}}{literal}{{% endfor %}}");
    let list = map(vec![("l", Value::List(vec![Value::Null; 3]))]);
    assert!(html_of(&repeated, &list).is_ok());
    let list = map(vec![("l", Value::List(vec![Value::Null; 5000]))]);
    let error = html_of(&repeated, &list).unwrap_err();
    assert_eq!(first(&error).reason, Reason::OutputTooLarge);
    assert_eq!(first(&error).part, Some(Part::Html));
}

#[test]
fn the_subject_is_limited_to_a_header_line() {
    let context = map(vec![("v", s(&"x".repeat(MAX_SUBJECT_BYTES)))]);
    assert!(compile("{{ v }}", None, "x").render(&context).is_ok());
    let error = compile("a{{ v }}", None, "x").render(&context).unwrap_err();
    assert_eq!(
        (first(&error).reason, first(&error).part),
        (Reason::OutputTooLarge, Some(Part::Subject))
    );
}

#[test]
fn a_value_with_a_line_break_cannot_enter_the_subject() {
    for bad in [
        "a\r\nBcc: x@example.com",
        "a\nb",
        "a\rb",
        "a\0b",
        "a\x07b",
        "a\x7fb",
        "a\u{85}b",
        "a\u{2028}b",
        "a\u{2029}b",
    ] {
        let error = compile("Hi {{ v }}", None, "x")
            .render(&map(vec![("v", s(bad))]))
            .unwrap_err();
        let problem = first(&error);
        assert_eq!(problem.reason, Reason::HeaderInjection, "{bad:?}");
        assert_eq!(problem.part, Some(Part::Subject));
        assert!(
            !error.to_string().contains("Bcc"),
            "the value is never quoted"
        );
    }
    let rendered = compile("Hi {{ v }}", None, "x")
        .render(&map(vec![("v", s("a\tb"))]))
        .unwrap();
    assert_eq!(rendered.subject, "Hi a\tb");
}

#[test]
fn a_unicode_line_separator_in_a_subject_source_is_refused() {
    for bad in ["a\u{85}b", "a\u{2028}b", "a\u{2029}b"] {
        let error = Template::compile("t", bad, None, "x").unwrap_err();
        assert_eq!(first(&error).reason, Reason::ControlCharacter, "{bad:?}");
    }
}

#[test]
fn a_value_in_an_address_attribute_needs_a_safe_scheme() {
    let template = compile("s", None, r#"<a href="{{ u }}">x</a>"#);
    let rendered = |u: &str| template.render(&map(vec![("u", s(u))]));
    assert_eq!(
        rendered("https://example.com/?a=1&b=2").unwrap().html,
        r#"<a href="https://example.com/?a=1&amp;b=2">x</a>"#
    );
    assert_eq!(
        rendered("/relative").unwrap().html,
        r#"<a href="/relative">x</a>"#
    );
    for bad in [
        "javascript:alert(1)",
        " JavaScript:x",
        "data:text/html,x",
        "java\nscript:x",
    ] {
        let error = rendered(bad).unwrap_err();
        assert_eq!(first(&error).reason, Reason::UnsafeUrl, "{bad:?}");
        assert_eq!(first(&error).variable.as_deref(), Some("u"));
        assert!(!error.to_string().contains("alert"));
    }
}

#[test]
fn a_value_where_escaping_is_not_enough_is_refused_when_compiling() {
    for html in [
        "<a title={{ v }}>",
        "<script>var a = {{ v }};</script>",
        "<style>p { color: {{ v }} }</style>",
        "<a onclick=\"{{ v }}\">",
    ] {
        let error = Template::compile("t", "s", None, html).unwrap_err();
        let problem = first(&error);
        assert_eq!(problem.reason, Reason::UnsafeContext, "{html}");
        assert_eq!(problem.variable.as_deref(), Some("v"));
        assert_eq!(problem.part, Some(Part::Html));
    }
    Template::compile("t", "{{ v }}", Some("<a href={{ v }}>"), "<p>{{ v }}</p>").unwrap();
}

#[test]
fn line_breaks_are_fine_in_the_text_and_html_but_nul_is_not() {
    let context = map(vec![("v", s("a\nb"))]);
    let rendered = compile("s", Some("{{ v }}"), "{{ v }}")
        .render(&context)
        .unwrap();
    assert_eq!(
        (rendered.text.as_str(), rendered.html.as_str()),
        ("a\nb", "a\nb")
    );
    let nul = map(vec![("v", s("a\0b"))]);
    let error = html_of("{{ v }}", &nul).unwrap_err();
    assert_eq!(first(&error).reason, Reason::ControlCharacter);
    let error = compile("s", Some("{{ v }}"), "x").render(&nul).unwrap_err();
    assert_eq!(
        (first(&error).reason, first(&error).part),
        (Reason::ControlCharacter, Some(Part::Text))
    );
}

#[test]
fn errors_never_contain_values_of_the_context() {
    let secret = "hunter2-sentinel";
    let context = map(vec![("v", s(secret)), ("n", Value::Int(424242))]);
    for src in [
        "{{ v.x }}",
        "{{ missing }}",
        "{% for i in v %}{% endfor %}",
        "{{ n.x }}",
    ] {
        let text = html_of(src, &context).unwrap_err().to_string();
        assert!(!text.contains(secret) && !text.contains("424242"), "{text}");
    }
    let text = compile("{{ v }}", None, "x")
        .render(&map(vec![("v", s(&format!("{secret}\n")))]))
        .unwrap_err()
        .to_string();
    assert!(!text.contains(secret), "{text}");
}

#[test]
fn compile_checks_the_name_and_the_sizes() {
    for name in ["", "Welcome", "-a", "_a", "a b", "é", &"a".repeat(65)] {
        let error = Template::compile(name, "s", None, "x").unwrap_err();
        assert_eq!(first(&error).reason, Reason::InvalidName, "{name:?}");
        assert!(error.template.len() <= MAX_NAME_BYTES);
    }
    assert_eq!(
        Template::compile("a\nb", "s", None, "x")
            .unwrap_err()
            .template,
        "a?b"
    );
    for name in ["a", "0", "reset-password_2", &"a".repeat(64)] {
        assert!(Template::compile(name, "s", None, "x").is_ok(), "{name}");
    }
    let too = |subject: &str, text: Option<&str>, html: &str| {
        let error = Template::compile("t", subject, text, html).unwrap_err();
        (first(&error).reason, first(&error).part)
    };
    let body = "a".repeat(MAX_BODY_SOURCE_BYTES);
    assert!(Template::compile(
        "t",
        &"a".repeat(MAX_SUBJECT_SOURCE_BYTES),
        Some(&body),
        &body
    )
    .is_ok());
    assert_eq!(
        too(&"a".repeat(MAX_SUBJECT_SOURCE_BYTES + 1), None, "x"),
        (Reason::TooLarge, Some(Part::Subject))
    );
    assert_eq!(
        too("s", Some(&format!("{body}a")), "x"),
        (Reason::TooLarge, Some(Part::Text))
    );
    assert_eq!(
        too("s", None, &format!("{body}a")),
        (Reason::TooLarge, Some(Part::Html))
    );
}

#[test]
fn compile_reports_the_part_and_position_of_a_syntax_error() {
    let error = Template::compile("t", "ok", Some("a\n{{ x | y }}"), "x").unwrap_err();
    assert_eq!(
        (first(&error).reason, first(&error).part, first(&error).at),
        (
            Reason::Syntax,
            Some(Part::Text),
            Some(Pos { line: 2, column: 4 })
        )
    );
    let error = Template::compile("t", "a\nb", None, "x").unwrap_err();
    assert_eq!(first(&error).reason, Reason::ControlCharacter);
    let error = Template::compile("t", "s", None, "{% if a %}").unwrap_err();
    assert_eq!(first(&error).reason, Reason::UnclosedBlock);
}

#[test]
fn the_node_limit_is_shared_by_the_three_parts() {
    let third = "{{ a }}".repeat(MAX_NODES / 3);
    let error = Template::compile(
        "t",
        &"{{ a }}".repeat(100),
        Some(&third),
        &"{{ a }}".repeat(MAX_NODES),
    )
    .unwrap_err();
    assert_eq!(first(&error).reason, Reason::TooManyNodes);
}

#[test]
fn a_deeply_nested_template_is_refused_not_recursed() {
    let src = format!(
        "{}x{}",
        "{% if a %}".repeat(10_000),
        "{% endif %}".repeat(10_000)
    );
    let error = Template::compile("t", "s", None, &src).unwrap_err();
    assert_eq!(first(&error).reason, Reason::TooDeep);
}

#[test]
fn standalone_block_lines_leave_no_blank_lines_in_the_output() {
    let src = "<ul>\n{% for i in l %}\n  <li>{{ i }}</li>\n{% endfor %}\n</ul>\n";
    let context = map(vec![("l", Value::List(vec![Value::Int(1), Value::Int(2)]))]);
    assert_eq!(
        html_of(src, &context).unwrap(),
        "<ul>\n  <li>1</li>\n  <li>2</li>\n</ul>\n"
    );
}

#[test]
fn large_maps_are_found_through_the_index() {
    let entries: Vec<_> = (0..50).map(|i| (format!("k{i}"), Value::Int(i))).collect();
    let context = Value::map(entries);
    assert_eq!(html_of("{{ k0 }} {{ k49 }}", &context).unwrap(), "0 49");
}

mod verification {
    use super::*;

    fn object(fields: Vec<(&str, Shape)>) -> Shape {
        Shape::Object(fields.into_iter().map(|(k, v)| (k.to_owned(), v)).collect())
    }

    fn list(inner: Shape) -> Shape {
        Shape::List(Box::new(inner))
    }

    fn welcome() -> Shape {
        object(vec![
            (
                "user",
                object(vec![("name", Shape::Scalar), ("trial", Shape::Scalar)]),
            ),
            (
                "steps",
                list(object(vec![
                    ("title", Shape::Scalar),
                    ("url", Shape::Scalar),
                ])),
            ),
            ("tags", list(Shape::Scalar)),
        ])
    }

    const FULL: &str = "{{ user.name }}{% if user.trial %}{% endif %}\
        {% for s in steps %}{{ s.title }}{{ s.url }}{% endfor %}{% for t in tags %}{{ t }}{% endfor %}";

    fn problems(
        subject: &str,
        text: Option<&str>,
        html: &str,
        shape: &Shape,
    ) -> Vec<(Reason, Option<Part>, Option<String>)> {
        match Template::compile("t", subject, text, html)
            .unwrap()
            .verify(shape)
        {
            Ok(()) => vec![],
            Err(error) => error
                .problems
                .iter()
                .map(|p| (p.reason, p.part, p.variable.clone()))
                .collect(),
        }
    }

    fn named(
        reason: Reason,
        part: Option<Part>,
        variable: &str,
    ) -> (Reason, Option<Part>, Option<String>) {
        (reason, part, Some(variable.to_owned()))
    }

    #[test]
    fn a_template_that_uses_every_field_is_accepted() {
        assert!(problems("s", None, FULL, &welcome()).is_empty());
    }

    #[test]
    fn fields_used_in_any_part_count_as_used() {
        let html = "{{ user.name }}{% for s in steps %}{{ s.title }}{% endfor %}";
        let text =
            Some("{% for s in steps %}{{ s.url }}{% endfor %}{% for t in tags %}{% endfor %}");
        assert_eq!(
            problems("{% if user.trial %}{% endif %}", text, html, &welcome()),
            vec![]
        );
    }

    #[test]
    fn unknown_variables_are_reported_in_every_place_with_their_part() {
        let found = problems(
            "{{ nope }}",
            Some("{{ user.nick }}"),
            "{% if user.name.x %}{% endif %}{% for s in nope %}{{ s }}{% endfor %}",
            &welcome(),
        );
        assert_eq!(
            found[0],
            named(Reason::UnknownVariable, Some(Part::Subject), "nope")
        );
        assert_eq!(
            found[1],
            named(Reason::UnknownVariable, Some(Part::Text), "user.nick")
        );
        assert_eq!(
            found[2],
            named(Reason::NotAnObject, Some(Part::Html), "user.name.x")
        );
        assert_eq!(
            found[3],
            named(Reason::UnknownVariable, Some(Part::Html), "nope")
        );
    }

    #[test]
    fn printing_an_object_or_list_and_looping_over_a_scalar_are_reported() {
        let found = problems(
            "s",
            None,
            "{{ user }}{{ steps }}{{ tags }}{% for x in user.name %}{% endfor %}{% for y in user %}{% endfor %}",
            &welcome(),
        );
        let reasons: Vec<_> = found.iter().take(5).map(|p| p.0).collect();
        assert_eq!(
            reasons,
            [
                Reason::NotText,
                Reason::NotText,
                Reason::NotText,
                Reason::NotAList,
                Reason::NotAList
            ]
        );
    }

    #[test]
    fn unused_leaves_are_reported_by_schema_path_without_a_position() {
        let found = problems(
            "s",
            None,
            "{{ user.name }}{% for s in steps %}{{ s.title }}{% endfor %}",
            &welcome(),
        );
        assert_eq!(
            found,
            vec![
                named(Reason::UnusedField, None, "user.trial"),
                named(Reason::UnusedField, None, "steps[].url"),
                named(Reason::UnusedField, None, "tags"),
            ]
        );
    }

    #[test]
    fn a_loop_variable_cannot_hide_a_field() {
        let shape = object(vec![("steps", list(Shape::Scalar)), ("s", Shape::Scalar)]);
        let found = problems(
            "{{ s }}",
            None,
            "{% for s in steps %}{{ s }}{% endfor %}",
            &shape,
        );
        assert_eq!(found[0], named(Reason::ShadowedName, Some(Part::Html), "s"));
    }

    #[test]
    fn refused_fields_are_reported_even_when_unused_and_secrets_by_their_own_reason() {
        let shape = object(vec![
            ("token", Shape::Refused(Refusal::Secret)),
            ("raw", Shape::Refused(Refusal::Bytes)),
            ("meta", Shape::Refused(Refusal::Mapping)),
            (
                "inner",
                object(vec![
                    ("any", Shape::Refused(Refusal::Untyped)),
                    ("ok", Shape::Scalar),
                ]),
            ),
            ("many", list(Shape::Refused(Refusal::Union))),
            ("pair", Shape::Refused(Refusal::Tuple)),
        ]);
        let found = problems("{{ inner.ok }}", None, "x", &shape);
        assert_eq!(
            found,
            vec![
                named(Reason::SecretField, None, "token"),
                named(Reason::UnsupportedField, None, "raw"),
                named(Reason::UnsupportedField, None, "meta"),
                named(Reason::UnsupportedField, None, "inner.any"),
                named(Reason::UnsupportedField, None, "many[]"),
                named(Reason::UnsupportedField, None, "pair"),
            ]
        );
        assert_eq!(found.len(), 6);
    }

    #[test]
    fn using_a_refused_field_adds_no_noise() {
        let shape = object(vec![
            ("meta", Shape::Refused(Refusal::Mapping)),
            ("l", list(Shape::Refused(Refusal::Bytes))),
        ]);
        let found = problems("s", None, "{{ meta.x }}{{ meta }}{% for i in meta %}{{ i.y }}{% endfor %}{% for i in l %}{{ i }}{% endfor %}", &shape);
        assert_eq!(found.len(), 2, "{found:?}");
    }

    #[test]
    fn problems_are_capped_and_flagged() {
        let src = "{{ nope }}".repeat(MAX_PROBLEMS + 5);
        let error = Template::compile("t", "s", None, &src)
            .unwrap()
            .verify(&object(vec![]))
            .unwrap_err();
        assert_eq!(error.problems.len(), MAX_PROBLEMS);
        assert!(error.truncated);
    }

    #[test]
    fn the_shape_must_be_an_object_and_not_too_deep() {
        let error = Template::compile("t", "s", None, "x")
            .unwrap()
            .verify(&Shape::Scalar)
            .unwrap_err();
        assert_eq!(first(&error).reason, Reason::UnsupportedField);
        let mut deep = Shape::Scalar;
        for _ in 0..MAX_CONTEXT_DEPTH + 1 {
            deep = object(vec![("a", deep)]);
        }
        let error = Template::compile("t", "s", None, "x")
            .unwrap()
            .verify(&deep)
            .unwrap_err();
        assert_eq!(first(&error).reason, Reason::TooDeep);
    }

    #[test]
    fn nested_lists_of_objects_and_paths_in_loops_resolve() {
        let shape = object(vec![(
            "rows",
            list(list(object(vec![("v", Shape::Scalar)]))),
        )]);
        let html = "{% for r in rows %}{% for c in r %}{{ c.v }}{% endfor %}{% endfor %}";
        assert!(problems("s", None, html, &shape).is_empty());
        let unused = problems("s", None, "{% for r in rows %}{% endfor %}", &shape);
        assert_eq!(unused, vec![named(Reason::UnusedField, None, "rows[][].v")]);
    }

    #[test]
    fn refusals_parse_by_name() {
        for (name, refusal) in [
            ("secret", Refusal::Secret),
            ("bytes", Refusal::Bytes),
            ("untyped", Refusal::Untyped),
            ("mapping", Refusal::Mapping),
            ("tuple", Refusal::Tuple),
            ("union", Refusal::Union),
        ] {
            assert_eq!(Refusal::parse(name), Some(refusal));
        }
        assert_eq!(Refusal::parse("other"), None);
    }
}
