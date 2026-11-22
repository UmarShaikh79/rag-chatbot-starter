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
