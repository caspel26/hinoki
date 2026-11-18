---
status: implemented
related:
  - rules.md
  - endpoints.md
---

# Authentication and access policies

Hinoki keeps credential parsing in an authentication provider and permission checks in an `Access` policy. Endpoints are public unless a policy is attached; protect each private action deliberately.

Define closed permission names with `Permission`, an empty string-enum base. Plain
string permission values remain accepted: `Access` and `Caller` store every permission
as its string value, so a `Permission` member and the equal string match each other in
checks, reprs and logs. The compatibility cycle for string permissions (a deprecation
warning) and a typing that accepts only `Permission` members are deferred; neither is
in effect yet.

```python
from hinoki import Permission


class BlogPermission(Permission):
    WRITE = "posts:write"
```

Use `BlogPermission.WRITE` in `Access` policies and `Caller.permissions` so editors
and protected routes refer to the same typed constant.

## Define a provider

Subclass `BearerAuth[User]`, `APIKeyAuth[User]` or `HeaderAuth[User]`. The provider returns a `Caller` with a stable id, your user object and its permissions. `authenticate`/`resolve_token` may be async or synchronous; synchronous hooks run on the bounded sync executor. This example reuses the complete Blog example's user model.

```python
from blog.models import User
from blog.security import BlogPermission
from hinoki import BearerAuth, Caller


class TokenAuth(BearerAuth[User]):
    async def resolve_token(self, token: str) -> Caller[User] | None:
        user = await User.aget_or_none(token=token)
        if user is None:
            return None
        permissions = (
            frozenset({BlogPermission.WRITE}) if user.is_editor else frozenset()
        )
        return Caller(
            id=str(user.id),
            user=user,
            permissions=permissions,
        )
```

The [Blog security module](https://github.com/caspel26/hinoki/blob/main/examples/blog/security.py) contains the same provider and its reusable policies.

Absent credentials let the provider decline; invalid supplied credentials produce `401`. A valid caller without a required permission receives `403`.

## Attach a policy

```python
from blog.models import User
from blog.security import editor
from blog.schemas import PostOut
from hinoki import Caller, http


@http.get("/posts/mine", access=editor)
async def list_my_posts(caller: Caller[User]) -> list[PostOut]:
    # Replace with the application's scoped query.
    return []
```

`Caller[T]` is injected when annotated. Use `caller.user` for the application user and `caller.permissions` for the resolved permission set. Do not accept an owner id from untrusted input when it should come from the caller.

`RouteGroup` can provide a default policy for a function group. An action may override it intentionally. The `Actions.require_access` rule can flag routes with no policy.

## Passwords and JWT

`hinoki.crypto.PasswordHasher` provides Argon2id hashing and verification; `JWTKey` and `JWTAuth` issue and verify configured token algorithms. Read keys from `settings.Secret` fields and reveal them only when constructing the provider. Validate audience, issuer, expiry and key rotation settings for your deployment. See the [crypto API reference](reference/auth.md).

For `JWTAuth`, Rust verifies the signature and registered claims using the current key snapshot. `now()` and the configured issuer, audience, leeway (0 to 120 seconds; a provider with another value fails when it is built) and expiry policy are read for each verification. A request already verifying with a captured key snapshot finishes with that snapshot; a later verification observes key rotation. Rust does not turn token claims into a `Caller`: the application's `resolve_claims` hook still runs for every valid token, and remains responsible for loading or rejecting the identity and deciding its permissions. Sync, async and sync functions returning awaitables follow the normal hook execution path.

Hinoki does not create users, sessions or permission rules for your application. Those remain application data and policy.
