"""Admin list tabs, toolbar, root-mounted URLs and lazy UI resources."""

import types
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

from hinoki import db
from hinoki.admin import components, templates
from hinoki.admin.components import Components
from hinoki.admin.lists import render_toolbar, tab_bar
from hinoki.admin.modeladmin import ModelAdmin
from hinoki.admin.parts import Tab
from hinoki.admin.site import ListState, Site
from hinoki.admin.templates import Templates
from hinoki.orm import Model, field, pk


@db.soft_delete()
class Draft(Model, table="admin_ux_drafts"):
    id: int = pk()
    title: str
    deleted_at: datetime | None = field(default=None)


class Plain(Model, table="admin_ux_plain"):
    id: int = pk()
    qty: int


def site_for(state: ListState) -> Site:
    def url(*parts: str, **query: str) -> str:
        found = "&".join(f"{k}={v}" for k, v in query.items() if v)
        return "/" + "/".join(parts) + (f"?{found}" if found else "")

    return cast(
        Site,
        SimpleNamespace(
            admin=SimpleNamespace(templates=Templates()),
            state=state,
            url=url,
        ),
    )


def entry_of(model: type[Model], **config: Any) -> ModelAdmin[Any]:
    base: Any = ModelAdmin[model]  # type: ignore[valid-type]
    admin: ModelAdmin[Any] = types.new_class(
        "Config", (base,), exec_body=lambda ns: ns.update(config)
    )()
    admin.normalize(model.__name__.lower())
    return admin


def test_deleted_tab_follows_custom_tabs_on_a_soft_delete_model() -> None:
    entry = entry_of(Draft, tabs=(Tab("Short", {"title": "a"}),))
    html = str(tab_bar(site_for(ListState(deleted=True, done="saved")), "d", entry))
    assert ">All<" in html and ">Short<" in html and ">Deleted<" in html
    assert html.count('aria-current="page"') == 1
    assert "done=" not in html and 'role="tab"' not in html
    assert "<nav" in html


def test_soft_delete_models_without_tabs_keep_active_and_deleted() -> None:
    html = str(tab_bar(site_for(ListState()), "d", entry_of(Draft)))
    assert ">Active<" in html and ">Deleted<" in html


def test_no_tabs_and_no_search_render_neither_control() -> None:
    state = ListState()
    entry = entry_of(Plain)
    assert str(tab_bar(site_for(state), "p", entry)) == ""
    html = str(render_toolbar(site_for(state), "p", entry))
    assert "hn-tabs" not in html and "hn-search" not in html
    searchable = entry_of(Plain, search=("title",))
    assert "hn-search" in str(render_toolbar(site_for(state), "p", searchable))


def test_a_root_mounted_admin_links_to_the_root() -> None:
    admin = SimpleNamespace(base="")
    assert Site.url(cast(Site, SimpleNamespace(admin=admin))) == "/"
    assert Site.url(cast(Site, SimpleNamespace(admin=admin)), "notes") == "/notes"


def test_ui_resources_are_read_on_first_use_not_on_import() -> None:
    assert "toolbar" in templates.component_names()
    assert "search" in components.icon_names()
    assert Components is not None
