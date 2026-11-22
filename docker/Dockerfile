FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" CHATBOT_ROOT=/app
WORKDIR /app

# Optional extras, e.g. --build-arg UV_EXTRAS="--extra presidio" plus
# --build-arg SPACY_MODEL_URL=<a spaCy model wheel URL> for Presidio PII detection.
ARG UV_EXTRAS=""
ARG SPACY_MODEL_URL=""

# Dependencies first, for layer caching.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project ${UV_EXTRAS}

COPY src ./src
COPY config ./config
COPY prompts ./prompts
COPY migrations ./migrations
COPY alembic.ini ./
COPY samples ./samples
COPY evals ./evals
RUN uv sync --frozen --no-dev ${UV_EXTRAS} \
    && if [ -n "$SPACY_MODEL_URL" ]; then uv pip install "$SPACY_MODEL_URL"; fi

RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app
EXPOSE 8000
CMD ["chatbot", "serve", "--host", "0.0.0.0", "--port", "8000"]
