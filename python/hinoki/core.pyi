import asyncio
import os
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

NativeFailure = tuple[str, str, str | None, str | None, str | None]
NativeRows = tuple[list[dict[str, Any]], tuple[int, int, int] | None]

Param = (
    None
    | bool
    | int
    | float
    | str
    | bytes
    | UUID
    | datetime
    | dict[str, Any]
    | list[Any]
)
type Row = dict[str, Any]

def core_version() -> str: ...

class CompiledSchema:
    def validate(
        self,
        data: Any,
        construct: bool = True,
        use_aliases: bool = True,
        allow_naive_datetime: bool = False,
        skip_fields: list[str] = ...,
        input_bounds: bool | None = None,
    ) -> tuple[Any | None, list[tuple[str, str, str]]]: ...
    def coerce_text(self, data: Any, use_aliases: bool = True) -> Any: ...

def check_pattern(source: str) -> None: ...
def wire_text(value: Any) -> str | None: ...

class ModelPlan:
    def __init__(
        self, cls: Any, fields: list[tuple[str, bool, Any]], direct: bool = True
    ) -> None: ...
    def build(self, row: Any, extra: dict[str, Any] | None = None) -> Any: ...

class _RowPlan:
    def __init__(
        self,
        columns: list[str],
        fields: list[tuple[str, str, str, bool]],
    ) -> None: ...

class _ModelRows:
    def __init__(
        self,
        cls: type,
        columns: list[str],
        json_columns: list[str],
        snapshot_key: str,
    ) -> None: ...

class _NativeWait:
    def __await__(self) -> Any: ...

def driving() -> bool: ...
def _drive(coro: Any, cancel_after: int | None = None) -> Any: ...
def _native_wait(millis: int, value: Any, fail: bool) -> _NativeWait: ...

class _NativeRun:
    def cancel(self) -> None: ...

class _NativeSteps:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None: ...

def run_native(
    coro: Any,
    on_done: Callable[[Any, BaseException | None, str | None], None],
    on_foreign: Callable[[Any, Any, Any, float | None, str | None], None],
    ctx: Any = None,
    deadline_ms: int | None = None,
    scheduler: Callable[[Any], Any] | _NativeSteps | None = None,
) -> _NativeRun: ...

class _ClaimsAuth:
    def __init__(
        self,
        keys: list[tuple[str | None, str, bytes, bool]],
        issuer: str | None,
        audience: str | None,
        leeway: int,
        require_exp: bool,
        scope_claim: str,
        required: list[str],
    ) -> None: ...

class _CrudPlan:
    def __init__(
        self,
        action: str,
        model_name: str,
        table: str,
        columns: list[str],
        primary_key: str,
        ordering: list[tuple[str, bool]],
        page_size: int,
        max_page_size: int,
        model_fields: list[tuple[str, str, bool]],
        model_defaults: list[tuple[str, str, str]],
        create_defaults: list[tuple[str, str, str]],
        update_defaults: list[tuple[str, str, str]],
        create_columns: list[str],
        update_columns: list[str],
        updated_at: str | None,
        read_schema: CompiledSchema,
        read_fields: list[tuple[str, str, str, bool]],
        create_schema: CompiledSchema | None = ...,
        update_schema: CompiledSchema | None = ...,
        validation_error: bytes = ...,
        auth: _ClaimsAuth | None = ...,
        owner: str | None = ...,
    ) -> None: ...
    @property
    def action(self) -> str: ...
    @property
    def table(self) -> str: ...
    @property
    def dialects(self) -> list[str]: ...
    @property
    def validation_error_template(self) -> bytes: ...
    @property
    def update_columns(self) -> list[str]: ...
    def model_defaults(self) -> list[tuple[str, str, str | None]]: ...
    @property
    def create_defaults(self) -> list[tuple[str, str, str | None]]: ...
    def templates(self) -> list[tuple[str, str, str]]: ...
    def list_sql(self, dialect: str, page_size: int, offset: int) -> str: ...

