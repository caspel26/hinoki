---
status: implemented
related:
  - status.md
  - toolchain.md
---

# Documentation versions

Hinoki keeps unreleased documentation separate from release documentation.

## Development

The [`dev` documentation](https://hinoki.caspel.dev/dev/) follows `main` and may change at any time. Before the first release, the site root redirects here. Treat it as documentation for the current pre-release code.

## Releases

Each release is built from its matching `vX.Y.Z` tag. Release pages remain available at their versioned path; `stable` and `latest` point to the most recent release. Changes merged to `main` update only `dev` and do not replace a released version.

Use the version menu in the header to move between available docs. When a page does not exist in an older version, the menu opens that version's home page.

Each version also has its own `llms.txt`, `llms-full.txt` and Markdown copy of every page, for example `https://hinoki.caspel.dev/dev/llms.txt`; see [docs for agents](ai-workflow.md#docs-for-agents). The `llms.txt` and `llms-full.txt` at the site root are copies from the default version: `dev` before the first release, `latest` after. Per-page Markdown copies exist only under a version path.
