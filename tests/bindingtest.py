from typing import Annotated

import pytest

from hinoki import (
    API,
    Field,
    Hinoki,
    MultiPartParser,
    Query,
    Request,
    Schema,
    UploadedFile,
    core,
    http,
)
from hinoki.errors import RegistrationError, ValidationError
from hinoki.runtime.binding import (
    Param,
    RequestData,
    bind,
    to_bool,
    to_uuid,
)


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


class RouteOverStatic(API, path="/static"):
    @http.get("/x")
    @staticmethod
    def only(request: Request) -> None: ...


class RouteOverClassMethod(API, path="/class"):
    @http.get("/x")
    @classmethod
    def only(cls, request: Request) -> None: ...


@pytest.mark.parametrize(
    "target",
    [
        repeated,
        positional_only,
        star_args,
        star_kwargs,
        NeedsArgs,
        WithStatic,
        RouteOverStatic,
        RouteOverClassMethod,
    ],
)
def test_unusable_declarations_fail_at_include(target: object) -> None:
    with pytest.raises((RegistrationError, ValueError)):
        Hinoki().include(target)  # type: ignore[arg-type]


def test_override_without_decorator_drops_the_route() -> None:
    assert [r.path for r in Child.routes()] == ["/base/one"]


def test_subclass_without_path_inherits_the_prefix() -> None:
    assert next(r.path for r in Child.routes()).startswith("/base")


def test_subclass_can_choose_its_own_prefix() -> None:
    assert [r.path for r in Other.routes()] == ["/other/one", "/other/two"]


def test_extension_types_report_the_renamed_module() -> None:
    assert core.Router.__module__ == "hinoki.core"
    assert core.DatabaseError.__module__ == "hinoki.core"


def test_boolean_uuid_and_page_binding_fallbacks() -> None:
    assert not to_bool("false")
    with pytest.raises(ValueError, match="yes"):
        to_bool("yes")
    with pytest.raises(ValueError, match=r"\{"):
        to_uuid("{00000000-0000-0000-0000-000000000001}")
    with pytest.raises(TypeError, match="without a pagination strategy"):
        Param("page", "page").extract(RequestData())
    with pytest.raises(ValidationError, match="validation error"):
        Param("files", "file", file_mode="many").extract(RequestData())


def test_file_binding_properties_distinguish_required_and_optional() -> None:
    @http.post("/required-file", parsers=[MultiPartParser()])
    def required_file(file: UploadedFile) -> None:
        return None

    @http.post("/optional-file", parsers=[MultiPartParser()])
    def optional_file(file: UploadedFile | None = None) -> None:
        return None

    required_app, optional_app = Hinoki(), Hinoki()
    required_app.include(required_file)
    optional_app.include(optional_file)
    required = bind(required_app.routes[0])
    optional = bind(optional_app.routes[0])
    assert required.has_files and required.requires_files
    assert optional.has_files and not optional.requires_files


def test_signature_introspection_failure_is_a_registration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "hinoki.runtime.binding.signature_of",
        lambda _handler: (_ for _ in ()).throw(TypeError("uninspectable")),
    )
    app = Hinoki()
    with pytest.raises(RegistrationError, match="cannot inspect signature"):
        app.include(http.get("/x", validate_response=False)(lambda: None))  # type: ignore[misc]


def test_annotation_resolution_failure_is_a_registration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_app = Hinoki()
    route_app.include(http.get("/x", validate_response=False)(lambda: None))  # type: ignore[misc]
    monkeypatch.setattr(
        "hinoki.runtime.binding.hints_of",
        lambda _handler: (_ for _ in ()).throw(NameError("missing annotation")),
    )
    with pytest.raises(RegistrationError, match="cannot resolve annotations"):
        bind(route_app.routes[0])


def test_request_must_be_positional_when_it_is_declared_first() -> None:
    @http.get("/keyword-only")
    def keyword_only(*, request: Request) -> None:
        return None

    with pytest.raises(RegistrationError, match="request parameter must be usable"):
        Hinoki().include(keyword_only)


def test_file_parameters_reject_defaults_before_registration() -> None:
    @http.post("/required-default", parsers=[MultiPartParser()])
    def required_default(file: UploadedFile = "default") -> None:  # type: ignore[assignment]
        return None

    @http.post("/repeated-default", parsers=[MultiPartParser()])
    def repeated_default(files: list[UploadedFile] = []) -> None:  # noqa: B006
        return None

    @http.post("/optional-default", parsers=[MultiPartParser()])
    def optional_default(file: UploadedFile | None = "default") -> None:  # type: ignore[assignment]
        return None

    for handler, message in (
        (required_default, "required file parameter"),
        (repeated_default, "repeated file parameter"),
        (optional_default, "optional file parameter"),
    ):
        with pytest.raises(RegistrationError, match=message):
            Hinoki().include(handler)


class Limits(Schema):
    n: Annotated[int, Field(ge=1, le=5)] = 1


def test_field_on_a_scalar_query_parameter_is_refused() -> None:
    async def route(n: Annotated[int, Field(ge=1, le=5)] = 1) -> None: ...

    with pytest.raises(
        RegistrationError, match=r"route.*scalar parameter 'n'.*Query\[Schema\]"
    ):
        Hinoki().include(http.get("/b", validate_response=False)(route))


def test_field_on_a_scalar_path_parameter_is_refused() -> None:
    async def route(slug: Annotated[str, Field(max_length=3)]) -> None: ...

    with pytest.raises(RegistrationError, match="scalar parameter 'slug'"):
        Hinoki().include(http.get("/b/{slug}", validate_response=False)(route))


@pytest.mark.parametrize(
    "hint",
    [
        Annotated[int, Field(ge=1)] | None,
        Annotated[Annotated[int, Field(ge=1)], "doc"],
        Annotated[int, Field(description="a number")],
    ],
)
def test_field_in_any_http_annotation_shape_is_refused(hint: object) -> None:
    async def route(n: int = 1) -> None: ...

    route.__annotations__["n"] = hint
    with pytest.raises(RegistrationError):
        Hinoki().include(http.get("/b", validate_response=False)(route))


def test_field_inside_query_schema_and_plain_annotations_still_register() -> None:
    async def schema_route(filters: Query[Limits]) -> None: ...
    async def plain_route(n: Annotated[int, "doc"] = 1) -> None: ...

    app = Hinoki()
    app.include(http.get("/a", validate_response=False)(schema_route))
    app.include(http.get("/b", validate_response=False)(plain_route))
