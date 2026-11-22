import pytest

from chatbot.core import container
from chatbot.core.prompts import PromptRegistry, PromptTemplate
from chatbot.core.registry import Registry, RegistryError, registry
from tests.conftest import ROOT


def test_registry_lists_known_names_on_error():
    r = Registry()
    r.add("retriever", "dense", lambda: "x")
    assert r.create("retriever", "dense") == "x"
    with pytest.raises(RegistryError, match="known: dense"):
        r.get("retriever", "bm25")


def test_builtins_register():
    container.load_modules()
    assert {"openai", "fake"} <= set(registry.names("llm"))
    assert {"openai", "hashing"} <= set(registry.names("embedder"))
    assert "pgvector" in registry.names("vector_store")
    assert {"markdown", "html", "pdf", "docx"} <= set(registry.names("parser"))


def test_every_prompt_renders():
    prompts = PromptRegistry(ROOT / "prompts")
    found = prompts.all()
    assert {p.ref for p in found} >= {"answer/v1", "clarify/v1"}
    for p in found:
        messages = p.render(**{v: "x" for v in p.variables})
        assert messages[0].role == "system" and messages[1].role == "user"


def test_prompt_render_is_strict():
    p = PromptTemplate(name="t", version=1, variables=["a"], system="{a}", user="{b}")
    with pytest.raises(KeyError, match="undeclared"):
        p.render(a=1)
    with pytest.raises(KeyError, match="missing"):
        PromptTemplate(name="t", version=1, variables=["a"], system="{a}", user="").render()


def test_system_literals_leave_out_filled_in_values():
    prompts = PromptRegistry(ROOT / "prompts")
    clarify = prompts.get("clarify/v1")
    parts = clarify.system_literals()
    assert parts and all("{" not in p for p in parts)
    rendered = clarify.render(question="q", history="h", memory="m", topics="Alpha, Beta")
    assert "Alpha, Beta" in rendered[0].content
    assert not any("Alpha" in p for p in parts)
    answer = prompts.get("answer/v2")  # no placeholders in its system text: one part
    assert answer.system_literals() == [answer.system.strip()]
