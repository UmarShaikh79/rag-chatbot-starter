# ADR 017: JWT/OIDC alongside API keys

Status: Accepted (milestone 2)

## Decision

`auth.mode` is `none`, `api_key`, `jwt` or `api_key_or_jwt`. The API is an OAuth 2.0 resource server: it never runs a login flow itself. The calling app (or its identity provider: Auth0, Okta, Entra ID, Keycloak, Cognito, ...) signs users in and sends their access token as `Authorization: Bearer <jwt>`.

Tokens are verified against the provider's JWKS (discovered from `{issuer}/.well-known/openid-configuration`, cached, refetched once on an unknown `kid`), with `iss`, `aud` and `exp` required, `nbf`/`iat` checked with a small leeway, and only the configured algorithms accepted (`none` never; HS256 only with a configured secret, for local development via `chatbot dev-token`).

Two kinds of caller follow:

- **API key**: a trusted backend. It may pass any `user_id` and use every route.
- **JWT**: an end user. Their `user_id` is the token's `sub` (`auth.jwt.user_id_claim`); passing a different one is a 403. They can chat and read, correct or delete their own memory. Ingestion, documents and jobs need an API key or a token with `auth.jwt.admin_scope`.

## Alternatives considered

API keys only, with the app vouching for user IDs (milestone 1); session cookies; running an authorization-code login flow in this service.

## Why

Front ends (SPA, mobile) can call the API directly with the token they already have, and the user ID can no longer be spoofed by the client, which ADR 008 left to the calling app. Keys stay for server-to-server calls.

## Consequences

The service needs outbound HTTPS to the identity provider's JWKS. Token revocation is only as fast as token expiry. Mapping provider-specific role claims to scopes is left to the template's owner (`chatbot/auth/jwt.py`).
