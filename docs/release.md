---
status: implemented
related:
  - toolchain.md
  - documentation-versions.md
---

# Release checklist

How a Hinoki release is prepared. Nothing here runs by itself: the release
build workflow produces files, and publishing them is a manual decision of the
owner.

## What a release contains

The PyPI package is named `hinoki-api`; the module you import is `hinoki`
(`pip install hinoki-api`, `import hinoki`). The `hinoki` name on PyPI belongs to
an unrelated project.

| Artifact | Platforms | Built by |
| --- | --- | --- |
| abi3 wheel (`cp312-abi3`) | Linux x86_64 and aarch64 (manylinux 2014), macOS arm64 and x86_64 | `release-build.yml` |
| Source distribution | any (needs a Rust toolchain to install) | `release-build.yml` |

Windows is not built. The native extension uses Unix sockets, so
`pip install hinoki-api` on Windows falls back to the source distribution and
stops at a compile error that points to WSL2. On Windows, install and run
Hinoki inside WSL2.

The release profile uses fat LTO with one codegen unit (`[profile.release]` in
the workspace `Cargo.toml`), which makes the release build slower than a
default one. Measure a build before relying on its duration.

## Before the release

1. Run the checks of [Toolchain and CI](toolchain.md) on `main`, including `cargo deny check`
   and the full test suite.
2. If the change can affect speed, compare at least five current benchmark
   samples with a retained baseline (see [Benchmarks](benchmarks.md)).
3. Set the version, which must be the same in `pyproject.toml`
   (`project.version`) and in the workspace `Cargo.toml`
   (`workspace.package.version`), then run `uv lock` so `uv.lock` follows.
4. In `CHANGELOG.md`, turn `Unreleased` into `[X.Y.Z] - date`.
5. Update [Status](status.md) and the `Pre-alpha, not released` notes in the
   README if they no longer hold.
6. Merge these changes by pull request.

## Build and inspect the artifacts

1. Start **Release build** on `main` from the Actions tab (`workflow_dispatch`).
   A tag that starts with `v` starts the same workflow.
2. Wait for the four wheel jobs and the sdist job. Each wheel job installs its
   wheel in a clean Python 3.12 environment and serves a minimal app with
   `scripts/smoke_wheel.py`.
3. Download the artifacts and check them locally:

   ```sh
   gh run download RUN_ID --dir dist
   uvx twine check dist/*/*
   ```

## Publish

The repository has no automated publishing and no PyPI credentials. Publishing
is the owner's action:

1. Upload `dist/*/*` to PyPI by the owner's chosen method (a project token or
   trusted publishing configured for this repository).
2. Install the published version in a clean environment and run
   `python scripts/smoke_wheel.py` with that environment's interpreter.
3. Tag the release `vX.Y.Z`. The tag must equal `project.version`: the
   `release` job of `deploy-docs.yml` checks it and publishes the versioned
   documentation (see [Documentation versions](documentation-versions.md)).
   The development documentation must be published first.

## After the release

1. Create the GitHub release from the tag, with the `CHANGELOG.md` section as
   its notes.
2. Open a pull request that adds an empty `Unreleased` section to
   `CHANGELOG.md`.
