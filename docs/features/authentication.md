# Authentication

Who may call the API, and as whom. **API keys** are for your backend services: trusted, they
may act for any user. **JWTs** from your identity provider (Auth0, Okta, Entra ID, Keycloak,
Cognito, Google, …) are for end users: they chat, and manage their memory, only as themselves.

Back to the [docs home](../README.md) · See also: [configuration: `auth`](../configuration.md#auth)

## At a glance

| | |
| --- | --- |
| **What it does** | Checks every request's credentials and turns them into a `Principal` (who, which scopes, admin or not); refuses with 401, 403 or 503 (identity provider down). |
| **Turned on by** | `auth.mode` in [`config/app.yaml`](../../config/app.yaml): `api_key` (default), `jwt`, `api_key_or_jwt` or `none` |
| **Main files** | [`auth/api_key.py`](../../src/chatbot/auth/api_key.py) (`Authenticator`, `hash_key`, `new_key`), [`auth/jwt.py`](../../src/chatbot/auth/jwt.py) (`JwtVerifier`, `scopes_of`, `sign_dev_token`), [`auth/principal.py`](../../src/chatbot/auth/principal.py) (`Principal`, `current_principal`, `require_admin`), route wiring in `create_app` in [`api/app.py`](../../src/chatbot/api/app.py) |
| **Depends on** | `CHATBOT_API_KEYS` (key hashes); for JWTs, your provider's issuer URL and audience (or an HS256 secret for local tests) |
| **Used by** | Every `/v1` route; [memory](memory.md) and conversations are keyed by the resulting user ID |
| **Decisions** | ADR [017](../adr/017-jwt-oidc-alongside-api-keys.md) (JWT/OIDC alongside API keys), ADR [008](../adr/008-memory-keyed-by-caller-user-id.md) (memory keyed by the caller's user ID) |

## What it does

| Caller | Sends | Becomes | May |
| --- | --- | --- | --- |
| Your backend | `X-API-Key: cb_…` (or `Authorization: Bearer cb_…`) | `Principal(method="api_key", admin=True)` | Everything; pass any `user_id`. |
| An end user | `Authorization: Bearer <jwt>` | `Principal(method="jwt", subject=<sub>, scopes=…)` | Chat as `sub`; read and delete their own memory. Not ingestion, sources or usage. |
| An admin user | A JWT with the `chatbot:admin` scope | `admin=True` | What a backend may. |
| Anyone, `mode: none` | Nothing | Anonymous admin | Everything. Local use only. |

Which routes need what (`create_app`):

| Routes | Needs |
| --- | --- |
| `/healthz`, `/livez`, `/readyz` | Nothing |
| `/v1/chat`, `/v1/users/{user_id}/memory…` | Any valid credential (end users only for themselves) |
| `/v1/ingest`, `/v1/sources`, `/v1/documents…`, `/v1/jobs…`, `/v1/usage` | An API key or an admin token |

Real responses, from the API in `api_key_or_jwt` mode with a dev token for `alice`:

| Request | Status | Body |
| --- | --- | --- |
| No credentials | 401 | `{"detail": "missing bearer token", "trace_id": "…"}` |
| Alice chats with `"user_id": "bob"` | 403 | `{"detail": "user_id does not match the token", "trace_id": "…"}` |
| Alice calls `GET /v1/sources` | 403 | `{"detail": "this route needs an API key or admin scope", "trace_id": "…"}` |
| A token that expired two minutes ago | 401 | `{"detail": "token expired", "trace_id": "…"}` |
| Alice chats without a `user_id` | 200 | An answer; the turn is stored under `alice` |

## How it works

`create_app` builds one `Authenticator(settings.auth)` and adds it as a dependency to the
protected routers (and `require_admin` to the admin-only ones). Per request,
[`Authenticator.authenticate(request)`](../../src/chatbot/auth/api_key.py):

1. `mode: none` → the anonymous admin principal.
2. `api_key` or `api_key_or_jwt`: take the key from `X-API-Key`, or from `Authorization: Bearer`
   when that value isn't a JWT. Hash it (SHA-256) and compare with each configured hash in
   constant time (`hmac.compare_digest`). A match → an API-key principal.
   No match: in `api_key` mode, or when the key came in the header and there's no bearer token,
   → 401 "invalid or missing API key".
3. `jwt` or `api_key_or_jwt`: [`JwtVerifier.verify(token)`](../../src/chatbot/auth/jwt.py):
   1. Reads the token header; refuses an algorithm not in `algorithms` (`none` never).
   2. Finds the signing key: discovers the JWKS URL from
      `{issuer}/.well-known/openid-configuration` (unless `jwks_url` is set), caches the keys
      for `jwks_cache_s`, and refetches once (at most every 30 s) when a token names an unknown
      `kid`, which is how key rotation is picked up. HS256 uses the secret instead.
   3. Verifies the signature, `iss`, `aud`, `exp` (required), `nbf`, `iat`, with `leeway_s` (30)
      of clock skew.
   4. Takes the user ID from `user_id_claim` (`sub`) and scopes from `scope` or `scp`; checks
      `required_scopes`; sets `admin` if `admin_scope` is present.
   - `TokenError` (bad, expired, untrusted) → 401 with the reason.
   - `KeysUnavailable` (provider unreachable, no keys cached) → **503** with `Retry-After: 30`,
     not 401, because many front ends sign users out on a 401.
4. The principal is stored on `request.state.principal`.

Then the routes use it: `chat` calls `principal.resolve_user_id(req.user_id)` (end users get
their `sub`, or a 403 if they named someone else); the memory routes check the path's
`user_id`; `require_admin` refuses end users. Every 401/403/503 body includes the `trace_id`.

At startup, `api_key` mode with an empty `CHATBOT_API_KEYS` stops with a message telling you to
run `chatbot hash-key`, and JWT modes without `issuer` and `audience` fail validation.

## Inputs and outputs

**In:** request headers.

**Out:** a `Principal(method, subject, scopes, admin)`, available in any route as
`Depends(current_principal)`; or an error response with `WWW-Authenticate: Bearer` (401).

## Turn it on, change it, turn it off

**API keys** (the default):

```bash
uv run chatbot hash-key
# API key (give to the client):   cb_<random>
# Hash (put in CHATBOT_API_KEYS): <sha256>
```

Put the hash in `CHATBOT_API_KEYS` (comma-separated for several keys); give the key to the
client. Only hashes are stored, so a leaked `.env` doesn't leak keys. **Rotate** by adding the
new hash, moving clients over, then removing the old hash; restart to apply.

**JWTs** from your identity provider:

```yaml
auth:
  mode: api_key_or_jwt          # backends keep their keys; users send tokens
  jwt:
    issuer: https://your-tenant.eu.auth0.com/   # must equal the token's iss
    audience: https://chatbot.example.com       # must be in the token's aud
```

| Setting | Default | Notes |
| --- | --- | --- |
| `auth.mode` | `api_key` | `none` is for local use only. |
| `auth.api_keys_env` | `CHATBOT_API_KEYS` | The variable holding the hashes. |
| `auth.jwt.issuer` / `audience` | required in JWT modes | |
| `auth.jwt.jwks_url` | discovered | Set it if your provider has no discovery document. |
| `auth.jwt.algorithms` | `[RS256, ES256]` | `HS256` only with `hs256_secret_env`. |
| `auth.jwt.user_id_claim` | `sub` | e.g. `email` or a custom claim. |
| `auth.jwt.required_scopes` | `[]` | Scopes every token needs. |
| `auth.jwt.admin_scope` | `chatbot:admin` | Grants backend-level access. |
| `auth.jwt.leeway_s` / `jwks_cache_s` | 30 / 3600 | |

**Trying JWTs without a provider** (HS256, local only):

```bash
export DEV_JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
export CHATBOT__AUTH__MODE=jwt CHATBOT__AUTH__JWT__ISSUER=http://localhost \
       CHATBOT__AUTH__JWT__AUDIENCE=chatbot CHATBOT__AUTH__JWT__ALGORITHMS='[HS256]' \
       CHATBOT__AUTH__JWT__HS256_SECRET_ENV=DEV_JWT_SECRET
TOKEN=$(uv run chatbot dev-token --sub alice)              # --admin for the admin scope
curl localhost:8000/v1/chat -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"message": "How long is the refund window?"}'
```

**Rotating the provider's signing keys:** publish the new key before tokens use it; an unknown
`kid` refetches the keys at most every 30 seconds. The Keycloak procedure is in
[configuration: `auth`](../configuration.md#auth).

**Turning it off:** `auth.mode: none`, for local development only. Behind a gateway that
already authenticates users, prefer API keys between the gateway and this API, with the
gateway passing the `user_id`.

## Extend it

- **Permissions in another claim** (`roles`, `groups`, `permissions`): extend `scopes_of` in
  [`auth/jwt.py`](../../src/chatbot/auth/jwt.py) to read it; `required_scopes` and `admin_scope`
  then work with those values.
- **Your own rules in a route:** depend on the principal:

  ```python
  from fastapi import Depends, HTTPException

  from chatbot.auth.principal import Principal, current_principal


  @router.get("/v1/reports")
  async def reports(principal: Principal = Depends(current_principal)):
      if "reports:read" not in principal.scopes and not principal.admin:
          raise HTTPException(403, "needs reports:read")
      ...
  ```

- **Another kind of credential** (mTLS, a session cookie): add a branch to
  `Authenticator.authenticate` that returns a `Principal`; everything downstream only sees the
  principal.

More in [extending: map identity-provider claims](../extending.md#map-identity-provider-claims).

## Test it

```bash
uv run pytest tests/unit/test_auth.py tests/unit/test_api.py
```

| Test file | Covers |
| --- | --- |
| [`test_auth.py`](../../tests/unit/test_auth.py) | valid tokens via OIDC discovery (a fake provider); keys cached and rotation picked up; bad claims, forged and unsigned tokens refused; required scopes; a custom user-ID claim; HS256 dev tokens; config validation; both credentials in `api_key_or_jwt`; refusals carry the trace ID; provider down is 503, not 401; end users chat as themselves, manage only their own memory, can't reach ingestion; `jwt` mode refuses API keys |
| [`test_api.py`](../../tests/unit/test_api.py) | API-key auth; `api_key` mode without keys fails at startup; a wrong conversation owner gets 403 |

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| Startup: "auth.mode is api_key but CHATBOT_API_KEYS is empty" | No key hashes | `chatbot hash-key`, put the hash in `.env`. |
| 401 "invalid or missing API key" | Wrong key, or the key itself was put in `CHATBOT_API_KEYS` | The variable holds the **hash**; the client sends the key. |
| 401 "invalid token: InvalidAudienceError" / "…IssuerError" | `aud` / `iss` don't match the config | Copy the exact values from a decoded token (trailing slashes count). |
| 401 "token signed with an unknown key" | The provider rotated keys and the new one isn't published yet, or a token from another tenant | Check the provider's JWKS; wait 30 s after publishing. |
| 401 "token expired" right after sign-in | Server clocks differ by more than `leeway_s` | Fix NTP; raise `leeway_s` slightly. |
| 503 with `Retry-After` | The provider's discovery or JWKS URL is unreachable and no keys are cached | Check network access from the API to the provider. |
| 403 "user_id does not match the token" | The client sends a `user_id` for an end user | Omit `user_id` with JWTs; it comes from the token. |
| 403 "this route needs an API key or admin scope" | An end-user token on ingestion, sources or usage | Use an API key, or a token with `chatbot:admin`. |

## Related

- What a user ID owns: [memory](memory.md).
- Browser apps on other domains: CORS in [configuration: `api`](../configuration.md#api).
- All routes: [HTTP API](../api.md).
- Terms: [API key](../glossary.md#api-key), [JWT / OIDC](../glossary.md#jwt--oidc),
  [trace ID](../glossary.md#trace-id).
