FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" CHATBOT_ROOT=/app
WORKDIR /app

# Dependencies first, for layer caching.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY config ./config
COPY prompts ./prompts
COPY migrations ./migrations
COPY alembic.ini ./
COPY samples ./samples
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app
EXPOSE 8000
CMD ["chatbot", "serve", "--host", "0.0.0.0", "--port", "8000"]
