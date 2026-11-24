# Email API

- `hinoki.mail.Message(...)`: One email. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.Message)

- `hinoki.mail.Address(email: 'str', name: 'str | None' = None) -> None`: A mailbox: a bare addr-spec and an optional display name. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.Address)

- `hinoki.mail.Attachment(filename: 'str', content: 'bytes', content_type: 'str' = 'application/octet-stream') -> None`: A file attached to a message; ``content_type`` is ``type/subtype``. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.Attachment)

- `hinoki.mail.SentMail(message_id: 'str', recipients: 'tuple[str, ...]', attempts: 'int', raw: 'bytes | None' = None, message: 'Message | None' = None) -> None`: What ``send`` returns. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.SentMail)

- `hinoki.mail.Mailer(settings: 'EmailSettings', templates: 'Sequence[TemplateBase]' = ()) -> 'None'`: The email resource of one worker: it owns the connection pool of the ``smtp`` backend, so create it inside a running worker (``EmailModule`` does) and never…. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.Mailer)

- `hinoki.mail.EmailModule(settings: 'EmailSettings', templates: 'Sequence[TemplateBase]' = ()) -> 'None'`: Opens a ``Mailer`` in each worker's lifespan and provides it (``ctx.get(Mailer)``); installed automatically by an ``email: EmailSettings`` field. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.EmailModule)

- `hinoki.mail.EmailError(...)`: A message was refused or could not be delivered. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.EmailError)

- `hinoki.mail.EmailErrorKind(*values)`: Enum where members are also (and must be) strings. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.EmailErrorKind)

- `hinoki.mail.Template(*, name: 'str', subject: 'str', html: 'str', text: 'str | None' = None, context: 'type[Schema] | None' = None, description: 'str | None' = None) -> 'None'`: An email template: a subject, an HTML body and optionally a text body, written in the small template language of ``docs/email-templates.md``. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.Template)

- `hinoki.mail.TemplateBase(*, name: 'str', description: 'str | None', context_type: 'type[Schema] | None', origin: 'core.MailTemplate | Path') -> 'None'`: What every template is, whatever its context type: the name registries key on, a description, the file it is read from (if any) and the context ``Schema`` (if…. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.TemplateBase)

- `hinoki.mail.TemplateError(*, template: 'str', problems: 'Sequence[TemplateProblem]', truncated: 'bool' = False) -> 'None'`: A template is invalid or cannot be rendered. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.TemplateError)

- `hinoki.mail.TemplateReason(*values)`: Why a template was refused: the names the engine reports. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.TemplateReason)

- `hinoki.mail.TemplateProblem(reason: 'TemplateReason', part: 'str | None' = None, line: 'int | None' = None, column: 'int | None' = None, variable: 'str | None' = None) -> None`: One thing wrong with a template. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.TemplateProblem)

- `hinoki.mail.RenderedTemplate(subject: 'str', text: 'str', html: 'str') -> None`: The three parts of a rendered template. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.RenderedTemplate)

- `hinoki.mail.Context`: Type alias. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.Context)

- `hinoki.mail.Value`: Type alias. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.Value)

- `hinoki.mail.send(message: 'Message') -> 'SentMail'`: Send ``message`` through the current worker's mailer. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.send)

- `hinoki.mail.use(mailer: 'Mailer | None') -> 'Iterator[Mailer | None]'`: Make ``mailer`` the current one inside the block (the runtime does this for every request, startup hook and management command); ``None`` clears it. [reference](https://hinoki.caspel.dev/dev/reference/email/#hinoki.mail.use)
