"""SQL record source against Postgres: a plain postgresql:// URL goes through psycopg 3."""

import pytest

from chatbot.ingestion.sources.sql import SqlSource, SqlSourceOptions

pytestmark = pytest.mark.integration


async def test_rows_from_postgres(db, pg_url):
    async with db.connection() as conn:
        await conn.execute("DROP TABLE IF EXISTS help_articles")
        await conn.execute(
            "CREATE TABLE help_articles (id int PRIMARY KEY, title text, body text, "
            "updated_at timestamptz DEFAULT now())"
        )
        await conn.execute(
            "INSERT INTO help_articles (id, title, body) VALUES "
            "(1, 'Refunds', 'Thirty days.'), (2, 'Exports', 'Markdown or PDF.')"
        )
    try:
        source = SqlSource(
            "articles",
            SqlSourceOptions(
                query="SELECT * FROM help_articles ORDER BY id",
                id_column="id",
                title_column="title",
                text_template="# {title}\n\n{body}",
                metadata_columns=["updated_at"],
            ),
            pg_url,
        )
        docs = [d async for d in source.iter_documents()]
        assert [d.title for d in docs] == ["Refunds", "Exports"]
        assert docs[0].content.decode() == "# Refunds\n\nThirty days."
        assert isinstance(docs[0].metadata["updated_at"], str)  # JSON-safe
    finally:
        async with db.connection() as conn:
            await conn.execute("DROP TABLE help_articles")
