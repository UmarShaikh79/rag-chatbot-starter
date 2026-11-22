"""SQL record source: one row of a query becomes one document.

For knowledge that lives in your app's database (help articles, product records, FAQ rows)
rather than in files. Any database SQLAlchemy can reach works: Postgres out of the box
(``psycopg`` is already installed), MySQL, SQL Server, Oracle or SQLite with their driver
added (``uv add pymysql``, for example).

Configuration (``ingestion.sources``)::

    - name: articles
      type: sql
      options:
        url_env: APP_DATABASE_URL         # env var with the URL; default DATABASE_URL
        query: >
          SELECT id, title, body, category, updated_at
          FROM help_articles WHERE published
        id_column: id
        title_column: title               # optional
        text_columns: [title, body]       # joined with blank lines, or use text_template
        text_template: null               # e.g. "# {title}\\n\\n{body}"
        metadata_columns: [category]      # copied to chunk metadata, usable as filters
        user_id_column: null              # set when rows belong to one end user
        media_type: text/markdown         # text/plain | text/markdown | text/html
        uri_template: "https://app.example.com/help/{id}"   # optional

The query runs in full on every sync; documents whose text did not change are skipped by
content hash, and rows that disappeared are removed from the index (ADR 013). Rows are
fetched in batches of ``batch_size``. Give the query its own read-only database user:
the source only needs SELECT.
"""

from __future__ import annotations

import asyncio
import os
import queue
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chatbot.core.registry import registry
from chatbot.core.settings import SourceConfig
from chatbot.core.types import RawDocument

_DONE = object()


class SqlSourceOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url_env: str = "DATABASE_URL"
    query: str = Field(min_length=1)
    id_column: str
    title_column: str | None = None
    text_columns: list[str] = []
    text_template: str | None = None
    metadata_columns: list[str] = []
    user_id_column: str | None = None
    media_type: Literal["text/plain", "text/markdown", "text/html"] = "text/markdown"
    uri_template: str | None = None
    batch_size: int = Field(500, ge=1)

    @model_validator(mode="after")
    def _text(self) -> SqlSourceOptions:
        if not self.text_columns and not self.text_template:
            raise ValueError("set text_columns or text_template")
        return self


def sqlalchemy_url(url: str) -> str:
    """Plain ``postgresql://`` URLs use psycopg 3 (installed), not psycopg2 (not)."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


class SqlSource:
    def __init__(self, name: str, options: SqlSourceOptions, url: str) -> None:
        self.name = name
        self.options = options
        self.url = sqlalchemy_url(url)

    def _document(self, row: dict[str, Any]) -> RawDocument:
        o = self.options
        if o.text_template:
            text = o.text_template.format(**{k: "" if v is None else v for k, v in row.items()})
        else:
            text = "\n\n".join(str(row[c]) for c in o.text_columns if row.get(c) is not None)
        key = str(row[o.id_column])
        title = str(row[o.title_column]) if o.title_column and row.get(o.title_column) else None
        uri = o.uri_template.format(**row) if o.uri_template else f"sql://{self.name}/{key}"
        metadata = {c: row.get(c) for c in o.metadata_columns}
        # JSON-safe metadata: dates and decimals become strings.
        metadata = {
            k: v if isinstance(v, str | int | float | bool) or v is None else str(v)
            for k, v in metadata.items()
        }
        user_id = row.get(o.user_id_column) if o.user_id_column else None
        return RawDocument(
            id=f"{self.name}:{key}",
            source=self.name,
            uri=uri,
            title=title,
            media_type=o.media_type,
            content=text.encode("utf-8"),
            metadata=metadata,
            user_id=None if user_id is None else str(user_id),
        )

    def _produce(self, out: queue.Queue[Any], stop: threading.Event) -> None:
        from sqlalchemy import create_engine, text

        engine = create_engine(self.url)
        try:
            with engine.connect() as conn:
                result = conn.execution_options(stream_results=True).execute(
                    text(self.options.query)
                )
                for batch in result.mappings().partitions(self.options.batch_size):
                    if stop.is_set():
                        return
                    out.put([dict(r) for r in batch])
            out.put(_DONE)
        except Exception as exc:
            out.put(exc)
        finally:
            engine.dispose()

    async def iter_documents(self) -> AsyncIterator[RawDocument]:
        # SQLAlchemy's sync engine runs in a thread and hands over batches, so a large
        # table is streamed instead of loaded at once.
        out: queue.Queue[Any] = queue.Queue(maxsize=4)
        stop = threading.Event()
        worker = threading.Thread(target=self._produce, args=(out, stop), daemon=True)
        worker.start()
        try:
            while True:
                item = await asyncio.to_thread(out.get)
                if item is _DONE:
                    return
                if isinstance(item, Exception):
                    raise item
                for row in item:
                    if row.get(self.options.id_column) is None:
                        raise ValueError(
                            f"Source '{self.name}': a row has no {self.options.id_column}"
                        )
                    yield self._document(row)
        finally:
            stop.set()
            while worker.is_alive():  # unblock a producer waiting on a full queue
                try:
                    out.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.01)


@registry.register("source", "sql")
def make_sql_source(cfg: SourceConfig, root_dir: Path) -> SqlSource:
    options = SqlSourceOptions.model_validate(cfg.options)
    url = os.environ.get(options.url_env)
    if not url:
        raise ValueError(f"Source '{cfg.name}': set {options.url_env} to a database URL")
    return SqlSource(cfg.name, options, url)
