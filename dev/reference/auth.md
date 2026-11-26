# Auth and crypto

`JWTAuth` delegates signature and registered-claim verification to the Rust core. Its current key and policy values are captured for each token verification, while the application-defined `resolve_claims` hook remains Python and runs for every verified token.

- `hinoki.auth.Caller(id: 'str', user: 'T', permissions: 'Iterable[str]' = (), claims: 'Mapping[str, Any]' = mappingproxy({})) -> 'None'`: Who is making the request, as decided by authentication. [reference](https://hinoki.caspel.dev/dev/reference/auth/#hinoki.auth.Caller)

- `hinoki.auth.Permission(new_class_name, /, names, *, module=None, qualname=None, type=None, start=1, boundary=None)`: Base for an application's typed permission names. [reference](https://hinoki.caspel.dev/dev/reference/auth/#hinoki.auth.Permission)

- `hinoki.auth.Credentials(headers: 'Mapping[str, str]', query: 'dict[str, list[str]]', session: 'object | None' = None) -> None`: The parts of a request a provider may inspect. [reference](https://hinoki.caspel.dev/dev/reference/auth/#hinoki.auth.Credentials)

- `hinoki.auth.BaseAuth()`: Base class of providers. [reference](https://hinoki.caspel.dev/dev/reference/auth/#hinoki.auth.BaseAuth)

- `hinoki.auth.HeaderAuth()`: Credential carried in a named header; subclasses implement ``resolve``. [reference](https://hinoki.caspel.dev/dev/reference/auth/#hinoki.auth.HeaderAuth)

- `hinoki.auth.BearerAuth()`: ``Authorization: Bearer <token>``; subclasses implement ``resolve_token``. [reference](https://hinoki.caspel.dev/dev/reference/auth/#hinoki.auth.BearerAuth)

- `hinoki.auth.APIKeyAuth()`: API key in ``x-api-key`` (override ``header`` to change it). [reference](https://hinoki.caspel.dev/dev/reference/auth/#hinoki.auth.APIKeyAuth)

- `hinoki.crypto.JWTKey(kid: 'str | None', algorithm: 'str', key: 'bytes | str | Secret', public: 'bool' = False) -> None`: One verification (and optionally signing) key. [reference](https://hinoki.caspel.dev/dev/reference/auth/#hinoki.crypto.JWTKey)

- `hinoki.crypto.JWTAuth() -> 'None'`: Bearer-token provider for JWTs. [reference](https://hinoki.caspel.dev/dev/reference/auth/#hinoki.crypto.JWTAuth)

- `hinoki.crypto.ClaimsJWTAuth() -> 'None'`: A JWT provider whose caller comes from the signed claims alone. [reference](https://hinoki.caspel.dev/dev/reference/auth/#hinoki.crypto.ClaimsJWTAuth)

- `hinoki.crypto.PasswordHasher(*, memory_kib: 'int' = 19456, time_cost: 'int' = 2, lanes: 'int' = 1, workers: 'int' = 2, max_pending: 'int' = 64, allow_weak: 'bool' = False) -> 'None'`: Argon2id hashes in PHC format, computed off the event loop. [reference](https://hinoki.caspel.dev/dev/reference/auth/#hinoki.crypto.PasswordHasher)

- `hinoki.crypto.random_token(size: 'int' = 32) -> 'str'`: ``size`` random bytes (1 to 1024) as URL-safe text, from the OS CSPRNG. [reference](https://hinoki.caspel.dev/dev/reference/auth/#hinoki.crypto.random_token)
