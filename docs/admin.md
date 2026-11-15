---
status: implemented
---

# The admin

The optional `hinoki.admin` module gives staff a list, search, create, edit, bulk delete and a dashboard for your models, with the same access policies and CSRF protection as the rest of the app. It is included in the framework package; nothing is mounted unless you ask, and importing it never touches a database.

The following excerpt follows the [Blog admin module](https://github.com/caspel26/hinoki/blob/main/examples/blog/adminsite.py). The `PostAdmin` class configures the model; `build_admin()` creates the cookie-authenticated `editors` policy, registers the class and returns the `Admin`. The app factory mounts that API. The snippet shows the relevant imports and function context; consult the linked module for the complete cookie-auth implementation.

```python
from blog.adminsite import CookieAuth
from blog.models import Post
from blog.security import WRITE
from hinoki import Access
from hinoki.admin import Admin, ModelAdmin, Badge, Tab, Fieldset

class PostAdmin(ModelAdmin[Post]):
    label = "Posts"
    columns = ("title", "published", "author_id", "created_at")
    search = ("title",)
    ordering = ("-created_at", "-id")
    bulk_delete = True
    readonly = ("created_at",)
    assign = {"author_id": lambda principal: principal.user.id}   # set by the server
    badges = {"published": {True: Badge("Published", "ok"), False: Badge("Draft", "warn")}}
    tabs = (Tab("Published", {"published": True}), Tab("Drafts", {"published": False}))
    fieldsets = (Fieldset("Organization", ("author_id", "published")),)

def build_admin(secret: str, theme: str = "studio") -> Admin:
    editors = Access(auth=CookieAuth(), permissions=[WRITE])
    admin = Admin(
        access=editors,
        secret=secret,
        prefix="/admin",
        theme=theme,
        site="Blog Studio",
    )
    admin.register(Post, PostAdmin, slug="posts")
    return admin
```

`search` names the columns the list's search box and the global search look in: a row matches when any of them contains the typed text, compared case-insensitively as text (so a number or date column matches on its printed form). Without `search` the box is inert.

Row visibility, field assignment and which columns are shown are decided in the admin class; hooks (`queryset`, `can`, `display`, `stats`, `check`, `save`, `remove`) override what you need. `queryset(principal)` is the primary way to decide which rows a caller sees: counts and page numbers come from it. `can(principal, "view", row)` is also applied row by row to what a list page or the dashboard fetched, so a denied row is not shown; it can only trim the page already fetched, so a page may hold fewer rows than `per_page` and the totals still count the queryset. Mass assignment, excluded columns and out-of-scope rows are refused by the framework, and every form carries a signed CSRF token bound to the signed-in subject.

Datetime fields in the admin use the including app's `settings.timezone` (UTC when the app has no built-in settings). Forms display stored instants in that timezone and interpret edited `datetime-local` values there before saving them as UTC-aware datetimes. Times that do not exist or occur twice during a daylight-saving clock change are rejected for correction. API datetimes with offsets remain absolute instants; `utc_today()` continues to use UTC.

## Bulk actions

Add an async handler and declare it with `BulkAction` on the model admin. The list shows its action button after rows are selected:

```python
from typing import Any

from hinoki import Caller
from hinoki.admin import BulkAction, ModelAdmin


async def publish(posts: tuple[Post, ...], _principal: Caller[Any]) -> None:
    await Post.filter(id__in=[post.id for post in posts]).aupdate(
        {"published": True}
    )


class PostAdmin(ModelAdmin[Post]):
    bulk_actions = (
        BulkAction(
            name="publish",
            label="Publish selected",
            confirm="Publish the selected posts?",
            handler=publish,
        ),
    )
```

The action name is also the permission checked by `ModelAdmin.can(principal, name, row)`. Hinoki checks model access before showing the button and checks every selected row against the scoped queryset and row permission both before confirmation and before execution. The confirmation includes the selected row count. If any selected row is unavailable or denied, the whole action is refused. The handler receives only the selected rows and the caller. During execution, Hinoki locks the selected model rows until the action transaction finishes, so another transaction cannot change their row-level authorization fields between the check and the handler's writes. Handlers should still apply their domain conditions to writes, especially when those conditions involve related records or external state.

Each action requires confirmation and runs inside the admin write transaction. A failure rolls back the selected work; successful actions redirect to the list with a notice. Actions accept at most 200 distinct rows. `bulk_delete = True` adds the built-in delete action.

## Four styles, each with light and dark

Each theme sets navigation, list presentation, typography and color. All four use the same components and provide light and dark modes; the system preference selects the initial mode, and the toggle overrides it.

| Style | Navigation | List | Edit | Look |
| --- | --- | --- | --- | --- |
| **Studio** (default) | sidebar | cards with a table | drawer beside the list | soft violet, rounded |
| **Console** | horizontal top bar | dense monospaced grid | drawer | neon cyan / teal, square |
| **Paper** | quiet text index, centred column | document rows | its own page | serif type, terracotta |
| **Bento** | dark icon rail (bottom tab bar on phones) | a grid of cards | its own page | hard ink outlines and offset shadows, bold type, solid colour tiles, yellow accent |

=== "Studio"

    ![Studio, light](screenshots/admin-studio-list-light.png)
    ![Studio, dark](screenshots/admin-studio-list-dark.png)

=== "Console"

    ![Console, light](screenshots/admin-console-list-light.png)
    ![Console, dark](screenshots/admin-console-list-dark.png)

=== "Paper"

    ![Paper, light](screenshots/admin-paper-list-light.png)
    ![Paper, dark](screenshots/admin-paper-list-dark.png)

=== "Bento"

    ![Bento, light](screenshots/admin-bento-list-light.png)
    ![Bento, dark](screenshots/admin-bento-list-dark.png)
    ![Bento edit form in dark mode](screenshots/admin-bento-edit-dark.png)
    ![Bento on a phone: the rail becomes a bottom tab bar](screenshots/admin-bento-phone-light.png)

Pick one with `Admin(theme="bento")`, or build your own: a `Theme` is a set of `--hn-*` design tokens for the two schemes plus a `layout` (`sidebar`, `topbar`, `index`, `rail`) and a `list_style` (`table`, `rows`, `cards`). The text and surface colour pairs of every built-in theme are tested against WCAG AA contrast. Bento's pages were checked in Chromium and Firefox at 1440, 390, 360 and 320 px; WebKit was not run.

## Mounting under a prefix

`Admin(prefix="/admin")` sets the admin's own path. The admin can also be included under a `Mount` (`Hinoki(include=[Mount("/ops", admin.api())])`): its links, assets and the CSRF cookie path then use `/ops/admin`, so the pages work at the mounted address.
