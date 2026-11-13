# Admin extra — design (Q007)

Status: **A1 design, A2–A4 implemented** (`Reply`, `set_cookie`, `HTMLRenderer`; `hinoki.admin`: Classic theme, forms, CSRF, list/search/add/edit/delete; a `contains` filter operator in the SQL compiler). A5 done (blog integration, real-browser verification, screenshots; see below); A6 (review) open (2026-11-13). Source: `FRAMEWORK_SPEC.md` §16 (local package). Nothing below is implemented until a step says so.

## Observed

* Handlers return values that a renderer turns into bytes; there is no way for a handler to choose a status or set a header (needed for redirects and cookies). Only JSON is rendered today.
* `FormParser` and `MultiPartParser` exist (Q008), `request.POST`/`COOKIES`/`user` exist, `Access` policies and the generated-CRUD `Resource` (ownership, assigned fields) exist, `Model.QuerySet` offers filter/order/paginate.
* There is no session or login system; providers are `BearerAuth`, `APIKeyAuth`, `HeaderAuth`, or custom `BaseAuth` subclasses that see `Credentials(headers, query)` (cookies are in `headers["cookie"]`).
* No template engine or CSS toolchain is a dependency. Playwright browsers exist on the development machine for UI verification (used through `uvx`, never a project dependency).

## Scope of the first slice (what is and is not promised)

In: one theme (**Classic**, tabular), model list with search, sorting-free pagination and chosen columns, create/edit forms generated from the model, delete with confirmation, a front page listing registered models with counts, CSRF protection, per-request authorisation, template/CSS overrides, accessible HTML, desktop + mobile layouts verified in a browser.

Out (listed so nobody assumes them): Studio and Compact themes, Tailwind and Lucide (hand-written CSS and a few inline SVGs instead), relation widgets (a foreign key is an id text field), dashboards/metrics, audit log, account/login/session UI and tables, file uploads, bulk actions, MCP/admin integration. Installing or importing the admin never touches a database (no admin tables exist).

## Decisions

### D1 The admin is a module that produces ordinary routes

```python
from hinoki.admin import Admin, ModelAdmin

class PostAdmin(ModelAdmin[Post]):
    columns = ("title", "author_id", "published", "created_at")
    search = ("title",)

admin = Admin(prefix="/admin", access=staff, templates="templates/admin")
admin.register(Post, PostAdmin)
app.include(admin.api())
```

`admin.api()` returns a generated `API` class whose actions are real declarations (they show up in OpenAPI, rules, `check`, MCP), protected by the given `Access`. Mounting under a prefix, access policy and registration are explicit; nothing is registered by import. This keeps one registry and one auth path instead of a second mini-framework.

### D2 `Reply`: handlers can choose status, headers and body

A handler may return `hinoki.Reply(status, body=b"", headers={...})`, which bypasses rendering (it is the "explicit Response" escape hatch of the request design). Header names/values are validated (no CR/LF) and an action declaring `renderers=[...]` is unaffected otherwise. `HTMLRenderer` (`text/html; charset=utf-8`) renders a returned `str`. Both are general features, not admin-only.

### D3 Authorisation is per request and per model

The `Access` on the admin API authenticates and checks scopes. `ModelAdmin.queryset(principal)` (default: all rows) scopes lists and detail pages; `ModelAdmin.can(principal, action, row)` (default: allow if authenticated) gates view/add/change/delete. A hidden row is a 404, like generated CRUD. Saving goes through `ModelAdmin.save(values, principal, row=None)` (default `Model.acreate` / `aupdate_where` scoped by the queryset), so applications override one method to run a domain command or rule instead of writing around it. The admin never invents a bypass: it uses the same queryset-in-the-statement scoping as generated CRUD.

### D4 CSRF and cookies

State-changing admin requests are POST forms. Each page issues a random token in a hidden field and a `hinoki_csrf` cookie (`SameSite=Strict`, `HttpOnly`, `Path=<prefix>`, `Secure` unless the request scheme is `http`); the server compares both with `hmac.compare_digest` and answers 403 on mismatch or absence. Unsafe methods other than POST are not exposed. Authentication itself stays the application's `Access`; the docs show a cookie-token provider and state that a login UI is the application's job.

### D5 Templates: own, small, escaping by default

Components are files (`layout.html`, `table.html`, `form.html`, `field.html`, `notice.html`, …) with `{{ name }}` (HTML-escaped) and `{{{ name }}}` (raw, for pre-rendered child components) placeholders and no logic. Resolution order: the application's templates directory, then the built-in Classic theme. Missing placeholder values are errors at render time, never silently empty. The component contract (names and variables) is documented and versioned; the built-in theme uses it too. Python code builds the data and loops; the engine does no I/O except reading template files once at startup (no import-time I/O).

