import pytest

from hinoki import API, Hinoki, Module, ModuleGraphError, RegistrationError, http
from hinoki.modules import order_modules
from hinoki.request import Request


class A(Module):
    name = "a"


class B(Module):
    name = "b"
    requires = (A,)


class C(Module):
    name = "c"
    requires = (B, A)


class Standalone(Module):
    name = "standalone"


class Things(API, path="/things"):
    @http.get("/")
    def listing(self, request: Request) -> list[int]:
        return []


class WithApi(Module):
    name = "things"
    apis = (Things,)


class CycleX(Module):
    name = "x"


class CycleY(Module):
    name = "y"
    requires = (CycleX,)


CycleX.requires = (CycleY,)


def names(modules: list[Module]) -> list[str]:
    return [m.name for m in modules]


def test_dependencies_come_first_regardless_of_install_order() -> None:
    assert names(order_modules([C(), B(), A()])) == ["a", "b", "c"]


def test_order_is_stable_for_independent_modules() -> None:
    assert names(order_modules([Standalone(), A()])) == ["standalone", "a"]


def test_missing_requirement_is_reported() -> None:
    with pytest.raises(ModuleGraphError, match="requires A"):
        order_modules([B()])


def test_cycle_is_reported_with_path() -> None:
    with pytest.raises(ModuleGraphError, match="cycle: x -> y -> x"):
        order_modules([CycleX(), CycleY()])


def test_duplicate_and_empty_names_are_rejected() -> None:
    with pytest.raises(ModuleGraphError, match="duplicate"):
        order_modules([A(), A()])
    with pytest.raises(ModuleGraphError, match="no name"):
        order_modules([Module()])


def test_install_includes_module_apis_and_validates_early() -> None:
    app = Hinoki()
    app.install(WithApi())
    assert app.resolve("GET", "/things") is not None
    assert [m.name for m in app.modules] == ["things"]

    with pytest.raises(ModuleGraphError):
        app.install(B())  # requires A, not installed
    assert len(app.modules) == 1


def test_install_and_include_are_refused_after_serving_starts() -> None:
    app = Hinoki()
    app.asgi()
    with pytest.raises(RegistrationError, match="frozen"):
        app.install(A())
