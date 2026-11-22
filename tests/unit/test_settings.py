import pytest

from chatbot.core.settings import ConfigError, env_overrides, load_settings
from tests.conftest import ROOT


def test_defaults_load():
    s = load_settings(root=ROOT, environ={})
    assert s.models.embed == "openai:text-embedding-3-small"
    assert s.embed_profile().dims == 1536
    assert s.retrieval.min_hits == 1
    assert s.answer.stream_mode == "draft_then_final"
    assert s.guardrails.input.pii_redaction and s.guardrails.input.injection_check


def test_unknown_key_fails_with_path():
    with pytest.raises(ConfigError) as err:
        load_settings(root=ROOT, environ={"CHATBOT__RETRIEVAL__TOPK": "3"})
    assert "retrieval.topk" in str(err.value)


def test_env_overrides_win_over_profile():
    s = load_settings(
        root=ROOT,
        profile="offline",
        environ={"CHATBOT__RETRIEVAL__TOP_K": "5", "CHATBOT__ANSWER__STREAM_MODE": "buffered"},
    )
    assert s.retrieval.top_k == 5
    assert s.answer.stream_mode == "buffered"
    assert s.models.embed == "hashing:384"  # from the profile


def test_unknown_model_name_fails():
    with pytest.raises(ConfigError, match=r"models\.answer"):
        load_settings(root=ROOT, environ={"CHATBOT__MODELS__ANSWER": "nope:model"})


def test_overlap_must_be_smaller_than_size():
    with pytest.raises(ConfigError, match="overlap_tokens"):
        load_settings(root=ROOT, environ={"CHATBOT__INGESTION__CHUNKER__OVERLAP_TOKENS": "600"})


def test_missing_profile_fails():
    with pytest.raises(ConfigError, match="Profile"):
        load_settings(root=ROOT, profile="nope", environ={})


def test_env_override_parsing():
    tree = env_overrides({"CHATBOT__A__B": "true", "CHATBOT__A__C": "1.5", "OTHER": "x"})
    assert tree == {"a": {"b": True, "c": 1.5}}


# ---------------------------------------------------------------- API keys


def test_required_credentials_follow_the_configured_models():
    dev = load_settings(root=ROOT, profile="dev", environ={})
    assert dev.required_credentials() == {
        "OPENAI_API_KEY": [
            "models.answer (openai:answer)",
            "models.cheap (openai:cheap)",
            "models.embed (openai:text-embedding-3-small)",
        ]
    }
    assert load_settings(root=ROOT, profile="offline", environ={}).required_credentials() == {}
    assert load_settings(root=ROOT, profile="local-ollama", environ={}).required_credentials() == {}
    rerank = load_settings(
        root=ROOT, profile="offline", environ={"CHATBOT__RETRIEVAL__RERANK__ENABLED": "true"}
    )
    assert rerank.required_credentials() == {"COHERE_API_KEY": ["retrieval.rerank (cohere:rerank)"]}


def test_missing_keys_are_one_readable_error():
    s = load_settings(
        root=ROOT, profile="dev", environ={"CHATBOT__RETRIEVAL__RERANK__ENABLED": "true"}
    )
    with pytest.raises(ConfigError) as err:
        s.check_credentials({"CHATBOT_PROFILE": "dev", "OPENAI_API_KEY": "  "})
    message = str(err.value)
    assert "Missing API key(s) for the 'dev' profile" in message
    assert "OPENAI_API_KEY  needed by models.answer" in message
    assert "COHERE_API_KEY  needed by retrieval.rerank" in message
    assert "CHATBOT_PROFILE=offline" in message
    s.check_credentials({"OPENAI_API_KEY": "sk-x", "COHERE_API_KEY": "c"})  # no error


def test_openai_clients_need_a_key(monkeypatch):
    pytest.importorskip("openai")
    from chatbot.providers.embeddings.openai import OpenAIEmbedder
    from chatbot.providers.llm.openai import OpenAILLM

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    s = load_settings(root=ROOT, profile="dev", environ={})
    with pytest.raises(ConfigError, match="Set OPENAI_API_KEY for the OpenAI model"):
        OpenAILLM(s.llm_profile("answer"))
    with pytest.raises(ConfigError, match="Set OPENAI_API_KEY"):
        OpenAIEmbedder(s.embed_profile())


@pytest.mark.parametrize("command", ["serve", "worker", "eval"])
def test_cli_reports_a_missing_key_without_a_traceback(command, monkeypatch, capsys):
    """Regression: these crashed with the OpenAI SDK's traceback ("Missing credentials")."""
    from chatbot import cli

    monkeypatch.setenv("CHATBOT_PROFILE", "dev")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cli, "get_settings", load_settings)  # not the cached copy
    with pytest.raises(SystemExit) as exc:
        cli.main([command])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("Missing API key(s) for the 'dev' profile")
    assert "Traceback" not in err


def test_serve_checks_database_url_and_api_keys_before_starting(monkeypatch, capsys):
    from chatbot import cli

    def serve_error(**env) -> str:
        for name in ("DATABASE_URL", "CHATBOT_API_KEYS", "OPENAI_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("CHATBOT_PROFILE", "offline")
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        with pytest.raises(SystemExit):
            cli.main(["serve"])
        return capsys.readouterr().err

    monkeypatch.setattr(cli, "get_settings", load_settings)
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: pytest.fail("uvicorn started"))
    assert serve_error().startswith("Set DATABASE_URL to a Postgres URL")
    err = serve_error(DATABASE_URL="postgresql://u:p@db:5432/x")
    assert err.startswith("auth.mode is api_key but CHATBOT_API_KEYS is empty")
    assert "Traceback" not in err


def test_index_unalias_refuses_the_configured_alias(monkeypatch, capsys):
    from types import SimpleNamespace

    from chatbot import cli

    removed = []

    class Store:  # an IndexAdmin store, enough for the command
        async def create_index(self, *a, **k): ...
        async def mark_ready(self, *a, **k): ...
        async def activate(self, *a, **k): ...
        async def rollback(self, *a, **k): ...
        async def drop_index(self, *a, **k): ...
        async def list_indexes(self): ...
        async def resolve_any(self, *a): ...
        async def iter_documents(self, *a): ...

        async def remove_alias(self, alias):
            removed.append(alias)
            return ["docs_offline__hashing_v1__384"]

    async def close():
        return None

    async def components(role="cli"):
        settings = load_settings(root=ROOT, profile="offline", environ={})
        return SimpleNamespace(store=Store(), settings=settings, close=close)

    monkeypatch.setattr(cli, "_components", components)
    with pytest.raises(SystemExit) as exc:
        cli.main(["index", "unalias", "docs_offline"])
    assert exc.value.code == 2 and removed == []
    assert "the alias this configuration serves" in capsys.readouterr().err

    cli.main(["index", "unalias", "scratch"])
    assert removed == ["scratch"]
    assert '"removed": "scratch"' in capsys.readouterr().out
    cli.main(["index", "unalias", "docs_offline", "--force"])
    assert removed == ["scratch", "docs_offline"]