### D6 Forms from model annotations

Fields come from the model's columns (excluding assigned/primary key on create unless declared): `str` → text input (`textarea` when `max_length` is absent or > 200), `int`/`float` → number, `bool` → checkbox, `UUID` → text with pattern, `datetime` → `datetime-local`, `| None` makes the field optional (empty → `NULL`). Server-side conversion mirrors the binding rules (strict, no coercion beyond form text → declared type) and reports errors per field with `aria-describedby`. Unsupported types are shown read-only, not guessed.

### D7 Accessibility and responsive behaviour are part of the slice

Semantic landmarks, one `h1`, labelled controls, visible focus, skip link, keyboard-operable navigation, tables with captions/`scope`, destructive actions behind a confirmation page (no JavaScript required), `prefers-color-scheme` and reduced-motion respected. Layout: sidebar collapses to a top bar below 800 px; tables scroll horizontally inside their own container. No JavaScript is needed for any function in the first slice.

## Alternatives considered

* Jinja2 as a dependency: mature, but adds an engine with its own escaping/sandbox surface for a small need; revisit if the component set outgrows placeholders.
* A SPA front end: needs Node and a JSON admin API; rejected for the first slice.
* Session login module: valuable but a separate item; the admin takes any `Access`.

## Failure, empty and private states

Unauthenticated: 401 (or the provider's challenge). Missing scope: 403. Unknown model/row or hidden row: 404 HTML page. Empty list: an explicit "no rows" state with an add link when allowed. Validation errors re-render the form with values kept and status 422. CSRF failure: 403 page without performing the action. A database error is a 500 page with the request id, never the SQL. HTML is escaped everywhere; user data never enters attributes unescaped; `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'` and `X-Content-Type-Options: nosniff` on every admin response.

## Tests

Unit: template engine (escaping, raw, missing variable, override order), form field derivation per type, value conversion errors, CSRF accept/reject, header validation in `Reply`, `HTMLRenderer` negotiation. Integration (PostgreSQL): list/search/paginate, create/edit/delete round trip, hidden rows, `can()` denial, custom `save()`, 401/403/404. Browser (Playwright, dev-only): screenshots at 1440×900, 390×844, 320 wide, 360×740 in Chromium; keyboard path through list → edit → save → delete confirmation; a second engine (Firefox, WebKit) for layout and form behaviour; accessibility checks of the rendered DOM (labels, landmarks, focus order).

## Steps

| Step | Deliverable | Files |
| --- | --- | --- |
| A1 | This design | `docs/ADMIN-DESIGN.md` |
| A2 (done) | `Reply`, `HTMLRenderer`, header/cookie helpers | `python/hinoki/reply.py`, `renderers.py`, `runtime/asgi.py` |
| A3 (done) | Template engine, Classic theme (HTML + CSS), components | `python/hinoki/admin/templates.py`, `admin/theme/` |
| A4 (done) | `Admin`, `ModelAdmin`, forms, CSRF, list/create/edit/delete | `python/hinoki/admin/` |
| A5 (done) | Blog integration, PostgreSQL tests, browser verification and screenshots | `examples/blog`, `tests/admintest.py`, `tests/browser/` |
| A6 | Independent review (authz, CSRF, XSS, data writes), fixes, docs, handoff | — |

## Open (owner)

Template engine: own placeholders (recommended for now) versus Jinja2. Whether a session/login module should be the next item after the admin.

## Browser verification (A5)

`tests/browser/verify.py` drives the reference blog (served by Granian, synthetic seed data from `examples/blog/seed.py`) with Playwright, run through `uvx` so it is not a project dependency. On 2026-11-13: **0 failures in Chromium and Firefox**; **WebKit could not start** on this host (missing system libraries; installing them needs root), so WebKit is *not verified*.

Checked per engine: Swagger UI and ReDoc render the document; list, search, pagination, create with validation errors, edit, delete confirmation and delete through real form submissions; a forged CSRF token is refused; `can()` hides the add page for users; no horizontal page scroll and every input labelled at 1440×900, 390×844, 360×740 and 320×640 on the dashboard, list and form; keyboard path (skip link, focus to content, Tab to the search field, visible outline, Enter submits); dark scheme. Screenshots were inspected by eye: the first run showed an over-tall header bar, wrapped slugs on phones and long timestamps, all fixed (CSS grid rows, no-wrap cells, shorter dates), and the admin pages appearing in the API reference, fixed by excluding HTML routes from OpenAPI.

Limits of this verification: touch gestures were not exercised (the slice has none), screen readers were not used, and the contrast of the dark scheme was judged by eye, not measured.
