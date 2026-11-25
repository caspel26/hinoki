import asyncio
import os
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import datetime
from typing import Any, Literal, TypedDict
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

class _ResponseObjectList:
    def __init__(
        self, fallback: Callable[[object], bool], item: Callable[[object], bool]
    ) -> None: ...
    def __call__(self, value: object) -> bool: ...

class _ResponseTypeList:
    def __init__(self, target: type, fallback: Callable[[object], bool]) -> None: ...
    def __call__(self, value: object) -> bool: ...
    def scan(self, value: object) -> int:
        """0 invalid, 1 valid and every item exactly ``target``, 2 valid otherwise."""

def all_exact(value: object, target: type) -> bool:
    """Whether ``value`` is a list whose items are all exactly of ``target``."""

CURSOR_MAX_TOKEN: int
"""Longest cursor token, in characters, that the native codec issues or accepts."""

class MemoryCache:
    """Native bounded byte cache with monotonic expiry and clock eviction."""

    def __init__(self, max_entries: int, max_bytes: int) -> None: ...
    def get(self, key: str) -> bytes | None: ...
    def set(self, key: str, value: bytes, ttl_seconds: float) -> None: ...
    def delete(self, key: str) -> bool: ...
    def clear(self) -> None: ...

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
    def decode_typed(self, raw: bytes) -> Any | None: ...
    def build(self, data: Any) -> Any: ...
    def build_text(self, data: Any) -> Any: ...
    def replace(self, instance: Any, changes: dict[str, Any]) -> Any | None: ...

class Pinned:
    """A per-class entry cached on the class it was built for; through another
    class it defers to ``fallback.__get__``."""

    def __init__(self, owner: Any, entry: Any, fallback: Any) -> None: ...
    def __get__(self, instance: Any, owner: Any = None) -> Any: ...

class NativeMethod:
    """``__copy__``, ``__deepcopy__``, ``__reduce__`` or ``__setstate__`` of a
    schema class, run in Rust over the names of its slots. ``kind`` is
    ``"copy"``, ``"deepcopy"``, ``"reduce"`` or ``"setstate"``; ``state`` lists
    every slot."""

    def __init__(self, kind: str, state: list[str]) -> None: ...
    def __call__(self, instance: Any, *arguments: Any) -> Any: ...
    def __get__(self, instance: Any, owner: Any = None) -> Any: ...

class NativeInit:
    """The constructor of a schema class: validates keyword arguments and writes
    them into the instance in Rust, or runs ``slow`` (the Python constructor)
    for anything it cannot vouch for. ``prepare()`` is called until it answers,
    and returns the compiled schema or ``None``."""

    def __init__(self, slow: Any, prepare: Any) -> None: ...
    def __call__(self, instance: Any, *args: Any, **kwargs: Any) -> None: ...
    def __get__(self, instance: Any, owner: Any = None) -> Any: ...
    @property
    def __wrapped__(self) -> Any: ...
    def __getattr__(self, name: str) -> Any: ...

def check_pattern(source: str) -> None: ...
def wire_text(value: Any) -> str | None: ...

class ModelPlan:
    def __init__(
        self, cls: Any, fields: list[tuple[str, bool, Any]], direct: bool = True
    ) -> None: ...
    def build(self, row: Any, extra: dict[str, Any] | None = None) -> Any: ...

type RawNestedPlan = tuple[str, str, str, list[tuple[str, str, str, bool]]]
type RawBridgeSpec = tuple[str, str, str, str, str]
type RawPrefetchPlan = tuple[
    str,
    str,
    list[str],
    str | None,
    list[tuple[str, bool]],
    RawBridgeSpec | None,
    str,
    str,
    list[tuple[str, str, str, bool]],
    int | None,
    str,
]

class _RowPlan:
    def __init__(
        self,
        columns: list[str],
        fields: list[tuple[str, str, str, bool]],
        nested: list[RawNestedPlan] = ...,
        prefetches: list[RawPrefetchPlan] = ...,
        key: tuple[str, str] | None = ...,
    ) -> None: ...

class _ProjectedRows:
    @property
    def len(self) -> int: ...
    @property
    def page_info(self) -> tuple[int, int, int] | None: ...
    def json_bytes(self) -> bytes: ...
    def rows(self) -> list[Row]: ...
    def prefetched_rows(self) -> dict[str, list[Row]]: ...

