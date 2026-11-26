# Protect endpoints with authentication and access

Every endpoint is public until you attach an `Access` policy. A policy pairs an
authentication provider (how a request proves who it is) with the permissions an
action needs. Write both once and name the policy on each private action.

```python
# blog/apps/blog/security.py
import hashlib

from hinoki import Access, BearerAuth, Caller, Permission

from .models import User


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class BlogPermission(Permission):
    WRITE = "posts:write"


class TokenAuth(BearerAuth[User]):
    """Looks the token's digest up in ``blog_users``; editors get the write scope."""

    async def resolve_token(self, token: str) -> Caller[User] | None:
        user = await User.aget_or_none(token_hash=hash_token(token))
        if user is None:
            return None
        scopes = frozenset({BlogPermission.WRITE}) if user.is_editor else frozenset()
        return Caller(id=str(user.id), user=user, permissions=scopes)


auth = TokenAuth()
editor = Access(auth=auth, permissions=[BlogPermission.WRITE])
reader = Access(auth=auth)
```

This is the [Blog example's security module](https://github.com/caspel26/hinoki/blob/main/examples/blog/blog/apps/blog/security.py).

## Name your permissions

Subclass `Permission`, an empty string enum, and use its members in policies and
callers. `Access` and `Caller` store each permission as its string value, so
`BlogPermission.WRITE` and `"posts:write"` match each other in checks, reprs and
logs; plain strings are still accepted. Passing one string instead of a
collection (`permissions="posts:write"`) is a `TypeError`.

## Define a provider

Pick the base class for where the credential travels and implement one hook:

| Base class | Credential | Implement |
| --- | --- | --- |
| `BearerAuth[User]` | `Authorization: Bearer <token>` | `resolve_token(token)` |
| `APIKeyAuth[User]` | the `x-api-key` header | `resolve(value)` |
| `HeaderAuth[User]` | the header named by `header` | `resolve(value)` |

The hook returns a `Caller` with a stable `id`, your user object and its
permissions, or `None` when the credential is unknown. It may be `async def` or
`def`; a synchronous hook runs on the bounded sync executor, so it may use
blocking libraries.

## Attach a policy

Name the policy with `access=` on the HTTP decorator and annotate a
`Caller[User]` parameter to receive the authenticated caller:

```python
# blog/apps/blog/drafts.py
from hinoki import Caller, http

from .models import Post, User
from .schemas import PostCreate, PostOut
from .security import editor


@http.post("/drafts", status=201, access=editor, atomic=True)
async def create_draft(data: PostCreate, caller: Caller[User]) -> PostOut:
    post = await Post.acreate(
        data,
        author_id=caller.user.id,
        publication_id=caller.user.publication_id,
    )
    return PostOut.from_model(post, author_name=caller.user.name)
```

Take the owner from `caller.user`, never from the request body. Permissions say
*what* a caller may do; whether a given row is theirs is still your query (the
Blog's `owned_post` helper in
[`api.py`](https://github.com/caspel26/hinoki/blob/main/examples/blog/blog/apps/blog/api.py)
shows one way).

To protect a whole group, set the policy once on a `RouteGroup`
(`RouteGroup("/invoices", access=billing_staff)`); an action may override it, and
`access=None` makes one action public on purpose.

## What clients receive

| Request | Answer |
| --- | --- |
| No credential | `401` with a `WWW-Authenticate` challenge |
| A credential the hook rejects (returns `None`), or a value that is not `Bearer <token>` | `401` with the challenge |
| A valid caller without a required permission | `403` |
| A policy without permissions (`reader` above) | any authenticated caller passes |

A policy without permissions is legitimate ("any signed-in user"), and it is easy
to write by accident; the [rule](https://hinoki.caspel.dev/dev/rules.md) `HNK-ACC-002` asks you to list such
actions explicitly.

## Hash passwords

`hinoki.crypto.PasswordHasher` hashes and verifies with Argon2id in Rust, off the
event loop. Create it where you own its lifetime (a factory or a module) and call
`close()` at shutdown, or use it as a context manager (`with` or `async with`).
Its worker threads start on first use; a closed hasher starts them again if it
is used, and a hasher that is garbage-collected shuts them down.

```python
# blog/apps/blog/passwords.py
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from hinoki import Module, Resource
from hinoki.crypto import PasswordHasher
from hinoki.modules import ModuleContext


@dataclass(frozen=True, slots=True)
class PasswordService(Resource):
    hasher: PasswordHasher


class Passwords(Module):
    name = "passwords"

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        hasher = PasswordHasher()
        ctx.provide(PasswordService, PasswordService(hasher))
        try:
            yield
        finally:
            hasher.close()
```

Inject `PasswordService` into handlers (see [Application modules](https://hinoki.caspel.dev/dev/modules.md)):

```python
async def change_password(new_password: str, passwords: PasswordService) -> str:
    return await passwords.hasher.hash(new_password)


async def password_matches(
    password: str, stored: str, passwords: PasswordService
) -> bool:
    return await passwords.hasher.verify(password, stored)
```

`hash_sync` and `verify_sync` exist for scripts and seeds; `needs_rehash(stored)`
tells you when to rehash after a parameter change, and `random_token()` returns a
URL-safe random token for opaque credentials. When the hashing queue is full, the
hasher raises `HashingBusy`, a `503`. The Blog's
[accounts module](https://github.com/caspel26/hinoki/blob/main/examples/blog/blog/apps/blog/accounts.py)
shows a complete login with constant-time behaviour for unknown users.

## Verify JSON Web Tokens

Subclass `JWTAuth[User]`, list the keys and implement `resolve_claims`, which
turns verified claims into a `Caller` (or `None`). Read secrets from a
`settings.Secret` field and pass it as the key: `JWTKey("2026-11", "HS256", app_settings.jwt_key)` accepts `bytes`, text or a `Secret`, and the key never appears in its `repr`.

For `JWTAuth`, Rust verifies the signature and registered claims using the current key snapshot. `now()` and the configured issuer, audience, leeway (0 to 120 seconds; a provider with another value fails when it is built) and expiry policy are read for each verification. A request already verifying with a captured key snapshot finishes with that snapshot; a later verification observes key rotation. Rust does not turn token claims into a `Caller`: the application's `resolve_claims` hook still runs for every valid token, and remains responsible for loading or rejecting the identity and deciding its permissions. Sync, async and sync functions returning awaitables follow the normal hook execution path.

## Claims-only tokens on the native lane

`ClaimsJWTAuth` is a `JWTAuth` whose caller comes from the token alone: `sub` (a
non-empty string) becomes `Caller.id`, the claim named by `scope_claim` (default
`scopes`, a list of strings) becomes the permissions, and `Caller.user` is `None`.
No database lookup happens, so a token cannot be revoked before it expires: keep
lifetimes short.

```python
# notes/tokens.py
from collections.abc import Sequence
from typing import ClassVar

from hinoki import Access, Caller, Permission, Schema, http
from hinoki.crypto import ClaimsJWTAuth, JWTKey


class NotesPermission(Permission):
    WRITE = "notes:write"


class Tokens(ClaimsJWTAuth):
    keys: ClassVar[Sequence[JWTKey]] = (
        JWTKey("2026-11", "HS256", b"0123456789abcdef0123456789abcdef"),
    )  # read the key from a Secret in a real application
    issuer = "https://api.example.com"
    audience = "notes"


class WhoAmI(Schema):
    id: str
    permissions: list[str]


tokens = Tokens()
writer = Access(auth=tokens, permissions=[NotesPermission.WRITE])


@http.get("/me", access=writer)
async def me(caller: Caller[None]) -> WhoAmI:
    return WhoAmI(id=caller.id, permissions=sorted(caller.permissions))
```

`tokens.issue({"sub": "u1", "scopes": ["notes:write"]}, ttl=900)` signs with the
first key that can sign and sets `iat`, `exp`, and `iss`/`aud` when configured.

### The policies of the billing examples {#billing-policies}

The endpoint, CRUD and transaction guides import `billing_staff` and
`billing_admin` from one module. Declare the policies once and import them
wherever an action or `RouteGroup` names them:

```python
# notes/security.py
from collections.abc import Sequence
from typing import ClassVar

from hinoki import Access, Permission
from hinoki.crypto import ClaimsJWTAuth, JWTKey


class BillingPermission(Permission):
    MANAGE = "billing:manage"
    ADMIN = "billing:admin"


class StaffTokens(ClaimsJWTAuth):
    keys: ClassVar[Sequence[JWTKey]] = (
        JWTKey("2026-11", "HS256", b"0123456789abcdef0123456789abcdef"),
    )  # read the key from a Secret in a real application


tokens = StaffTokens()
billing_staff = Access(auth=tokens, permissions=[BillingPermission.MANAGE])
billing_admin = Access(auth=tokens, permissions=[BillingPermission.ADMIN])
```

A token from `tokens.issue({"sub": "u1", "scopes": ["billing:manage"]}, ttl=900)`
passes `billing_staff`; without a token the answer is `401`, and with a token
that lacks the permission it is `403`.

Generated CRUD routes protected by an unmodified `ClaimsJWTAuth` can run on the native lane. Rust only admits requests: a single `Authorization: Bearer <token>` header whose token verifies (signature, expiry, issuer, audience, `sub`) and carries every required permission goes straight to the native action. Every other request, including a missing, malformed, expired, forged or under-privileged one, is handed to the Python pipeline, which answers exactly as it does without the native lane (`401` with the challenge, or `403`). With `owner` and `identity_key = caller_id`, create, update and delete are native too: Rust sets the owner column from `sub`, and update/delete are constrained to rows that `sub` owns. Foreign or missing rows, and MySQL/MariaDB, are answered by the Python pipeline. A subclass that overrides a method, a modified instance, or any other auth class keeps the route on Python. The route's key, issuer, audience, leeway and permission snapshot is rechecked about once per second; reassigning them disarms the native plan until the worker restarts, so a rotation reaches native routes after that interval, not per verification. `hinoki routes --lanes` shows which lane each route uses.

Rust verifies the token on these routes too: a handler route protected by an unmodified `ClaimsJWTAuth` (not generated CRUD) is authenticated by Rust, which admits the caller or answers `401` or `403` itself with the same body and `WWW-Authenticate` challenge as Python, in the same order relative to path/query errors, the rate limit and body validation. Rust defers to Python for anything it cannot decide identically: several `Authorization` headers, a value that is not a plain `Bearer <token>`, or a request Rust already found malformed. A rate limit still runs afterwards in Python, with the admitted caller. Error handlers registered with `app.errors` do not apply to these answers, on either path, because they translate other exceptions. The provider and policy are checked about once per second as for CRUD routes.

## Errors and edge cases

- Return `None` for an unknown credential; let unexpected exceptions propagate, they are server errors, not `401`.
- `JWTAuth` leeway must be 0–120 seconds; another value fails when the provider is built.
- Providers hold no per-request state: one instance serves every request and worker thread.
- Hinoki does not create users, sessions or permission rules: those remain your application's data and policy.
- A refusal by an access policy emits an `AuthRejected` [event](https://hinoki.caspel.dev/dev/events.md#authentication-and-rate-limit-events) (`missing`, `invalid` or `forbidden`, the route template and the method; never credentials or the requested path) when the application has a handler for it. The handler cannot change the answer. On Kiso, a route that Rust would authenticate is authenticated by Python while such a handler is registered, so the event can fire.

## See also

- [Rules](https://hinoki.caspel.dev/dev/rules.md): make "every private action has a policy" a checked promise.
- [Hosts, CORS and HTTPS](https://hinoki.caspel.dev/dev/web-security.md): the browser-facing side of security.
- [Authentication reference](https://hinoki.caspel.dev/dev/reference/auth.md)
