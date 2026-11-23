# Email API

::: hinoki.mail.Message
    options:
      show_if_no_docstring: true

::: hinoki.mail.Address

::: hinoki.mail.Attachment

::: hinoki.mail.SentMail

::: hinoki.mail.Mailer
    options:
      members: [send, check, aclose, stats, outbox, clear_outbox]
      show_if_no_docstring: true

::: hinoki.mail.EmailModule
    options:
      members: [from_settings, mailer]
      show_if_no_docstring: true

::: hinoki.mail.EmailError

::: hinoki.mail.EmailErrorKind

::: hinoki.mail.send

::: hinoki.mail.use
