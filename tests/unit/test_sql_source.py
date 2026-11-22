"""SQL record source with SQLite (no server). Postgres URLs are covered by the e2e suite."""

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from chatbot.core.container import load_modules, make_source
from chatbot.ingestion.chunkers.recursive import RecursiveChunker
from chatbot.ingestion.pipeline import IngestionPipeline
from chatbot.ingestion.sources.sql import SqlSource, SqlSourceOptions, sqlalchemy_url
from chatbot.providers.embeddings.hashing import HashingEmbedder
from chatbot.testing.fakes import InMemoryVectorStore
from tests.conftest import offline_settings


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "app.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT, body TEXT, "
            "category TEXT, owner TEXT, published INTEGER)"
        )
        conn.executemany(
            "INSERT INTO articles VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, "Refunds", "Annual plans can be refunded within 30 days.", "billing", None, 1),
                (2, "Exports", "Export notes as Markdown or PDF.", "features", "u7", 1),
                (3, "Draft", "Not published yet.", "billing", None, 0),
            ],
        )
    return path


def settings_with(database: Path, **options):
    opts = {
        "url_env": "APP_DB",
        "query": "SELECT * FROM articles WHERE published = 1 ORDER BY id",
        "id_column": "id",
        "title_column": "title",
        "text_columns": ["title", "body"],
        "metadata_columns": ["category"],
        **options,
    }
    return offline_settings(
        ingestion={"sources": [{"name": "articles", "type": "sql", "options": opts}]}
    )


async def test_rows_become_documents(database, monkeypatch):
    monkeypatch.setenv("APP_DB", f"sqlite:///{database}")
    load_modules()
    source = make_source(settings_with(database, user_id_column="owner"), "articles")
    docs = [d async for d in source.iter_documents()]
    assert [d.id for d in docs] == ["articles:1", "articles:2"]
    first = docs[0]
    assert first.title == "Refunds" and first.media_type == "text/markdown"
    assert first.content.decode() == "Refunds\n\nAnnual plans can be refunded within 30 days."
    assert first.metadata == {"category": "billing"} and first.uri == "sql://articles/1"
    assert docs[1].user_id == "u7" and first.user_id is None


async def test_template_and_uri(database, monkeypatch):
    monkeypatch.setenv("APP_DB", f"sqlite:///{database}")
    load_modules()
    source = make_source(
        settings_with(
            database,
            text_columns=[],
            text_template="# {title}\n\n{body}",
            uri_template="https://app.example.com/help/{id}",
        ),
        "articles",
    )
    docs = [d async for d in source.iter_documents()]
    assert docs[0].content.decode().startswith("# Refunds\n\n")
    assert docs[1].uri == "https://app.example.com/help/2"


async def test_sync_removes_deleted_rows_and_skips_unchanged(database):
    options = SqlSourceOptions(
        query="SELECT * FROM articles WHERE published = 1",
        id_column="id",
        text_columns=["body"],
        batch_size=1,  # several batches through the thread hand-over
    )
    source = SqlSource("articles", options, f"sqlite:///{database}")
    pipeline = IngestionPipeline(
        InMemoryVectorStore(), HashingEmbedder(384), RecursiveChunker(), "docs"
    )
    first = await pipeline.sync_source(source)
    assert sorted(first.indexed) == ["articles:1", "articles:2"]
    with sqlite3.connect(database) as conn:
        conn.execute("DELETE FROM articles WHERE id = 2")
    second = await pipeline.sync_source(source)
    assert second.unchanged == ["articles:1"] and second.removed == ["articles:2"]


async def test_query_error_aborts_the_sync_without_deleting(database):
    store = InMemoryVectorStore()
    pipeline = IngestionPipeline(store, HashingEmbedder(384), RecursiveChunker(), "docs")
    good = SqlSource(
        "articles",
        SqlSourceOptions(query="SELECT * FROM articles", id_column="id", text_columns=["body"]),
        f"sqlite:///{database}",
    )
    await pipeline.sync_source(good)
    broken = SqlSource(
        "articles",
        SqlSourceOptions(query="SELECT * FROM missing", id_column="id", text_columns=["body"]),
        f"sqlite:///{database}",
    )
    with pytest.raises(Exception, match="missing"):
        await pipeline.sync_source(broken)
    index = await store.get_index("docs")
    assert len(await store.list_document_ids(index, "articles")) == 3


def test_options_are_validated(monkeypatch):
    with pytest.raises(ValidationError, match="text_columns or text_template"):
        SqlSourceOptions(query="SELECT 1", id_column="id")
    with pytest.raises(ValidationError, match="Extra inputs"):
        SqlSourceOptions(query="SELECT 1", id_column="id", text_columns=["a"], qurey="x")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    load_modules()
    settings = offline_settings(
        ingestion={
            "sources": [
                {
                    "name": "rows",
                    "type": "sql",
                    "options": {"query": "SELECT 1", "id_column": "id", "text_columns": ["a"]},
                }
            ]
        }
    )
    with pytest.raises(ValueError, match="set DATABASE_URL"):
        make_source(settings, "rows")


def test_postgres_urls_use_psycopg3():
    assert sqlalchemy_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert sqlalchemy_url("mysql+pymysql://u@h/db") == "mysql+pymysql://u@h/db"
