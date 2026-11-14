"""The declared form: ``Hinoki(settings=Class, include=[...], modules=[Class])``."""

import pytest

from hinoki import API, DatabaseSettings, FrameworkSettings, Hinoki, Module, http
from hinoki.db import DatabaseModule


class Declared(FrameworkSettings, prefix="DECL_"):
    database: DatabaseSettings


@http.get("/ping")
async def ping() -> dict[str, str]:
    return {"ok": "yes"}


class Exposes:
    """Anything with an ``api()`` can be listed in ``include`` (the admin does)."""

    def api(self) -> type:
        class Extra(API, path="/extra"):
            @http.get("/")
            async def index(self) -> dict[str, str]:
                return {"extra": "yes"}

        return Extra


def test_an_app_declared_as_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DECL_DATABASE__URL", "postgres://u:pw@h/db")
    monkeypatch.setenv("DECL_DOCS__TITLE", "Declared")
    app = Hinoki(
        settings=Declared,
        include=[ping, Exposes()],
        modules=[DatabaseModule],
        rules=None,
    )
    assert isinstance(app.settings, Declared) and app.title == "Declared"
    assert {r.path for r in app.routes} == {"/ping", "/extra"}
    (module,) = app.modules
    assert isinstance(module, DatabaseModule)
    assert module.url == "postgres://u:pw@h/db" and module.max_size == 10


def test_declaration_mistakes_are_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DECL_DATABASE__URL", "x")
    with pytest.raises(TypeError, match="cannot be built from settings"):
        Hinoki(settings=Declared, modules=[Module])
    with pytest.raises(TypeError, match="database: DatabaseSettings"):
        Hinoki(settings=FrameworkSettings, modules=[DatabaseModule])
    assert Hinoki(modules=[DatabaseModule("postgres://x")]).modules  # an instance works
