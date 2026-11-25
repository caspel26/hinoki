# Email templates

A template turns a typed context into the subject, the plain text and the HTML of a message. The language is small and fixed, the template is parsed, checked against the context `Schema` and rendered in Rust (`crates/mail`), and the result is an ordinary `mail.Message` for [`mail.send`](https://hinoki.caspel.dev/dev/email.md). The call from Python to the renderer is one synchronous native call per message, with Python objects for the context and the three rendered strings.

The code on this page is the code of [`examples/mailroom`](https://github.com/caspel26/hinoki/tree/main/examples/mailroom); a test (`tests/mailtemplatesdoctest.py`) proves that every quoted block is a piece of that example and that every printed output is what the command prints now. Use only synthetic addresses such as `example.com`; nothing here sends real mail.

## A template file

A template file is TOML with a `subject`, a `template` (the HTML) and, optionally, a `text` and a `description`. The context it reads is a `Schema`:

```python
# mailroom/messages.py
class Account(Schema):
    name: Annotated[str, Field(max_length=80, examples=("Ada",))]
    trial_days: Annotated[int, Field(ge=0, examples=(14,))]


class Step(Schema):
    title: Annotated[str, Field(max_length=80, examples=("Create a project",))]
    url: Annotated[str, Field(format="url", examples=("https://example.com/start",))]


class Welcome(Schema):
    user: Account
    steps: Annotated[list[Step], Field(max_length=10)]
```

```toml
# mailroom/templates/welcome.toml
description = "Sent once, after sign-up"
subject = "Welcome to Mailroom, {{ user.name }}"
template = '''
<html><body>
<h1>Welcome, {{ user.name }}</h1>
{% if user.trial_days %}
<p>Your trial lasts {{ user.trial_days }} days.</p>
{% else %}
<p>Your plan is active.</p>
{% endif %}
<ol>
{% for step in steps %}
<li><a href="{{ step.url }}">{{ step.title }}</a></li>
{% endfor %}
</ol>
</body></html>
'''
```

`mail.Template.file(path, context=Welcome)` names the file and its context. Nothing is read when the module is imported; the name defaults to the file's stem (`welcome`):

```python
# mailroom/messages.py
WELCOME = mail.Template.file(HERE / "welcome.toml", context=Welcome)
RESET_PASSWORD = mail.Template.file(HERE / "reset_password.toml", context=ResetPassword)

templates: list[mail.TemplateBase] = [WELCOME, RESET_PASSWORD]
```

A template built in code takes the same parts: `mail.Template(name="x", subject=..., html=..., text=..., context=Ctx)`, or `mail.Template.from_toml(source, name="x", context=Ctx)` for TOML text. Those compile when they are built, with no I/O.

## Register and send

List the templates when you create the application. Nothing registers by import:

```python
# mailroom/main.py
def build_app(settings: MailroomSettings) -> Hinoki:
    """The application; the routes need the API key and the public base URL."""
    return Hinoki(
        settings=settings,
        title="Mailroom",
        include=build_routes(settings),
        email_templates=templates,
    )
```

The auto-installed `EmailModule` receives the list. If you declare the module yourself, pass `EmailModule(settings, templates=[...])`. Every worker reads and checks the files in its lifespan, and the worker does not start when one fails. At most 256 templates are registered, with distinct names.

A route builds the context, renders it with `Template.message(context, to=...)` and sends the message:

```python
# mailroom/routes.py
    @group.post("/signup", status=202)
    async def sign_up(data: SignUp) -> Sent:
        """Send the welcome message of a new account."""
        welcome = Welcome(
            user=Account(name=data.name, trial_days=data.trial_days),
            steps=[Step(title="Create a project", url=START)],
        )
        sent = await mail.send(WELCOME.message(welcome, to=(data.email,)))
        return Sent(message_id=sent.message_id)
```

`message` takes the other `Message` options (`cc`, `bcc`, `reply_to`, `headers`, `attachments`, `sender`) as keywords. A test sends through the `memory` backend, which keeps the message in the mailer's outbox:

```python
# mailroom_tests/mailroomsendtest.py
def messages() -> list[mail.Message]:
    module = next(m for m in app.modules if isinstance(m, mail.EmailModule))
    return [sent.message for sent in module.mailer.outbox if sent.message]


async def test_sign_up_sends_the_welcome_message(client: ASGIClient) -> None:
    response = await client.post(
        "/signup", json={"email": "ada@example.com", "name": "Ada <1>"}, headers=KEY
    )

    assert response.status == 202
    (message,) = messages()
    assert message.to == ("ada@example.com",)
    assert message.subject == "Welcome to Mailroom, Ada <1>"
    assert message.text is not None
    assert "Your trial lasts 14 days." in message.text
    assert message.html is not None
    assert "<h1>Welcome, Ada &lt;1&gt;</h1>" in message.html
```

To see a message printed while developing, run `MAILROOM_API_KEY=dev-key MAILROOM_EMAIL__BACKEND=console MAILROOM_EMAIL__DEFAULT_SENDER=noreply@example.com uv run hinoki dev` from `examples/mailroom` and `POST` to `/signup` with the header `x-api-key: dev-key`. The console output is not reproduced here because its `Date` and `Message-ID` headers change on every run.

## The language

A template is text with three constructs:

| Construct | Meaning |
| --- | --- |
| `{{ path }}` | Insert a value. `path` is one to eight identifiers joined by dots (`user.name`) |
| `{% if path %}` … `{% else %}` … `{% endif %}` | Render a branch by truthiness; `else` is optional |
| `{% for name in path %}` … `{% endfor %}` | Render the body once per item of a list; `name` exists only inside the body |

An identifier is ASCII letters, digits and `_`, not starting with a digit, up to 64 bytes. `if`, `else`, `endif`, `for`, `in` and `endfor` are reserved as loop variables. A loop variable may not have the name of a field of the context or of an enclosing loop (`shadowed_name`). There are no filters, expressions, comparisons, `not`, `elif`, comments, includes, macros or inheritance: anything else inside `{{ }}` or `{% %}` is a syntax error. A literal `{{` or `{%` cannot appear in the text; a lone `{`, `}` or `%` is text, so CSS works.

**Standalone lines.** A `{% … %}` tag that is the only non-blank content of its line removes that line (its leading and trailing spaces and tabs, and one line ending). `{{ }}` lines are never removed. This is why the preview below has no blank lines where the `if` and `for` tags are.

**Values.** The context is read as these values:

| Python / `Schema` field | In the template |
| --- | --- |
| `str`, `Email`, `Url`, `StrEnum` | Text as is |
| `int`, `float` | Decimal digits (an `int` of any size); a float with no fractional part gets `.0` (`1.0`), never an exponent |
| `bool` | `true` or `false` |
| `None` | Nothing (empty text); a path through a `None` object is also empty |
| `Decimal`, `date`, `datetime`, `UUID` | The text `dump()` gives |
| nested `Schema` | An object: use `user.name` |
| `list[...]` | Something `for` iterates; `for` over `None` renders nothing |
| `Secret`, `bytes`, `Any`, `tuple`, a mapping field | Refused when the template is checked |

An object or a list cannot be inserted with `{{ }}` (`not_text`); a `for` over a value that is not a list is `not_a_list`; a path through a non-object is `not_an_object`.

**Truthiness.** `None`, `false`, `0`, `0.0`, empty text, an empty list and an empty object are false; everything else is true.

**Escaping.** In `html` every inserted value is escaped (`&` `<` `>` `"` `'` become `&amp;`, `&lt;`, `&gt;`, `&quot;`, `&#x27;`); the literal text of the template is not touched. `subject` and `text` insert values as they are. A value inserted into the subject that has a line break, a NUL or another control character other than a tab (including `U+0085`, `U+2028` and `U+2029`) is refused (`header_injection`), so a value cannot add a header. A NUL in a value inserted into `text` or `html` is refused (`control_character`).

Escaping is enough in text and inside a quoted attribute, so the template is also checked for where each `{{ }}` sits in the HTML. A `{{ }}` inside `<script>` or `<style>`, in an unquoted attribute value, in a tag or attribute name, or in an event handler attribute (`onclick`, ...) is refused when the template is compiled (`unsafe_context`). A value inside a quoted `href`, `src`, `action`, `formaction`, `background` or `poster` is checked when the template is rendered: with a scheme it must be `http`, `https`, `mailto` or `tel`, otherwise the render fails (`unsafe_url`); an address without a scheme is relative and passes. The check also covers the dict shortcut. A `Schema` context is not allowed `Unset` fields (`str | Unset`), a `dump()` override, or a computed field that returns a secret: the first two are `unsupported_field` when the template is built, the last is `secret_value` when it is rendered.

**Limits.** These are fixed in `crates/mail/src/template/limits.rs`; they are not settings:

| What | Limit | Reason |
| --- | --- | --- |
| Subject source | 1 KiB | `too_large` |
| Text or HTML source, each | 256 KiB | `too_large` |
| TOML file | 512 KiB | `too_large` |
| Nodes in a template | 10 000 | `too_many_nodes` |
| Nesting of `if` and `for` | 16 | `too_deep` |
| Path segments, identifier | 8, 64 bytes | `invalid_path` |
| Rendered subject | 998 bytes | `output_too_large` |
| Rendered text or HTML, each | 1 MiB | `output_too_large` |
| Loop iterations in one render | 10 000 | `too_many_iterations` |
| Context depth, nodes, bytes | 16, 100 000, 4 MiB | `context_too_large` |
| Name | `[a-z0-9][a-z0-9_-]{0,63}` | `invalid_name` |
| Problems reported at once | 20 | |

The send path still applies `max_message_bytes` to the whole message.

## Text from HTML

When a template has no `text`, the plain text is derived from the rendered HTML by a single scanner in Rust, not by an HTML parser. Comments and the content of `head`, `style`, `script`, `title` and `template` are dropped; `br` is a line break; paragraphs, headings, lists, tables, blocks and `hr` make paragraph breaks; a list item starts with `- `; runs of white space collapse (except in `pre`); the entities `&amp; &lt; &gt; &quot; &apos; &#39; &nbsp;` and numeric ones are decoded; every other tag is removed. A link whose `href` starts with `http://`, `https://` or `mailto:` becomes `label (URL)`, or only the label when the label is the URL.

The `reset_password` template has no `text`. Printed from the sample context in `examples/mailroom` (`--part text` prints only that part):

```sh
uv run hinoki mail preview reset_password --context mailroom/samples/reset_password.json --part text
```

<!-- output: reset-text -->
```text
Hello Ada,

Someone asked to reset your password from Lisbon.

Choose a new password (https://example.com/reset?token=sample)

You are signed in on:

- Laptop
- Phone <work>

If it was not you, ignore this message.
```

The text part holds the value `Phone <work>` as it is; the HTML part holds `Phone &lt;work&gt;`. Write a `text` in the file when the derived text is not what you want.

## Checked at startup

A template is checked against its context `Schema` before any message is rendered: every variable must exist in the `Schema`, every `Schema` field must be used by some part, and a variable must have the right shape (a loop over a list, a path through an object, text where text is inserted). Change `{{ user.name }}` in the `<h1>` of `welcome.toml` to `{{ user.nickname }}` and the worker does not start; the error names the template, the variable and the position in the template, never a value:

<!-- output: unknown-variable -->
```text
template 'welcome': unknown variable 'user.nickname' in html at line 2, column 17
```

Removing the `<ol>` list from the HTML leaves the `steps` fields unused, which is also an error:

<!-- output: unused-field -->
```text
template 'welcome': unused field 'steps[].title' (and 1 more problem)
```

`hinoki mail check [TARGET]` runs the same check without starting anything. It reads every registered template, prints one line for each, and exits `1` when one is refused (`2` when the application cannot be loaded or has no email module). With the first change above:

<!-- output: mail-check -->
```text
template 'welcome': unknown variable 'user.nickname' in html at line 2, column 17
ok  reset_password  (mailroom/templates/reset_password.toml)
```

The rule `HNK-MAIL-001` (`from hinoki.rules import EmailTemplatesMatch`) makes the same check part of `hinoki check`. It is opt-in: put `EmailTemplatesMatch()` in your `RuleSet`. It reads the template files, so it declares the effect `files` and needs `--allow-effects`:

```sh
uv run hinoki check --rules myproject.rules:rules --allow-effects
```

## Preview

`hinoki mail preview NAME [TARGET]` renders a registered template from the command line, with no server and no sending. Without `--context` it builds a sample from the `examples=` of the context fields (a field that is not required and has no example is left out). Run it from `examples/mailroom` with the `memory` backend and a sender set:

```sh
export MAILROOM_API_KEY=dev-key
export MAILROOM_EMAIL__BACKEND=memory
export MAILROOM_EMAIL__DEFAULT_SENDER=noreply@example.com
uv run hinoki mail check
```

<!-- output: mail-check-ok -->
```text
ok  welcome  (mailroom/templates/welcome.toml)
ok  reset_password  (mailroom/templates/reset_password.toml)
```

```sh
uv run hinoki mail preview welcome
```

<!-- output: welcome-preview -->
```text
Subject: Welcome to Mailroom, Ada
----- text -----
Welcome, Ada

Your trial lasts 14 days.

- Create a project (https://example.com/start)
----- html -----
<html><body>
<h1>Welcome, Ada</h1>
<p>Your trial lasts 14 days.</p>
<ol>
<li><a href="https://example.com/start">Create a project</a></li>
</ol>
</body></html>
```

`--context FILE` (or `-` for standard input, at most 1 MiB) reads a JSON object that is validated as the context `Schema`; `--part subject|text|html` prints one part; `--format json` prints the three parts as JSON. The preview opens no mailer and sends nothing.

## Templates from strings

Some templates live in a database table, not in a file. `examples/mailroom/mailroom/notices.py` builds one from strings, without a `Schema`:

```python
# mailroom/notices.py
def notice_template(slug: str) -> mail.Template[mail.Context]:
    """The template of a stored notice; ``KeyError`` when there is none."""
    subject, html = NOTICES[slug]
    return mail.Template(name=f"notice-{slug}", subject=subject, html=html)
```

The context is a plain `dict` (`mail.Context`). Only the syntax is checked when the template is built; there is no `Schema` to check the variables against, so a missing key is found when the template is rendered, for the message being sent. A `Secret` in the `dict` is refused (`secret_value`). The error names the variable of the template:

<!-- output: missing-variable -->
```text
template 'notice-maintenance': missing variable 'from' in html at line 1, column 47
```

The `/notices/{slug}` route turns that error into `400 invalid_notice`, so the caller learns that the notice could not be rendered without seeing a value.

## Errors

Every failure is a `mail.TemplateError`, a subclass of `EmailError` with `kind` `template`. It carries `reason`, `template` (the name), `field` (the part), `line`, `column`, `variable` and `problems` (a tuple of `TemplateProblem`, at most 20; `truncated` says there were more). Its text never holds a recipient, a subject, a rendered text or a value of the context. A secret never reaches the engine: a `Secret` field in a `Schema` context is `secret_field`, and one in a `dict` is `secret_value`.

| Reason | When |
| --- | --- |
| `syntax`, `unclosed_tag`, `unexpected_tag`, `unclosed_block`, `invalid_path`, `shadowed_name`, `control_character`, `too_large`, `too_many_nodes`, `too_deep`, `invalid_name`, `unsafe_context` | The template does not compile |
| `unknown_variable`, `unused_field`, `not_text`, `not_a_list`, `not_an_object`, `secret_field`, `unsupported_field` | The template does not match its context `Schema` |
| `missing_variable`, `header_injection`, `output_too_large`, `too_many_iterations`, `context_too_large`, `unsupported_value`, `secret_value`, `unsafe_url` | A render fails |
| `unreadable`, `invalid_file` | The file cannot be read or is not a valid template TOML |

With metrics on [Kiso](https://hinoki.caspel.dev/dev/kiso.md), a `Template.message` that cannot be rendered while a mailer is current is counted in `hinoki_email_messages_total{outcome="template"}` (`Template.render` alone is not a send and counts nothing). Compile and startup failures are not sends and are not counted. See [export Prometheus metrics](https://hinoki.caspel.dev/dev/metrics.md#email).

## What templates do not do

- No filters, expressions, includes, macros or inheritance, and no comments.
- No translation: a template is one language; choose the template or write one per language.
- No inline images or attachment templates; `attachments` are the `Message` option.
- No registration in `hinoki.toml`: templates are listed in `Hinoki(email_templates=[...])` or `EmailModule(templates=[...])`.
- The limits above are fixed and are not settings.
- Rendering does not send: delivery, retries and the lack of a queue are those of [`mail.send`](https://hinoki.caspel.dev/dev/email.md).
