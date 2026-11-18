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
    ) -> None: ...
    def trusts(self, address: str) -> bool: ...
    def set_ready(self, ready: bool) -> None: ...
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

class RustServer:
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
    ) -> RustServer: ...
    @property
    def port(self) -> int: ...
    @property
    def wake_fd(self) -> int: ...
    def clear_wake(self) -> bool: ...
    def shutdown(self) -> Awaitable[int]: ...