def dump_instance(
    value: Any,
    json_mode: bool,
    exclude: list[str] = ...,
    by_alias: bool = True,
) -> dict[str, Any]: ...
def serialize_instance(
    value: Any,
    include: list[str] | None,
    exclude: list[str],
    exclude_unset: bool,
    by_alias: bool,
) -> dict[str, Any]: ...
def compile_schemas(
    definitions: list[tuple[type, list[tuple[str, str, tuple[Any, ...], bool]], bool]],
    root: int,
    uuid_class: type,
    from_iso: Any,
    unset: Any,
    direct: list[tuple[int, frozenset[str] | None]] | None = None,
) -> CompiledSchema: ...
def parse_query_string(raw: bytes, max_pairs: int) -> dict[str, list[str]]: ...
def parse_xml_body(raw: bytes, max_depth: int, max_elements: int) -> Any: ...
def parse_cookie_header(header: str) -> dict[str, str]: ...
def header_dict(headers: Sequence[Sequence[bytes]]) -> dict[str, str]: ...
def request_headers(
    headers: Sequence[Sequence[bytes]],
) -> tuple[dict[str, str], int]: ...
def response_headers(
    headers: list[tuple[bytes, bytes]],
    policy: list[tuple[bytes, bytes]],
    route_headers: Iterable[tuple[str, str]],
    request_id_name: bytes,
    request_id: bytes,
    allow_origin: bytes,
) -> tuple[list[tuple[bytes, bytes]], bool]: ...

class MultipartLimitError(ValueError): ...

def parse_multipart_body(
    raw: bytes, boundary: str, max_parts: int, max_part_bytes: int
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str, bytes]]]: ...
def negotiate_media(accept: str, available: list[str]) -> int | None: ...
def hash_password(
    password: str, memory_kib: int, time_cost: int, lanes: int
) -> str: ...
def verify_password(password: str, stored: str) -> bool: ...
def password_needs_rehash(
    stored: str, memory_kib: int, time_cost: int, lanes: int
) -> bool: ...
def make_token(count: int) -> str: ...
def constant_time_equal(a: bytes, b: bytes) -> bool: ...
def jwt_encode(claims: bytes, key: tuple[str | None, str, bytes, bool]) -> str: ...
def jwt_decode(
    token: str,
    keys: list[tuple[str | None, str, bytes, bool]],
    rules: tuple[int, int, str | None, str | None, bool],
) -> bytes: ...

class _JWTVerifier:
    def __init__(self, keys: list[tuple[str | None, str, bytes, bool]]) -> None: ...
    def verify(
        self, token: str, rules: tuple[int, int, str | None, str | None, bool]
    ) -> bytes: ...

def decode_json(raw: bytes) -> Any: ...
def encode_json(
    value: Any,
    indent: int | None = None,
    sort_keys: bool = False,
    default: Callable[[Any], Any] | None = None,
) -> bytes: ...

class Router:
    def __init__(self) -> None: ...
    def add(self, method: str, template: str, action: int) -> None: ...
    def find(
        self, method: str, path: str
    ) -> tuple[int, dict[str, str]] | tuple[str, list[str]] | None: ...

class DatabaseError(Exception):
    constraint: str | None
    table: str | None
    sqlstate: str | None

class UniqueViolation(DatabaseError): ...
class ForeignKeyViolation(DatabaseError): ...
class NotNullViolation(DatabaseError): ...
class CheckViolation(DatabaseError): ...

def configure_runtime(worker_threads: int) -> None: ...

class Transaction:
    async def fetch(self, sql: str, params: list[Param]) -> list[Row]: ...
    async def fetch_models(
        self, sql: str, params: list[Param], plan: _ModelRows
    ) -> list[Any]: ...
    async def _fetch_page_wire(
        self,
        sql: str,
        params: list[Param],
        plan: _RowPlan,
        page_info: tuple[int, int, int],
    ) -> bytes | list[Row]: ...
    async def execute(self, sql: str, params: list[Param]) -> int: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...

class Database:
    @staticmethod
    async def connect(url: str, max_size: int = 8, wait_ms: int = 5000) -> Database: ...
    async def fetch(self, sql: str, params: list[Param]) -> list[Row]: ...
    async def fetch_models(
        self, sql: str, params: list[Param], plan: _ModelRows
    ) -> list[Any]: ...
    async def _fetch_page_wire(
        self,
        sql: str,
        params: list[Param],
        plan: _RowPlan,
        page_info: tuple[int, int, int],
    ) -> bytes | list[Row]: ...
    async def execute(self, sql: str, params: list[Param]) -> int: ...
    async def begin(self) -> Transaction: ...
    async def begin_migration(self) -> Transaction: ...
    @property
    def dialect(self) -> str: ...
    def close(self) -> None: ...
    def status(self) -> tuple[int, int]: ...