class _FilterPlan:
    """The declared client filters of one ``FilterSet``: checks a query string
    against the allow-list and returns typed values and conditions."""

    def __init__(
        self,
        fields: list[tuple[str, str, str, bool, list[str]]],
        search: list[str],
        ordering: list[str],
        max_in: int,
        max_text: int,
        max_search: int,
    ) -> None: ...
    def parse(
        self, query: dict[str, list[str]], reserved: list[str]
    ) -> tuple[list[tuple[str, str, str]], FilterParsed | None]:
        """``(problems, parsed)``: ``problems`` holds ``(path, code, message)``
        and ``parsed`` is ``None`` unless it is empty."""

type FilterEntry = tuple[str, object, list[object] | None, object, object, bool | None]
type FilterCondition = tuple[str, str, list[object]]
type FilterParsed = tuple[
    list[FilterEntry],
    tuple[FilterCondition, ...],
    list[tuple[str, bool]] | None,
    str | None,
    bytes,
]

class _CursorCodec:
    """Signed keyset cursors for one ordering; ``ValueError("invalid" |
    "expired" | "unencodable")`` on a bad token or key."""

    def __init__(
        self, secret: bytes, fingerprint: bytes, ttl: int | None = None
    ) -> None: ...
    def decode(self, token: str, count: int) -> tuple[bool, list[Any]]: ...
    def encode(self, backward: bool, values: list[Any]) -> str: ...

class _ModelRows:
    def __init__(
        self,
        cls: type,
        columns: list[str],
        json_columns: list[str],
        snapshot_key: str,
    ) -> None: ...
    def joined(
        self,
        joins: list[tuple[str, _ModelRows, str, int]],
        store_key: str,
    ) -> _ModelRows: ...
    def linked(self, alias: str, max_items: int) -> _ModelRows: ...

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
def handoff_batch[T](body: Callable[[], T]) -> T: ...

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
        links: list[tuple[str, LinkSpec, str | None]] = ...,
        filters: _FilterPlan | None = ...,
        soft_delete: str | None = ...,
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
    def list_sql(
        self, dialect: str, page_size: int, offset: int
    ) -> tuple[str, list[int]]: ...

def dump_instance(
    value: Any,
    json_mode: bool,
    exclude: list[str] = ...,
    by_alias: bool = True,
) -> dict[str, Any]: ...
def dump_for_storage(value: Any) -> dict[str, Any]: ...
def dump_as(value: Any, declared: type) -> dict[str, Any] | None:
    """``value`` written as the schema class ``declared`` it derives from: that
    class's fields and computed values only, nested values narrowed by its field
    types; ``None`` when ``declared`` decides its own ``dump()``."""

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
    direct: (
        list[tuple[int, frozenset[str] | None, dict[str, Any], dict[str, Any]]] | None
    ) = None,
) -> CompiledSchema: ...
def parse_query_string(raw: bytes, max_pairs: int) -> dict[str, list[str]]: ...
def parse_xml_body(raw: bytes, max_depth: int, max_elements: int) -> Any: ...
def encode_xml(value: Any, root: str = "response", item: str = "item") -> bytes: ...
def xml_name_ok(name: str) -> bool: ...
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
def make_uuid7() -> bytes: ...
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
    def verify_claims(
        self, token: str, rules: tuple[int, int, str | None, str | None, bool]
    ) -> dict[str, Any]: ...

def decode_json(raw: bytes) -> Any: ...

class JsonDecodeError(ValueError): ...

