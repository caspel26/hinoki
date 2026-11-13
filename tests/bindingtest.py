import pytest

from hinoki import API, HinokiAPI, RegistrationError, get


@get("/a/{x}/b/{x}")
def repeated(x: int) -> None: ...


@get("/pos/{x}")
def positional_only(x: int, /) -> None: ...


@get("/star/{x}")
def star_args(*x: int) -> None: ...


@get("/kw/{x}")
def star_kwargs(**x: int) -> None: ...


class NeedsArgs(API, path="/needs"):
    def __init__(self, dependency: object) -> None:
        self.dependency = dependency

    @get("/")
    def index(self) -> None: ...


class Base(API, path="/base"):
    @get("/one")
    def one(self) -> None: ...

    @get("/two")
    def two(self) -> None: ...


class Child(Base):
    def two(self) -> None:  # override without a route removes it
        return None


class Other(Base, path="/other"):
    pass


class WithStatic(API, path="/static"):
    @staticmethod
    @get("/x")
    def only() -> None: ...


@pytest.mark.parametrize(
    "target",
    [repeated, positional_only, star_args, star_kwargs, NeedsArgs, WithStatic],
)
def test_unusable_declarations_fail_at_include(target: object) -> None:
    with pytest.raises((RegistrationError, ValueError)):
        HinokiAPI().include(target)


def test_override_without_decorator_drops_the_route() -> None:
    assert [r.path for r in Child.routes()] == ["/base/one"]


def test_subclass_without_path_inherits_the_prefix() -> None:
    assert next(r.path for r in Child.routes()).startswith("/base")


def test_subclass_can_choose_its_own_prefix() -> None:
    assert [r.path for r in Other.routes()] == ["/other/one", "/other/two"]
