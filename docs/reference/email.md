# Email API

::: hinoki.mail.Message
    options:
      show_if_no_docstring: true

::: hinoki.mail.Address

::: hinoki.mail.Attachment

::: hinoki.mail.SentMail

::: hinoki.mail.Mailer
    options:
      members: [send, check, aclose, stats, outbox, clear_outbox, loaded_template]
      show_if_no_docstring: true

::: hinoki.mail.EmailModule
    options:
      members: [from_settings, mailer, templates]
      show_if_no_docstring: true

::: hinoki.mail.EmailError

::: hinoki.mail.EmailErrorKind

::: hinoki.mail.Template
    options:
      members: [file, from_toml, render, message]
      show_if_no_docstring: true

::: hinoki.mail.TemplateBase
    options:
      members: [path, loaded, load, compiled]
      show_if_no_docstring: true

::: hinoki.mail.TemplateError

::: hinoki.mail.TemplateReason

::: hinoki.mail.TemplateProblem

::: hinoki.mail.RenderedTemplate

::: hinoki.mail.Context

::: hinoki.mail.Value

::: hinoki.mail.send

::: hinoki.mail.use
