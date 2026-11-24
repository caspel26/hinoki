# Rules API

- `hinoki.rules.RuleSet(*rules: 'Rule') -> 'None'`: The rules an application enabled. [reference](https://hinoki.caspel.dev/dev/reference/rules/#hinoki.rules.RuleSet)

- `hinoki.rules.Rule()`: Base class of rules. [reference](https://hinoki.caspel.dev/dev/reference/rules/#hinoki.rules.Rule)

- `hinoki.rules.Diagnostic(...)`: Diagnostic(rule_id: 'str', severity: 'Severity', phase: 'Phase', message: 'str', location: 'str', explanation: 'str', status: 'Status' = <Status.VIOLATED:…. [reference](https://hinoki.caspel.dev/dev/reference/rules/#hinoki.rules.Diagnostic)

- `hinoki.rules.Actions()`: Namespace of action rules. [reference](https://hinoki.caspel.dev/dev/reference/rules/#hinoki.rules.Actions)

- `hinoki.rules.Transactions()`. [reference](https://hinoki.caspel.dev/dev/reference/rules/#hinoki.rules.Transactions)

- `hinoki.rules.Models()`: Namespace of model rules; ``match`` is an ``fnmatch`` on the model's module. [reference](https://hinoki.caspel.dev/dev/reference/rules/#hinoki.rules.Models)

- `hinoki.rules.Schemas()`. [reference](https://hinoki.caspel.dev/dev/reference/rules/#hinoki.rules.Schemas)

- `hinoki.rules.Imports()`. [reference](https://hinoki.caspel.dev/dev/reference/rules/#hinoki.rules.Imports)

- `hinoki.rules.EmailTemplatesMatch()`: Base class of rules. [reference](https://hinoki.caspel.dev/dev/reference/rules/#hinoki.rules.EmailTemplatesMatch)

- `hinoki.rules.Phase(*values)`: Enum where members are also (and must be) strings. [reference](https://hinoki.caspel.dev/dev/reference/rules/#hinoki.rules.Phase)

- `hinoki.rules.Severity(*values)`: Enum where members are also (and must be) strings. [reference](https://hinoki.caspel.dev/dev/reference/rules/#hinoki.rules.Severity)

- `hinoki.rules.Status(*values)`: Enum where members are also (and must be) strings. [reference](https://hinoki.caspel.dev/dev/reference/rules/#hinoki.rules.Status)