def compile_query(
    kind: str,
    table: str,
    *,
    columns: list[str] | None = None,
    filters: list[tuple[str, str, list[Any]]] | None = None,
    assignments: list[tuple[str, Any]] | None = None,
    returning: list[str] | None = None,
    order: list[tuple[str, bool]] | None = None,
    limit: int | None = None,
    offset: int | None = None,
    joins: list[tuple[str, str, str, str, bool, list[str]]] | None = None,
    dialect: str = "postgres",
) -> tuple[str, list[Any]]: ...

class Submission:
    @property
    def early(self) -> tuple[int, list[tuple[bytes, bytes]], bytes] | None: ...
    @property
    def wake(self) -> bool: ...
    def reply(self) -> tuple[int, list[tuple[bytes, bytes]], bytes] | None: ...
    def cancel(self) -> None: ...

class PreparedRequest:
    @property
    def method(self) -> str: ...
    @property
    def path(self) -> str: ...
    @property
    def query(self) -> bytes: ...
    @property
    def request_id(self) -> str: ...
    @property
    def id_ignored(self) -> bool: ...
    @property
    def action(self) -> int | None: ...
    @property
    def needs_headers(self) -> bool: ...
    @property
    def params(self) -> dict[str, str]: ...
    @property
    def prepared_path_params(self) -> dict[str, str] | None: ...
    @property
    def client(self) -> tuple[str, int] | None: ...
    @property
    def http_version(self) -> str: ...
    @property
    def headers(self) -> list[tuple[bytes, bytes]]: ...
    def body(self, limit: int) -> Awaitable[bytes]: ...
    def _native_body(self, limit: int) -> Awaitable[bytes]: ...
    def _buffer_body(self, body: bytes) -> None: ...
    @property
    def native_handler(self) -> bool: ...
    @property
    def native_args(self) -> dict[str, object]: ...
    @property
    def native_principal(self) -> tuple[str, list[str]] | None: ...
    @property
    def native_error(
        self,
    ) -> tuple[str, str, str | None, list[tuple[str, str, str]]] | None: ...
    def _take_native_failure(self) -> NativeFailure | None: ...
    def _set_native_failure(
        self,
        kind: str,
        message: str,
        constraint: str | None = ...,
        table: str | None = ...,
        sqlstate: str | None = ...,
    ) -> None: ...
    def _take_native_rows(self) -> NativeRows | None: ...
    def _set_native_rows(
        self,
        rows: list[dict[str, Any]],
        page_info: tuple[int, int, int] | None = ...,
    ) -> None: ...
    def respond(
        self, status: int, headers: list[tuple[bytes, bytes]], body: bytes
    ) -> bool: ...
    def respond_final(
        self, status: int, headers: list[tuple[bytes, bytes]], body: bytes
    ) -> bool: ...
    def respond_routed(
        self,
        status: int,
        headers: list[tuple[bytes, bytes]],
        body: bytes,
        route_headers: Sequence[tuple[str, str]],
    ) -> bool: ...
    def respond_stream(
        self,
        status: int,
        headers: list[tuple[bytes, bytes]],
        route_headers: Sequence[tuple[str, str]],
        idle_timeout: float | None = ...,
        keepalive: float | None = ...,
        length: int | None = ...,
    ) -> ResponseSink | None:
        """Answer with the head now and the body from the returned sink.

        `None` when the request was already answered or nobody is waiting.
        Without `length` the body is chunked (HTTP/1.1) or ends with the stream
        (HTTP/2); with it the body must be exactly that long. Raises
        `ValueError` for a bodyless status (204, 304) or a timer that is not a
        positive number. `idle_timeout` and `keepalive` (seconds) are enforced
        by the Rust connection task: the first cuts the response when the
        producer sends nothing for that long, the second sends an SSE comment
        after that much silence.
        """

    def respond_file(
        self,
        status: int,
        headers: list[tuple[bytes, bytes]],
        route_headers: Sequence[tuple[str, str]],
        path: str | os.PathLike[str],
    ) -> bool:
        """Answer with the file at `path`; its size is the `content-length`.

        The connection opens and reads the file in Rust. A missing or
        non-regular file answers 404, any other failure 500. Raises
        `ValueError` for a bodyless status.
        """

