# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Report it privately through
GitHub: on the repository's **Security** tab, choose **Report a vulnerability** (private
vulnerability reporting). Only the maintainer sees the report.

Include what you found, how to reproduce it, and what an attacker could do with it. You should
get a first answer within a week. Once a fix is released, you are credited in the CHANGELOG
unless you'd rather not be.

## Supported versions

This is a template you copy and own, so fixes land on `main` and in the next release; there are
no backports. If you copied an older version, compare with the CHANGELOG: security fixes say
what to change in a copy you have modified.

## What is in scope

The code in this repository: the API, authentication (API keys, JWT/OIDC), guardrails (PII
redaction, prompt-injection checks), the ingestion and job pipeline, the Docker image and the
Kubernetes example. Prompt injection that makes a model say something it shouldn't is a known
limit of language models; report it when a guard you'd expect to stop it doesn't.

## Using it safely

- Keep secrets in `.env` or your secret manager, never in `config/` or git.
- Keep `auth.mode` on `api_key` or a JWT mode in any shared environment; `none` is for a
  laptop.
- List exact origins in `api.cors.allow_origins`; avoid `"*"` in production.
- Treat retrieved documents as untrusted input: the injection checks reduce risk, they don't
  remove it.
