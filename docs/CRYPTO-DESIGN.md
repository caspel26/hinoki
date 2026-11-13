# Crypto: password hashing and JWT, in Rust — design (Q011)

Status: **C1 design, C2 implemented** (Rust Argon2id hash/verify/needs_rehash, `random_token`, constant-time compare in `crates/bindings/src/crypto.rs`; Python wrapper and JWT still open) (2026-11-13). Owner request: define what to use for the crypto part, in Rust. Nothing below is implemented until a step says so.

## Observed

* Auth providers exist (`BearerAuth`, `APIKeyAuth`, `HeaderAuth`, custom `BaseAuth`), but there is **no JWT provider and no password hashing**; `docs/handoff` lists the JWT library choice as an open owner decision.
* The native core is `crates/bindings` (PyO3 `hinoki.native`) over pure-Rust crates; heavy work must not block the event loop (`AGENTS.md`: bounded executors, no unmanaged background work).
* Versions checked on crates.io on 2026-11-13: `argon2` 0.6.0 (MIT OR Apache-2.0), `hmac` 0.13.0, `sha2` 0.11.1, `subtle` 2.6.1, `getrandom` 0.4.3, `ed25519-dalek` 3.0.0, `p256` 0.14.0, `rsa` 0.10.0, `ring` 0.17.14 (last release 2025-03), `jsonwebtoken` 11.1.0 (MIT; its `rust_crypto` backend depends on `hmac` ^0.12, `sha2` ^0.10, `rsa` ^0.9, `ed25519-dalek` ^2, i.e. older majors than the current ones; its `aws_lc_rs` backend needs a C/asm build).

## Decisions

### D1 Algorithms and crates (recommendation)

| Need | Choice | Why |
| --- | --- | --- |
| Password hashing | **Argon2id** via the RustCrypto `argon2` crate, PHC strings | OWASP/RFC 9106 recommendation; pure Rust (no C toolchain); PHC format is self-describing, so parameters can be raised later and old hashes still verify |
| Randomness | `getrandom` (OS CSPRNG) | no user-space PRNG state |
| Constant-time compare | `subtle` | the standard crate; used for MACs and tokens |
| JWT HMAC (`HS256/384/512`) | `hmac` + `sha2` | the same family as the above; small |
| JWT public-key | **Ed25519 (`EdDSA`)** via `ed25519-dalek`; `ES256` later if asked | fast, small keys, no parameter pitfalls. **RSA is not in the first slice**: the `rsa` crate has carried a known timing-side-channel advisory (RUSTSEC-2023-0071); decide with `cargo audit` output in hand before adding `RS256` |
| JWT container | **in-house minimal JWS/JWT (compact serialisation only)** on the crates above, not `jsonwebtoken` | the library pins older majors of the same primitives (two versions of `hmac`/`sha2` in one wheel) and exposes much more than needed (JWE, PEM, JWK, many algorithms); a ~300-line verifier with a fixed allow-list is easier to review. It must stay boring: no feature the design does not list |
| Python `cryptography` / `PyJWT` | **not used** | owner requirement (Rust), and one fewer dependency tree |

Every crate version is pinned by `Cargo.lock`; adoption requires `cargo audit` and `cargo deny` (licences) to pass in CI before merge. If a crate named here turns out unsuitable at implementation time, that is recorded as a deviation, not hidden.

### D2 JWT rules (the security contract)

* **The application fixes the algorithm allow-list per key; the token's `alg` is only compared against it** (never used to choose a verifier). `none` and anything outside the list are rejected. Mixing a symmetric and an asymmetric algorithm on one key is impossible by construction (a key has one algorithm).
* Keys are explicit: `JWTKey(kid, algorithm, secret_or_public_key)`. With several keys the token **must** carry a `kid` that names one; with exactly one key `kid` is optional. HMAC secrets shorter than the hash size (32/48/64 bytes) are rejected at configuration time. Keys come from settings/environment, never from defaults.
* Claims are checked by the Rust verifier with an injected clock: `exp` required unless `require_exp=False`, `nbf`, optional `iat` sanity, `iss` and `aud` when the provider configures them (audience may be a string or a list), `leeway` (default 0, capped at 120 s). Unknown `crit` headers are rejected; `typ` is not trusted for anything; `zip` is rejected.
* Limits: token ≤ 8 KiB, header and payload JSON ≤ the existing JSON depth limit, each segment strict base64url without padding.
* Errors never reveal why a token failed to the client (always 401 `Invalid credentials`); the reason (`expired`, `bad_signature`, …) goes to a debug log at most, never with the token.
* Revocation, refresh tokens, sessions and login are **out of scope**: the provider answers "is this a valid token for this app"; what a principal may do is still `Access` scopes.