class ResponseSink:
    """The producer's end of a streamed response.

    A piece is accepted only while the channel has room, so a producer sees
    backpressure as `False`. The Rust side does the framing, the idle and
    keep-alive timers and the writes; the producer that calls this is Python.
    """

    def try_send(self, data: bytes) -> bool:
        """Queue `data`; `False` when full or the response is closed. Never blocks."""

    def ready(self) -> Awaitable[bool]:
        """`True` once `try_send` can take a piece, `False` if closed."""

    def closed(self) -> Awaitable[bool]:
        """Wait for the end of the response.

        Resolves `True` when the client went away. It resolves `False` when
        `finish` or `abort` closed the response first, so `False` does not mean
        the client is gone. It does not resolve while the client is connected
        and the response open.
        """

    def finish(self) -> None:
        """End the body as complete. Idempotent."""

    def abort(self) -> None:
        """End the body as truncated (HTTP/1.1 closes, HTTP/2 resets)."""

class FileLookupBusy(Exception):
    """Every place for a file lookup is taken (`locate_file` fails at once)."""

def locate_file(
    path: str | os.PathLike[str], root: str | os.PathLike[str] | None = ...
) -> Awaitable[tuple[str, int] | None]:
    """The real path and size of the regular file `path` names.

    Symbolic links are followed; with `root` (a relative `path` is taken from
    it) the real path must lie inside the real `root`. Resolves `None` when
    there is no such file. The lookup runs on a blocking thread; at most 32 run
    at once in the process and the next one raises `FileLookupBusy`.
    """

def content_disposition(filename: str) -> str:
    """The `content-disposition: attachment` value for a download.

    The name gets an ASCII fallback, and the exact UTF-8 name when they differ.
    Raises `ValueError` for an empty name or one with `/` or a control character.
    """

class Tally:
    """Holds a streamed body to the `content-length` its head declared."""

    def __init__(self, length: int | None = ...) -> None: ...
    def fit(self, piece: bytes) -> bytes:
        """The part of `piece` that fits in the declared length (all of it
        without a length); `over` turns `True` when some did not."""

    @property
    def over(self) -> bool:
        """A piece went past the declared length."""

    @property
    def short(self) -> bool:
        """Fewer bytes than declared were sent; `False` without a length."""

class StreamLimit:
    """The places a worker has for open streamed responses, counted in Rust."""

    def __init__(self, limit: int) -> None:
        """Raises `ValueError` for a limit below 1."""

    @property
    def open(self) -> int:
        """The streams that hold a place now."""

    @property
    def limit(self) -> int: ...
    def acquire(self) -> StreamPermit | None:
        """Take a place, or `None` when the worker is full."""

class StreamPermit:
    """One place of a `StreamLimit`; a dropped permit gives it back too."""

    def release(self) -> None:
        """Give the place back. Idempotent."""

def sse_frame(
    data: str | None = ...,
    event: str | None = ...,
    id: str | None = ...,
    retry: int | None = ...,
    comment: str | None = ...,
) -> bytes:
    """One server-sent event as wire bytes.

    Raises `ValueError` for an `event` or `id` with CR, LF or NUL, or when no
    field is set. Line breaks in `data` and `comment` become separate lines.
    """

class BodyReadError(Exception): ...

RouteSpec = tuple[
    int,
    str,
    str,
    bool,
    bool,
    list[str],
    int,
    list[tuple[str, str]],
    bool,
    bool,
    tuple[bool, list[tuple[str, str, bool, str]]],
    _CrudPlan | None,
]

