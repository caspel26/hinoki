---
status: implemented
related:
  - status.md
  - toolchain.md
---

# Documentation versions

Hinoki keeps unreleased documentation separate from release documentation.

## Development

The [`dev` documentation](https://caspel26.github.io/hinoki/dev/) follows `main` and may change at any time. Before the first release, the site root redirects here. Treat it as documentation for the current pre-release code.

## Releases

Each release is built from its matching `vX.Y.Z` tag. Release pages remain available at their versioned path; `stable` and `latest` point to the most recent release. Changes merged to `main` update only `dev` and do not replace a released version.

Use the version menu in the header to move between available docs. When a page does not exist in an older version, the menu opens that version's home page.