def encode_json(
    value: Any,
    indent: int | None = None,
    sort_keys: bool = False,
    default: Callable[[Any], Any] | None = None,
    ensure_ascii: bool = False,
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
    async def fetch_page_models(
        self,
        rows_sql: str,
        rows_params: list[Param],
        count_sql: str,
        count_params: list[Param],
        plan: _ModelRows,
        concurrent: bool = False,
    ) -> tuple[list[Any], int]: ...
    async def fetch_page_rows(
        self,
        rows_sql: str,
        rows_params: list[Param],
        count_sql: str,
        count_params: list[Param],
        concurrent: bool = False,
    ) -> tuple[list[Row], int]: ...
    async def fetch_projected_list(
        self,
        sql: str,
        params: list[Param],
        plan: _RowPlan,
    ) -> _ProjectedRows | list[Row]: ...
    async def fetch_projected_one(
        self,
        sql: str,
        params: list[Param],
        plan: _RowPlan,
    ) -> _ProjectedRows | list[Row] | str | None: ...
    async def fetch_projected_page(
        self,
        rows_sql: str,
        rows_params: list[Param],
        count_sql: str,
        count_params: list[Param],
        plan: _RowPlan,
        page: int,
        page_size: int,
        concurrent: bool = False,
    ) -> _ProjectedRows | tuple[list[Row], int]: ...
    async def _fetch_page_wire(
        self,
        sql: str,
        params: list[Param],
        plan: _RowPlan,
        page_info: tuple[int, int, int],
    ) -> bytes | list[Row]: ...
    async def _fetch_cursor_wire(
        self,
        sql: str,
        params: list[Param],
        plan: _RowPlan,
        codec: _CursorCodec,
        keys: list[str],
        limit: int,
        mode: int,
    ) -> bytes | tuple[list[Row], bool]: ...
    async def execute(self, sql: str, params: list[Param]) -> int: ...
    async def link_write(
        self,
        spec: LinkSpec,
        dialect: str,
        op: str,
        request: tuple[_LinkKey, list[_LinkKey]],
        scope: tuple[list[_RawJoin], list[_RawFilter], list[_RawSub]],
        owner_scope: tuple[list[_RawJoin], list[_RawFilter], list[_RawSub]]
        | None = None,
    ) -> tuple[str] | tuple[str, int] | tuple[str, list[_LinkKey], list[_LinkKey]]: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...

class Database:
    @staticmethod
    async def connect(url: str, max_size: int = 8, wait_ms: int = 5000) -> Database: ...
    async def fetch(self, sql: str, params: list[Param]) -> list[Row]: ...
    async def fetch_models(
        self, sql: str, params: list[Param], plan: _ModelRows
    ) -> list[Any]: ...
    async def fetch_page_models(
        self,
        rows_sql: str,
        rows_params: list[Param],
        count_sql: str,
        count_params: list[Param],
        plan: _ModelRows,
        concurrent: bool = False,
    ) -> tuple[list[Any], int]: ...
    async def fetch_page_rows(
        self,
        rows_sql: str,
        rows_params: list[Param],
        count_sql: str,
        count_params: list[Param],
        concurrent: bool = False,
    ) -> tuple[list[Row], int]: ...
    async def fetch_projected_list(
        self,
        sql: str,
        params: list[Param],
        plan: _RowPlan,
    ) -> _ProjectedRows | list[Row]: ...
    async def fetch_projected_one(
        self,
        sql: str,
        params: list[Param],
        plan: _RowPlan,
    ) -> _ProjectedRows | list[Row] | str | None: ...
    async def fetch_projected_page(
        self,
        rows_sql: str,
        rows_params: list[Param],
        count_sql: str,
        count_params: list[Param],
        plan: _RowPlan,
        page: int,
        page_size: int,
        concurrent: bool = False,
    ) -> _ProjectedRows | tuple[list[Row], int]: ...
    async def _fetch_page_wire(
        self,
        sql: str,
        params: list[Param],
        plan: _RowPlan,
        page_info: tuple[int, int, int],
    ) -> bytes | list[Row]: ...
    async def _fetch_cursor_wire(
        self,
        sql: str,
        params: list[Param],
        plan: _RowPlan,
        codec: _CursorCodec,
        keys: list[str],
        limit: int,
        mode: int,
    ) -> bytes | tuple[list[Row], bool]: ...
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
    joins: list[_RawJoin] | None = None,
    keyset: tuple[list[tuple[str, bool]], list[Any]] | None = None,
    subfilters: list[_RawSub] | None = None,
    dialect: str = "postgres",
    linked: bool = False,
) -> tuple[str, list[Any]]: ...

_RawJoin = tuple[str, str, str, str, bool, list[str], str | None]
_RawFilter = tuple[str, str, list[Any]]
# (table, selected column, joins, filters, subfilters)
_RawSelect = tuple[str, str, list[_RawJoin], list[_RawFilter], list["_RawSub"]]
# (outer column, negated, select)
_RawSub = tuple[str, bool, _RawSelect]
_LinkKey = int | str | UUID
_KeyEnd = tuple[str, str, str, str, int | None]

