---
status: implemented
related:
  - admin-customize.md
  - authentication.md
  - cli.md
---

# Add an admin

`hinoki.admin` gives staff a list, search, create, edit, bulk actions and a dashboard for your models, behind its own sign-in page, users and per-model permissions. It ships with the framework; nothing is served until you include it, and importing it never touches a database.

```python
# blog/adminsite.py
from hinoki.admin import Admin, ModelAdmin, Tone

from blog.models import Post, User

site = Admin(site="Blog Studio")


@site.admin(Post)
class PostAdmin(ModelAdmin[Post]):
    columns = ("title", "published", "author_id", "created_at")
    readonly = ("created_at",)
    badges = {"published": {True: ("Published", Tone.OK), False: ("Draft", Tone.WARN)}}


site.register(User)  # a model with the default admin
```

```python
# blog/routes.py
from hinoki import Includable, Mount

from blog.adminsite import site

routes: list[Includable] = [Mount("/admin", site)]
```

The [Blog example](https://github.com/caspel26/hinoki/blob/main/examples/blog/apps/blog/adminsite.py) builds a fuller site the same way, with a theme, links, a dashboard and activity.

## Create the admin's tables

The admin keeps its users in four tables of your database. Register them as an app, apply the migrations, then create the first user:

```sh
hinoki app add admin --models hinoki.admin.users.models --migrations hinoki.admin.users.migrations
hinoki migrations apply admin
hinoki admin create-superuser alice    # asks for the password twice
```

| Table | Migration | Holds |
| --- | --- | --- |
| `hinoki_admin_users` | `0001-initial` | Username, Argon2id password hash, active and superuser flags, last sign-in |
| `hinoki_admin_sessions` | `0001-initial` | The SHA-256 of each session token, its user, expiry and last request |
| `hinoki_admin_grants` | `0001-initial` | One row per user, model and action |
| `hinoki_admin_login_windows` | `0002-login-windows` | Sign-in attempts per client, read only by `DatabaseLoginWindow` |

Every other user is created from the admin's Users section. `hinoki admin set-password alice` resets a password from a shell; both commands are described in [`hinoki admin`](cli.md#admin).

## Set the signing secret

The admin signs its forms and cookies with `admin.secret` from the app's settings. Give it at least 32 random bytes through the environment:

```sh
export BLOG_ADMIN__SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
```

| Setting | Default | Meaning |
| --- | --- | --- |
| `admin.secret` | none | Signing key, at least 32 bytes |
| `admin.session_hours` | `12` | A session ends this long after sign-in |
| `admin.idle_minutes` | `120` | A session ends after this long without a request |
| `admin.min_password_length` | `12` | Shortest password accepted |
| `admin.theme` | none | Built-in theme (`studio`, `console`, `paper`, `bento`) for an `Admin` that sets no `theme=`; `studio` when neither is set |

`Admin(secret=...)` overrides the secret and `Admin(theme=...)` the theme for one site. An admin with neither stops the app at startup.

## Who may sign in

`Admin()` without `access=` uses its own users:

* A **superuser** holds every permission.
* Other users hold **grants** per model and action: `view`, `add`, `change`, `delete`. A bulk action needs `change`.
* A model without a grant is missing from the sidebar, and its pages answer `404`.
* Grants wrap each model admin's `can`: an override can narrow what a grant allows, never widen it.
* An inactive user cannot sign in.

To use your own identity instead (single sign-on, an existing user table), pass an access policy: `Admin(access=Access(auth=..., permissions=[...]))`. The admin then has no sign-in pages and no tables, and your callers' permissions decide what `ModelAdmin.can` allows. See [authentication and access](authentication.md).

## The sign-in page

`login` shows a card titled **Sign in** with a username and a password field. Every page of the admin redirects there (`303`) when the browser has no valid session.

| Request | Answer |
| --- | --- |
| `POST login`, correct credentials | `303` to the admin root with a new session cookie; any previous session token is closed, never reused |
| `POST login`, wrong username or password | `401` "Wrong username or password." |
| `POST login`, expired or forged form | `403` "The form expired or is invalid. Reload and try again." |
| `POST login`, too many attempts | `429` (see [limit sign-in attempts](#limit-sign-in-attempts)) |
| `GET login` while signed in | `303` to the admin root |
| `POST logout` | `303` to `login`; the session ends and its cookie is cleared |

Usernames are stored in lower case: 1 to 150 letters, digits and `. _ @ + -`, starting with a letter or digit. An unknown username costs the same hashing work as a wrong password and gets the same answer.

`password` changes the signed-in user's password. The form asks for the current password, a new one (at least `admin.min_password_length` characters) and the new one again. A wrong current password, two different new ones or a password that breaks the length rule answer `422` with the reason on the form; an expired form answers `403`. Success ends every other session of the user and redirects (`303`) to the admin root (`?done=password`) with a new session.

### Sessions and cookies

* The session is a random token in the `hinoki_admin_session` cookie: `HttpOnly`, `SameSite=Lax`, scoped to the admin's path. The database stores only its SHA-256, so a copy of the table cannot be replayed.
* Every form carries a signed CSRF token that must equal the `SameSite=Strict` `hinoki_csrf` cookie. On the sign-in form the token is signed for signing in; on every other form it is bound to the signed-in user. A form is also refused when the browser marks it as sent from another site (`Sec-Fetch-Site: cross-site` or `same-site`) or its `Origin` header names another host; a request without these headers relies on the token alone.
* Cookies are `Secure` on HTTPS. Behind a TLS-terminating proxy, set `server.trusted_proxies` so the forwarded scheme is believed, or pass `Admin(secure_cookies=True)`: cookies are then always `Secure` and named `__Secure-hinoki_admin_session` and `__Secure-hinoki_csrf`, so a sibling site cannot plant them. Changing the option signs everyone out once.
* A session ends after `admin.session_hours`, after `admin.idle_minutes` without a request, on sign-out, on a password change and when its user is deactivated. Reactivating a user never revives an old session.

## Manage users

A superuser sees a **Users** section in the sidebar: create a user, choose the models and actions it may use, rename, deactivate, change a password or delete.

* Only a superuser opens it. No grant does, because a user who could edit users could grant itself everything.
* The permission checkboxes list `view`, `add`, `change` and `delete` for each registered model.
* An empty password on edit keeps the current one. A new one signs the user out everywhere. Passwords are never written back into the page.
* You cannot deactivate yourself, remove your own superuser status or delete yourself. Every write checks that an active superuser remains; `hinoki admin create-superuser` recovers if two superusers demote each other at the same instant.
* Deleting a user removes its sessions and grants.

`Admin(users=AdminAccounts(), manage_users=False)` leaves the section out, for a site whose users are created another way. `AdminAccounts.set_active(user, False)` deactivates a user from code and ends its sessions.

## Limit sign-in attempts

The sign-in and password forms pass through a rate limit policy before any password is hashed, and hold no database connection while hashing.

| Policy | Counts in | Behaviour |
| --- | --- | --- |
| `LoginWindow(limit=10, window=60)` (default) | One worker's memory | Each worker counts on its own and forgets on restart: a brake, not a shared limit. Holds at most 10,000 clients and drops the oldest. |
| `DatabaseLoginWindow(limit=10, window=60)` | `hinoki_admin_login_windows` | One limit for every worker and host. Clients are stored as SHA-256 digests; expired windows are deleted on each attempt. |

```python
# blog/adminsite.py
from hinoki.admin import Admin
from hinoki.admin.users import AdminAccounts
from hinoki.admin.users.limits import DatabaseLoginWindow

site = Admin(users=AdminAccounts(login_limit=DatabaseLoginWindow()))
```

Both count one window per client address; an IPv6 address counts by its /64. Requests without a known address share one window ten times as large, so set `server.trusted_proxies` behind a proxy. With `DatabaseLoginWindow`, keep host clocks in sync: each host stamps the windows it opens. Any other policy works too; see [rate limit an action](endpoints.md#rate-limit-an-action).

## Serve it under another path

The `Mount` prefix decides where the admin lives; links, assets and the cookie path follow it. `Mount("/ops", site)` serves it at `/ops`. `Admin(prefix="/staff")` adds a path of its own after the mount (`/ops/staff`). An admin included without a `Mount` is served at `/admin`.

## Errors and edge cases

| When | What fails |
| --- | --- |
| Startup, no `admin.secret` and no `secret=` | `RegistrationError`: "admin: no secret to sign its forms with; set `admin.secret` …" |
| `Admin(access=..., users=...)` | `ValueError`: pass one or the other |
| `register` of `AdminUser`, `AdminSession` or `AdminGrant` | `RegistrationError`: a plain model admin would show password hashes and let a grant holder promote itself |
| `register` with a slug that is taken or reserved (`static`, `search`, `login`, `logout`, `password`) | `RegistrationError` |
| `DatabaseLoginWindow` before `0002-login-windows` is applied | The first sign-in attempt fails on the missing table; apply the migration first |
| One address racing many sign-ins at the same instant | `429` rather than an uncounted attempt |

## See also

* [Customize the admin](admin-customize.md)
* [`hinoki admin` commands](cli.md#admin)
* [Authentication and access](authentication.md)
* [Settings](settings.md)
