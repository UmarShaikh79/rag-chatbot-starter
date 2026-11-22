"""The re-index workflow through the command line, as an operator runs it (ADR 023)."""

import json
import shutil
from pathlib import Path

import pytest

from chatbot.cli import main
from chatbot.core.settings import get_settings
from tests.conftest import ROOT

pytestmark = pytest.mark.e2e


@pytest.fixture
def cli(db, pg_url, tmp_path: Path, monkeypatch, capsys):
    docs = tmp_path / "docs"
    shutil.copytree(ROOT / "samples" / "docs", docs)
    monkeypatch.setenv("CHATBOT_ROOT", str(ROOT))
    monkeypatch.setenv("CHATBOT_PROFILE", "offline")
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv(
        "CHATBOT__INGESTION__SOURCES",
        json.dumps([{"name": "samples", "type": "files", "path": str(docs)}]),
    )

    def run(*args: str, env: dict[str, str] | None = None):
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        capsys.readouterr()
        try:
            main(list(args))
            code = 0
        except SystemExit as exc:
            code = exc.code or 0
        finally:
            for key in env or {}:
                monkeypatch.delenv(key)
            get_settings.cache_clear()
        out = capsys.readouterr()
        return code, out.out, out.err

    return run


def test_reindex_activate_rollback_drop(cli):
    assert cli("ingest", "--inline")[0] == 0
    smaller = {
        "CHATBOT__INGESTION__CHUNKER__SIZE_TOKENS": "128",
        "CHATBOT__INGESTION__CHUNKER__OVERLAP_TOKENS": "16",
    }
    code, out, err = cli("reindex", env=smaller)
    assert code == 0, err
    built = json.loads(out)
    assert built["ready"] and not built["activated"] and built["summary"]["indexed"] == 8
    assert "chatbot index activate" in err

    code, out, _ = cli("index", "list")
    indexes = {i["name"]: i for i in json.loads(out)}
    assert indexes[built["new_index"]]["aliases"] == ["docs_offline_next"]
    assert indexes[built["old_index"]]["aliases"] == ["docs_offline"]

    code, out, err = cli("index", "activate", env=smaller)
    assert code == 0, err
    assert json.loads(out)["name"] == built["new_index"]
    code, out, err = cli("ask", "How long is the refund window for annual plans?", env=smaller)
    assert code == 0, err
    assert json.loads(out)["kind"] == "answer"

    code, _, err = cli("index", "drop", built["new_index"])
    assert code == 2 and "in use" in err
    assert cli("index", "rollback")[0] == 0
    code, out, _ = cli("index", "drop", built["new_index"])
    assert code == 0 and json.loads(out) == {"dropped": built["new_index"]}