### D3 Public API

```python
from hinoki.crypto import PasswordHasher, JWTAuth, JWTKey, random_token

hasher = PasswordHasher()                       # Argon2id, OWASP-grade defaults in settings
stored = await hasher.hash("correct horse")     # PHC string, random 16-byte salt, off the event loop
ok = await hasher.verify("correct horse", stored)
if ok and hasher.needs_rehash(stored): ...      # parameters raised since the hash was made

class Tokens(JWTAuth[User]):
    keys = [JWTKey("2026-11", "HS256", settings.jwt_secret)]
    issuer = "https://api.example.com"
    audience = "blog"
    async def resolve_claims(self, claims: dict[str, Any]) -> Principal[User] | None: ...

token = Tokens.issue({"sub": str(user.id)}, ttl=900)     # for services/tests that mint tokens
```

`PasswordHasher` runs Argon2 on a **bounded** dedicated thread pool (default 2 workers, configurable) with the GIL released in Rust, so hashing never stalls the event loop and cannot create unbounded work. `verify` returns `False` for a wrong password and raises `ValueError` for a malformed stored hash (a data bug, not a login failure). `random_token(n=32)` returns URL-safe text from the OS CSPRNG.

### D4 Parameters and storage

Argon2id defaults from current OWASP guidance (19 MiB memory, 2 iterations, 1 lane) as the floor, overridable in settings, with a startup check that refuses parameters below the floor unless explicitly allowed (tests). Only PHC strings are stored; passwords, keys and tokens are never logged, never in exception messages, and zeroised in Rust where the crates support it.

## Alternatives considered

* `jsonwebtoken` directly: less code to own, but the duplicate dependency majors and the larger surface (algorithms, JWK, PEM parsing in default features) weigh against it; revisit if the in-house verifier grows beyond the stated scope.
* `ring`: fast and audited, but the last release is from 2025 and it needs C/asm; not chosen for a pure-Rust core.
* scrypt/bcrypt: acceptable, but Argon2id is the current recommendation and the PHC format keeps migration possible.
* Sessions instead of JWT: a different feature (state, revocation); not blocked by this design.

## Failure, empty and private states

Wrong password → `False`; malformed hash → `ValueError`; wrong/expired/unknown-`kid`/oversized/`alg` mismatch token → the provider raises `Unauthorized("Invalid credentials.")` (401 with the bearer challenge); missing header → no credentials (public actions unaffected). Mis-configured keys fail at class creation/startup, not on the first request. A clock jump is bounded by `leeway`; the verifier takes `now` as a parameter so tests are deterministic.

## Tests

Independent oracles, not self-consistency: HMAC signatures cross-checked against Python's stdlib `hmac`/`hashlib`; the RFC 7515 Appendix A.1 and RFC 8037 (Ed25519) published vectors; Argon2 hashes verified both ways against `argon2-cffi` run through `uvx` during development (recorded in the review notes, not a project dependency). Negative suite: `alg: none`, algorithm substitution (HS256 token signed with a public key as secret), wrong/missing `kid`, truncated/padded/oversized segments, duplicate header keys, `exp`/`nbf`/`aud`/`iss` edges with leeway, non-string `aud` elements, huge numbers in claims, constant-time compare used (reviewed, not timed). Provider tests through the ASGI harness (401 vs 403, challenge header). Executor bound test for hashing. Independent review focused on authentication before merge.

## Steps

| Step | Deliverable | Files |
| --- | --- | --- |
| C1 | This design | `docs/CRYPTO-DESIGN.md` |
| C2 (done) | Rust: Argon2id hash/verify/needs_rehash, `random_token`, constant-time helpers | `crates/bindings/src/crypto.rs`, `Cargo.toml` |
| C3 | Rust: JWS compact (HS256/384/512, EdDSA) encode/verify + claim validation | `crates/bindings/src/jwt.rs` |
| C4 | Python: `PasswordHasher`, `JWTKey`, `JWTAuth`, settings checks, OpenAPI scheme | `python/hinoki/crypto.py` |
| C5 | Tests with oracles/vectors; example use in the blog (password column + login-less token issue script) | `tests/`, `examples/` |
| C6 | `cargo audit`/`deny` in CI; independent authentication review, fixes, docs, handoff | CI, — |

## Open (owner)

`RS256`/`ES256` support (recommended: only when a concrete need exists, after an advisory check); whether a login/session module follows (Q007 note); the Argon2 parameter floor if deployment memory is tight.