class LinkSpec:
    """Tables, columns and key types of one many-to-many relation; each end is
    ``(table, primary key, bridge column, "uuid" | "int" | "text", max length)``.
    ``target_live`` and ``owner_live`` are the soft-delete columns of the
    target and the owner, if they have one."""
    def __init__(
        self,
        bridge: str,
        bridge_pk: str,
        owner: _KeyEnd,
        target: _KeyEnd,
        max_items: int,
        declared_max: int | None = None,
        target_live: str | None = None,
        owner_live: str | None = None,
    ) -> None: ...

def coerce_link_keys(
    spec: LinkSpec, keys: list[Any], target: bool
) -> tuple[str, list[_LinkKey]] | tuple[str, int] | tuple[str]: ...

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
    @property
    def header_dict(self) -> dict[str, str]: ...
    def header(self, name: str) -> str | None: ...
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
    def respond_final_json(
        self,
        status: int,
        headers: list[tuple[bytes, bytes]],
        value: object,
        strict: bool = False,
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

    def accept_websocket(
        self,
        subprotocol: str | None = None,
        headers: list[tuple[bytes, bytes]] = ...,
    ) -> WebSocket | None:
        """Accept the handshake of a WebSocket request (method `"WS"`).

        The connection answers 101 with the negotiated `subprotocol` (one the
        client offered) and `headers`, then carries messages through the
        returned object. `None` when the request was already answered or nobody
        is waiting.
        """

class WebSocketSpec:
    """A WebSocket route and the limits Rust enforces on its connections."""

    def __init__(
        self,
        path: str,
        origins: list[bytes],
        max_message: int,
        queue: int,
        ping_interval: float | None,
        pong_timeout: float,
        idle_timeout: float | None,
        close_timeout: float,
        write_timeout: float,
    ) -> None: ...
    @property
    def path(self) -> str: ...

class WebSocket:
    """One open WebSocket: whole messages through two bounded queues.

    Framing, ping and pong, close, size limits and the timers run in Rust.
    """

    def receive(self) -> Awaitable[tuple[str, str | bytes] | None]:
        """`("text", str)` or `("bytes", bytes)`; `None` once the connection is
        closed and everything it delivered was read."""

    def send_text(self, text: str) -> Awaitable[bool]:
        """Queue a text message, waiting for room; `False` once closed."""

    def send_bytes(self, data: bytes) -> Awaitable[bool]:
        """Queue a binary message, waiting for room; `False` once closed."""

    def close(self, code: int = 1000, reason: str = "") -> Awaitable[bool]:
        """Close after the messages queued before it. `ValueError` for a code
        that cannot be sent; `False` when the connection is closed already."""

    @property
    def closed(self) -> tuple[int, str] | None:
        """`(code, reason)` once the connection ended, else `None`."""

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

def storage_disposition(disposition: str, filename: str, content_type: str) -> str:
    """The `content-disposition` value of a stored file served as `content_type`.

    `inline` is kept only for image/png, image/jpeg, image/gif, image/webp,
    image/avif, application/pdf and text/plain; any other type is downgraded to
    `attachment`. An empty `filename` sends no name. Raises `ValueError` for an
    unknown disposition or a name `content_disposition` refuses.
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
        route_cors: list[
            tuple[int, tuple[bool, list[bytes], bool, bytes, list[tuple[bytes, bytes]]]]
        ] = ...,
        websockets: list[WebSocketSpec] = ...,
        ws_connections: int = 1000,
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

class EventMetrics:
    def record(self, slot: int, outcome: int, seconds: float) -> None: ...

class MailMetrics: ...

class RateLimitMetrics:
    def record(self, decision: str) -> None: ...

class RateLimitUnavailable(RuntimeError): ...

class RateRule:
    def __init__(
        self,
        limit: int,
        window_s: int,
        cost: int,
        unknown_factor: int,
        fail_open: bool,
    ) -> None: ...

class RateDecision:
    @property
    def allowed(self) -> bool: ...
    @property
    def limit(self) -> int: ...
    @property
    def remaining(self) -> int: ...
    @property
    def reset(self) -> int: ...
    @property
    def retry_after(self) -> int: ...
    @property
    def failed_open(self) -> bool: ...

class RateLimiter:
    @staticmethod
    def database(
        db: Database, table: str, max_rows: int, timeout_ms: int, sweep_every: int
    ) -> RateLimiter: ...
    @staticmethod
    def memory(max_rows: int, sweep_every: int) -> RateLimiter: ...
    def check(
        self,
        rule: RateRule,
        route: str,
        target: tuple[int, str | None],
        tenant: str | None = None,
        now_us: int | None = None,
    ) -> Awaitable[RateDecision]: ...
    def check_blocking(
        self,
        rule: RateRule,
        route: str,
        target: tuple[int, str | None],
        tenant: str | None = None,
        now_us: int | None = None,
    ) -> RateDecision: ...
    def attach_metrics(self, metrics: RateLimitMetrics) -> None: ...

type _ShapeNode = (
    Literal["s"] | tuple[Literal["l"], _ShapeNode] | tuple[Literal["x"], str] | _Shape
)
type _Shape = dict[str, _ShapeNode]

class MailTemplate:
    def __init__(
        self,
        name: str,
        subject: str,
        html: str,
        text: str | None = None,
        shape: _Shape | None = None,
    ) -> None: ...
    def render(self, context: object) -> tuple[str, str, str]: ...

def parse_dotenv(text: str) -> list[tuple[str, str]]: ...
def is_host_pattern(entry: str) -> bool: ...
def is_valid_origin(entry: str) -> bool: ...
def is_http_token_str(entry: str) -> bool: ...
def is_media_rule(rule: str) -> bool: ...
def validate_storage_key(key: str) -> bool: ...
def normalize_content_type(value: str) -> str: ...
def new_storage_key() -> str: ...

class NativeMemoryStorage:
    def __init__(self, max_file_bytes: int, max_total_bytes: int) -> None: ...
    def save(
        self,
        content: bytes,
        content_type: str,
        filename: str,
        max_bytes: int | None = None,
    ) -> tuple[str, int, str, str, str]: ...
    def read(self, key: str, max_bytes: int | None = None) -> bytes: ...
    def metadata(self, key: str) -> tuple[str, int, str, str, str]: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> bool: ...

def html_escape(text: str) -> str: ...
def template_placeholders(source: str) -> list[tuple[str, bool]]: ...

class AdminTemplate:
    def __init__(self, source: str) -> None: ...
    def render(self, values: dict[str, Any], raw_type: type[Any]) -> str: ...

def append_recent(path: str, batch: list[str], max_bytes: int) -> None: ...
def read_recent(path: str, limit: int) -> list[dict[str, Any]]: ...
def validate_cache_key(value: str, prefix: str = "") -> str: ...
def validate_cache_ttl(value: object) -> float: ...

class NativeMemorySessionStore:
    def __init__(self, max_sessions: int) -> None: ...
    def load(
        self, token_hash: str
    ) -> tuple[bytes, int, int, int, int, str | None] | None: ...
    def save(
        self,
        token_hash: str,
        data_json: bytes,
        created_us: int,
        expires_us: int,
        idle_expires_us: int,
        last_seen_us: int,
        subject: str | None,
        now_us: int,
    ) -> None: ...
    def delete(self, token_hash: str) -> None: ...
    def delete_by_subject(self, subject: str) -> int: ...
    def purge_expired(self, now_us: int) -> int: ...
    def record_keys(self) -> list[str]: ...
    def revoked_keys(self) -> list[str]: ...

def fnmatch_case(text: str, pattern: str) -> bool: ...
def validate_waiver(
    index: int, raw: dict[str, Any]
) -> tuple[str, str, str, str, Any]: ...
def openapi_breaking_changes(old_json: bytes, new_json: bytes) -> list[str]: ...
def migration_quote(name: str, dialect: str = "postgres") -> str: ...
def migration_index_name(
    table: str, columns: list[str], dialect: str = "postgres"
) -> str: ...
def migration_foreign_key_name(
    table: str, column: str, dialect: str = "postgres"
) -> str: ...
def migration_dialect_type(state_type: str, dialect: str) -> str: ...
def migration_creation_order(tables_json: str) -> list[str]: ...
def migration_check_references(
    state_json: str, external_json: str | None = None
) -> None: ...
def migration_check_constraint_names(tables_json: str) -> None: ...
def migration_create_table_sql(
    name: str, spec_json: str, dialect: str = "postgres"
) -> str: ...
def migration_foreign_key_sql(
    table: str, column: str, state_json: str, dialect: str = "postgres"
) -> str: ...
def migration_alter_column(
    table: str,
    name: str,
    old_json: str,
    new_json: str,
    dialect: str = "postgres",
) -> list[tuple[str, str, str]]: ...
def migration_table_changes(
    table: str, old_json: str, new_json: str, dialect: str = "postgres"
) -> list[tuple[str, str, str]]: ...
def migration_sqlite_rebuild_warnings(old_json: str, new_json: str) -> list[str]: ...
def migration_diff(
    old_json: str, new_json: str, dialect: str = "postgres"
) -> list[tuple[str, str, str]]: ...
def migration_validate_state(state: object, filename: str) -> dict[str, Any]: ...
def migration_statements(text: str) -> list[str]: ...
def migration_render(title: str, operations: list[tuple[str, str, str]]) -> str: ...
def migration_auto_name(description: str) -> str: ...
def describe_template_error(
    template: str,
    problems: Sequence[tuple[str, str | None, int | None, int | None, str | None]],
    truncated: bool,
) -> str: ...
def template_reasons() -> list[str]: ...
def template_limits() -> list[tuple[str, int]]: ...

class _MailMessage(TypedDict):
    sender: tuple[str, str | None]
    to: list[tuple[str, str | None]]
    cc: list[tuple[str, str | None]]
    bcc: list[tuple[str, str | None]]
    reply_to: list[tuple[str, str | None]]
    subject: str
    text: str | None
    html: str | None
    attachments: list[tuple[str, str, bytes]]
    headers: list[tuple[str, str]]

class Mailer:
    def __init__(self, options: dict[str, object]) -> None: ...
    def send(
        self, message: _MailMessage
    ) -> Awaitable[tuple[str, list[str], int, bytes | None]]: ...
    def check(self) -> Awaitable[None]: ...
    def refuse(self, kind: str) -> None: ...
    def close(self) -> Awaitable[None]: ...
    def stats(self) -> tuple[int, int, int, int, int]: ...
    def attach_metrics(self, metrics: MailMetrics) -> bool: ...

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
        compression: dict[str, object] | None = None,
        metrics: dict[str, object] | None = None,
        reuse_port: bool = False,
    ) -> KisoServer: ...
    def event_metrics(self) -> EventMetrics | None: ...
    def mail_metrics(self) -> MailMetrics | None: ...
    def rate_limit_metrics(self) -> RateLimitMetrics | None: ...
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
        compression: dict[str, object] | None = None,
        metrics: dict[str, object] | None = None,
        reuse_port: bool = False,
    ) -> WsgiServer: ...
    @property
    def port(self) -> int: ...
    def shutdown(self) -> int: ...

def sha256_hex(text: str) -> str: ...
def hmac_sha256_hex(key: bytes, message: bytes) -> str: ...
def hmac_sha256_verify(key: bytes, message: bytes, expected: str) -> bool: ...
def csrf_mac(key: bytes, subject: str, nonce: str) -> str: ...
def csrf_mint(key: bytes, subject: str) -> str: ...
def csrf_verify(key: bytes, token: str, subject: str) -> bool: ...
def encode_header(name: str, value: str) -> tuple[bytes, bytes]: ...
def checked_headers(
    headers: dict[str, str],
) -> tuple[list[tuple[bytes, bytes]], list[str]]: ...
def set_cookie_header(
    name: str,
    value: str,
    path: str = "/",
    max_age: int | None = None,
    http_only: bool = True,
    secure: bool = True,
    same_site: str = "Lax",
) -> tuple[str, str]: ...
def decode_path_params(params: dict[str, str]) -> dict[str, str]: ...
def parse_bool(raw: str) -> bool: ...
def parse_int(raw: str) -> int: ...
def parse_float(raw: str) -> float: ...
def parse_uuid(raw: str) -> UUID: ...
def extract_boundary(content_type: str) -> str: ...
def same_site_origin(
    origin: str | None, fetch_site: str, host: str, scheme: str
) -> bool: ...
def websocket_origin_allowed(
    origin: str | None,
    host: str | None,
    https: bool,
    route_origins: list[str],
    cors_origins: list[str],
) -> bool: ...
def parse_page_params(
    query: dict[str, list[str]], default_size: int, upper: int
) -> tuple[int, int, list[tuple[str, str, str]]]: ...
def parse_cursor_params(
    query: dict[str, list[str]], default_size: int, upper: int
) -> tuple[int, str | None, list[tuple[str, str, str]]]: ...
def snake_case(name: str) -> str: ...
def app_label(module: str) -> str: ...
def resolve_model_names(
    class_name: str,
    table: str,
    label: str,
    plural: str,
    abstract_model: bool,
    module: str = "",
    app: str = "",
) -> tuple[str, str, str, bool, str]: ...
