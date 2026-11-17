# Auth and crypto

`JWTAuth` delegates signature and registered-claim verification to the Rust core. Its current key and policy values are captured for each token verification, while the application-defined `resolve_claims` hook remains Python and runs for every verified token.

::: hinoki.auth.Caller

::: hinoki.auth.Credentials

::: hinoki.auth.BaseAuth

::: hinoki.auth.HeaderAuth

::: hinoki.auth.BearerAuth

::: hinoki.auth.APIKeyAuth

::: hinoki.crypto.JWTKey

::: hinoki.crypto.JWTAuth

::: hinoki.crypto.PasswordHasher

::: hinoki.crypto.random_token