class ServerCore:
    def __init__(
        self,
        routes: list[RouteSpec],
        early_bodies: list[tuple[str, bytes]],
        policies: tuple[list[tuple[bytes, bytes]], list[tuple[bytes, bytes]]],
        request_id_name: bytes,
        request_id_prefix: str,
        limits: tuple[int, int, int, int] = ...,
        queue_capacity: int = 4096,
        request_timeout_ms: int = 30000,
        trust_request_id: bool = False,
        python_paths: list[str] = ...,
        cors: (
            tuple[bool, list[bytes], bool, bytes, list[tuple[bytes, bytes]]] | None
        ) = None,
        trusted_proxies: list[str] = ...,
        startup_path: str | None = None,
        startup_body: bytes = ...,
        hosts: tuple[list[bytes], list[bytes]] | None = None,
        redirect_https: bool = False,
        static_pages: list[tuple[str, list[tuple[bytes, bytes]], bytes]] = ...,
        route_auth: list[tuple[int, _ClaimsAuth, bytes | None]] = ...,
    ) -> None: ...
    def trusts(self, address: str) -> bool: ...
    def set_ready(self, ready: bool) -> None: ...
    def _disarm_route_auth(self, action: int) -> None: ...
    def _attach_database(self, database: Database) -> None: ...
    def _detach_database(self) -> bool: ...
    def _database_attached(self) -> bool: ...
    def _arm_crud(self, action: int) -> bool: ...
    def _disarm_crud(self, action: int) -> bool: ...
    def _disarm_crud_plans(self) -> None: ...
    def _crud_armed(self, action: int) -> bool: ...
    @property
    def _native_in_flight(self) -> int: ...
    def _drain_native(self, timeout_ms: int) -> Awaitable[int]: ...
    def take_ignored_ids(self) -> int: ...
    @property
    def ready(self) -> bool: ...
    @property
    def pending(self) -> int: ...
    @property
    def in_flight(self) -> int: ...
    def drain(self, max: int) -> list[PreparedRequest]: ...
    def submit(
        self,
        method: str,
        path: str,
        query: bytes,
        headers: list[tuple[bytes, bytes]],
        trusted: bool = False,
        https: bool = False,
    ) -> Submission: ...

class KisoServer:
    @staticmethod
    def start(
        core: ServerCore,
        host: str,
        port: int,
        max_connections: int = 10000,
        header_read_timeout: float = 10.0,
        shutdown_timeout: float = 10.0,
        max_header_bytes: int = 32768,
        body_read_timeout: float = 10.0,
        fd: int | None = None,
        write_timeout: float = 30.0,
        tls_cert: str | None = None,
        tls_key: str | None = None,
        http2: bool = False,
        access_log: str | None = None,
        access_log_skip: Sequence[str] | None = None,
        access_log_slow_ms: float | None = None,
        access_log_sink: Callable[[Sequence[str]], None] | None = None,
        server_timing: bool = False,
    ) -> KisoServer: ...
    @property
    def port(self) -> int: ...
    @property
    def wake_fd(self) -> int: ...
    def clear_wake(self) -> bool: ...
    def shutdown(self) -> Awaitable[int]: ...

class WsgiCall:
    @property
    def method(self) -> str: ...
    @property
    def path(self) -> str: ...
    @property
    def query(self) -> bytes: ...
    @property
    def headers(self) -> list[tuple[bytes, bytes]]: ...
    @property
    def client(self) -> tuple[str, int]: ...
    @property
    def secure(self) -> bool: ...
    @property
    def http_version(self) -> str: ...
    @property
    def body(self) -> bytes: ...
    def start(self, status: int, headers: list[tuple[bytes, bytes]]) -> bool: ...
    def send(self, chunk: bytes) -> bool: ...
    def abort(self) -> None: ...
    def finish(self) -> None: ...

class WsgiServer:
    @staticmethod
    def start(
        bridge: Callable[[WsgiCall], None],
        host: str,
        port: int,
        threads: int = 8,
        queue: int = 64,
        max_body_bytes: int = 1048576,
        request_timeout: float = 30.0,
        max_connections: int = 10000,
        header_read_timeout: float = 10.0,
        shutdown_timeout: float = 10.0,
        max_header_bytes: int = 32768,
        body_read_timeout: float = 10.0,
        fd: int | None = None,
        write_timeout: float = 30.0,
        tls_cert: str | None = None,
        tls_key: str | None = None,
        http2: bool = False,
        access_log: str | None = None,
        access_log_skip: Sequence[str] | None = None,
        access_log_slow_ms: float | None = None,
        access_log_sink: Callable[[Sequence[str]], None] | None = None,
        server_timing: bool = False,
    ) -> WsgiServer: ...
    @property
    def port(self) -> int: ...
    def shutdown(self) -> int: ...
