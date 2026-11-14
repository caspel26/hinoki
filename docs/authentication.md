---
status: implemented
related:
  - rules.md
  - endpoints.md
---

# Authentication and access policies

Hinoki keeps credential parsing in an authentication provider and permission checks in an `Access` policy. Endpoints are public unless a policy is attached; protect each private action deliberately.

## Define a provider

Subclass `BearerAuth[User]`, `APIKeyAuth[User]` or `HeaderAuth[User]`. The provider returns a `Caller` with a stable id, your user object and its permissions. `authenticate`/`resolve_token` may be async or synchronous; synchronous hooks run on the bounded sync executor.

```python
class SessionAuth(BearerAuth[Member]):
    async def resolve_token(self, token: str) -> Caller[Member] | None:
        session = await Session.aget_or_none(token=token)
        if session is None:
            return None
        member = await Member.aget(id=session.member_id)
        return Caller(
            id=str(member.id),
            user=member,
            permissions={"tasks:read", "tasks:write"},
        )
```

Absent credentials let the provider decline; invalid supplied credentials produce `401`. A valid caller without a required permission receives `403`.

## Attach a policy

```python
member_only = Access(SessionAuth(), permissions=["tasks:read"])

@get("/tasks", access=member_only)
async def list_tasks(caller: Caller[Member]) -> list[TaskOutput]:
    ...
```

`Caller[T]` is injected when annotated. Use `caller.user` for the application user and `caller.permissions` for the resolved permission set. Do not accept an owner id from untrusted input when it should come from the caller.

`RouteGroup` can provide a default policy for a function group. An action may override it intentionally. The `Actions.require_access` rule can flag routes with no policy.

## Passwords and JWT

`hinoki.crypto.PasswordHasher` provides Argon2id hashing and verification; `JWTKey` and `JWTAuth` issue and verify configured token algorithms. Read keys from `Secret` settings and reveal them only when constructing the provider. Validate audience, issuer, expiry and key rotation settings for your deployment. See the [crypto API reference](reference/auth.md).

Hinoki does not create users, sessions or permission rules for your application. Those remain application data and policy.
