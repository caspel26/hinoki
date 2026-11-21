# 📋 Release Notes

All notable user-facing changes to Hinoki are recorded here, newest first. Hinoki is pre-alpha, so any release may change the API and the migration format. Entries below `Unreleased` have not been published; this repository has no tagged release yet.

## [Unreleased]

---

### ✨ New Features

#### 🧭 Create an app at the end of `hinoki new`
> `python/hinoki/newcli.py`, `python/hinoki/appprompts.py`, `python/hinoki/scaffold.py` (#405, #408, #410)

`hinoki new` creates a project without models. In an interactive terminal it now asks whether to create the first app and reuses the same code as `hinoki app create`; `--app NAME` creates it without asking and `--no-app` skips the question. Project and app names may contain single underscores.

#### 📍 Docs URL when the dev server starts
> `python/hinoki/serve.py`, `python/hinoki/runtime/supervisor.py` (#411)

`hinoki dev` prints the documentation URL next to the server address.

#### 🎨 Admin theme and logo
> `python/hinoki/settings.py`, `python/hinoki/admin/core.py`, `python/hinoki/admin/authviews.py` (#399, #421)

The `admin.theme` setting selects the default admin theme, and the admin logo is configurable.

#### 🖥️ Terminal banner
> `python/hinoki/runtime/banner.py`, `python/hinoki/terminal.py` (#412, #415)

The Hinoki box appears on every colour terminal and shows Kiso's startup in the `hinoki new` box.

---

### 🔧 Improvements

#### 🔒 Hardened HTTP and WSGI edges
> `crates/kiso/src/conn.rs`, `crates/kiso/src/stream.rs`, `crates/bindings/src/wsgi.rs` (#389, #401, #402)

Middleware, raw responses and the WSGI path are stricter: a path with a NUL byte or a dot segment is refused, and a WSGI stream whose application goes silent is reset.

#### 🗄️ Database defaults and messages
> `python/hinoki/db.py`, `crates/orm/src/database.rs` (#395, #397)

The database pool defaults to the settings size of 10, and a URL scheme that lacks its slashes is named in the error.

---

### 🐛 Fixes

- **`hinoki check`:** it imports the manifest's models before running the rules and warns when it ran no rules (`python/hinoki/checkcli.py`, #391, #393).
- **Migrations:** a plan error no longer suggests running `migrations plan` (#392).
- **Model validation:** text length errors count characters, not items (`python/hinoki/orm/model.py`, #396).
- **WSGI:** a plain WSGI callable reads its settings from the environment (`python/hinoki/runtime/wsgiworker.py`, #400).

---

### 📚 Documentation

The docsite has a new navigation, rules explorer, entry cards, footer and page transitions. The README is restructured around an animated request flow, the demo is rendered in HD at 60 fps, the comparative benchmark page is reorganized, and documentation links point at `hinoki.caspel.dev` (#388, #406, #409, #413, #416, #417, #419, #420, #423, #425, #426).

---

### 🎯 Summary

This is the running list of changes since the repository started. The first tagged release will be 0.1.0: `Unreleased` becomes `[0.1.0] - date`.
