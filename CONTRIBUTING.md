# Contributing

Thank you for helping. This template is meant for everyone who wants to build with AI: students,
developers new to RAG, and experienced engineers. Contributions of every size are welcome: a
typo, a clearer sentence, a test, a bug fix or a new feature.

New here? Start with an issue labelled `good first issue`, or fix something in the docs that
confused you. That is a real contribution: if it confused you, it confuses others.

## Set up

You need Python 3.12+, [uv](https://docs.astral.sh/uv/) and Docker (for Postgres).

```bash
git clone https://github.com/UmarShaikh79/rag-chatbot-starter.git
cd rag-chatbot-starter
make install                 # uv sync
cp .env.example .env         # every variable is listed and explained there
make up                      # Postgres, migrations, API and worker in Docker
```

No API key? Set `CHATBOT_PROFILE=offline` in `.env`: a hashing embedder and an extractive fake
model run the whole pipeline without network access.

## Run the checks

```bash
make lint                    # ruff check and format check
make test-unit               # fast, no database
make test-db                 # integration and end-to-end tests against the Compose Postgres
make eval                    # the golden-set gate, as CI runs it (offline profile)
```

`uv sync --all-extras` installs the optional parts (OpenTelemetry, Qdrant, LangGraph, Presidio,
...) so their tests run too. Optional: `pre-commit install` runs ruff on every commit.

## Send a change

1. Open an issue first for anything bigger than a small fix, so we can agree on the approach.
2. Branch from `main`, keep the change focused, and add a test that fails without it.
3. Update the docs page the change affects, and add a line to `CHANGELOG.md` under
   "Unreleased" (with "In a copy you have modified: ..." when people who copied the template
   need to do something).
4. A real design decision gets an ADR in `docs/adr/` (copy an existing one).
5. Open a pull request. CI must be green: lint, tests, the base install without extras, the
   Kubernetes manifests and the eval gate.

## Code style for a mixed audience

Someone reading this code may be writing their first Python service. Write for them:

- **One decision per line.** Name a condition before you test it
  (`token_looks_like_jwt = ...`) instead of mixing `and` and `or` in one `if`.
- **No nested conditional expressions** (`a if x else b if y else c`): use `if` / `elif`.
- **One loop per comprehension.** Two loops or a comprehension inside another become a plain
  loop with named variables.
- **Say why, not what.** A short comment for anything clever: a context variable, a lock, a
  threshold, a retry.
- **Keep parts optional.** New dependencies go in a `uv` extra; the base install must still
  pass its tests (CI `test-base`).
- **Config, not code.** Names of models and secrets live in `config/` and `.env`, never in
  application code.

`ruff` (line length 100) settles formatting; there is nothing to argue about.

## Where things go

`docs/extending.md` shows how to add a source, parser, retriever, reranker, detector, guard,
provider, vector store or job kind. Your own additions can live in `src/chatbot/domain/`, which
is imported last, so its registrations win.

## Reporting security issues

Please don't open a public issue. See [SECURITY.md](SECURITY.md).

## Code of conduct

Everyone taking part agrees to the [Code of Conduct](CODE_OF_CONDUCT.md).
