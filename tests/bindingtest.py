import pytest

from hinoki import API, Hinoki, RegistrationError, core, http
from hinoki.request import Request


@http.get("/a/{x}/b/{x}")
def repeated(request: Request, x: int) -> None: ...


@http.get("/pos/{x}")
def positional_only(request: Request, x: int, /) -> None: ...


@http.get("/star/{x}")
def star_args(request: Request, *x: int) -> None: ...


@http.get("/kw/{x}")
def star_kwargs(request: Request, **x: int) -> None: ...


class NeedsArgs(API, path="/needs"):
    def __init__(self, dependency: object) -> None:
        self.dependency = dependency

    @http.get("/")
    def index(self, request: Request) -> None: ...


class Base(API, path="/base"):
    @http.get("/one")
    def one(self, request: Request) -> None: ...

    @http.get("/two")
    def two(self, request: Request) -> None: ...


class Child(Base):
    def two(self, request: Request) -> None:  # override without a route removes it
        return None


class Other(Base, path="/other"):
    pass


class WithStatic(API, path="/static"):
    @staticmethod
    @http.get("/x")
    def only(request: Request) -> None: ...


@pytest.mark.parametrize(
    "target",
    [repeated, positional_only, star_args, star_kwargs, NeedsArgs, WithStatic],
)
def test_unusable_declarations_fail_at_include(target: object) -> None:
    with pytest.raises((RegistrationError, ValueError)):
        Hinoki().include(target)


def test_override_without_decorator_drops_the_route() -> None:
    assert [r.path for r in Child.routes()] == ["/base/one"]


def test_subclass_without_path_inherits_the_prefix() -> None:
    assert next(r.path for r in Child.routes()).startswith("/base")


def test_subclass_can_choose_its_own_prefix() -> None:
    assert [r.path for r in Other.routes()] == ["/other/one", "/other/two"]


def test_extension_types_report_the_renamed_module() -> None:
    assert core.Router.__module__ == "hinoki.core"
    assert core.DatabaseError.__module__ == "hinoki.core"
