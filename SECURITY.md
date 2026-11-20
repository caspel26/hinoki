# Security policy

## Supported versions

Hinoki has not had a stable release yet. Security fixes land on `main` and are included in the next release; once `0.1.0` is published, the latest release is the supported one.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability. Report it privately through
[GitHub private vulnerability reporting](https://github.com/caspel26/hinoki/security/advisories/new).

Include the affected version or commit, a description of the impact and, if possible, steps or a minimal example that reproduce it. Do not include real credentials or production data.

You can expect an acknowledgement within 14 days. Vulnerabilities rated medium or higher are fixed in less than 60 days, and the fix is described in the release notes with a published advisory, crediting the reporter unless they prefer not to be named.

## Scope

In scope: the Hinoki Python package, the Rust crates (including the Kiso server) and the project's build and release workflows. Vulnerabilities in third-party dependencies should be reported to the dependency's maintainers; tell us if Hinoki needs to ship a fix or an update.
