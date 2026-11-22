"""Configuration loading: config/app.yaml, then a profile, then CHATBOT__* env vars.

Every model forbids unknown keys, so a typo such as ``retrieval.topk`` fails at startup
with the exact path instead of silently using a default.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ENV_PREFIX = "CHATBOT__"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- app.yaml sections


class AppSection(Strict):
    name: str = "rag-chatbot"
    env: Literal["dev", "prod"] = "dev"


class AuthSection(Strict):
    mode: Literal["none", "api_key"] = "api_key"
    api_keys_env: str = "CHATBOT_API_KEYS"


class ModelsSection(Strict):
    answer: str
    cheap: str
    embed: str


class DatabaseSection(Strict):
    url_env: str = "DATABASE_URL"
    pool_min: int = 1
    pool_max: int = 10


class HnswSection(Strict):
    m: int = 16
    ef_construction: int = 64
    ef_search: int = Field(100, ge=1, le=1000)
    iterative_scan: Literal["off", "strict_order", "relaxed_order"] = "relaxed_order"
    max_scan_tuples: int = 20000


class VectorStoreSection(Strict):
    backend: str = "pgvector"
    index: str = Field("docs", pattern=r"^[a-z][a-z0-9_]{0,40}$")
    distance: Literal["cosine"] = "cosine"
    hnsw: HnswSection = HnswSection()
    exact_scan_threshold: int = 2000


class SourceConfig(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,40}$")
    type: str = "files"
    path: str | None = None
    include: list[str] = ["*.pdf", "*.md", "*.docx", "*.html", "*.txt"]
    options: dict[str, Any] = {}


class ChunkerSection(Strict):
    type: str = "recursive"
    size_tokens: int = Field(512, ge=32)
    overlap_tokens: int = Field(64, ge=0)
    tokenizer: Literal["approx", "tiktoken"] = "approx"

    @model_validator(mode="after")
    def _overlap_smaller(self) -> ChunkerSection:
        if self.overlap_tokens >= self.size_tokens:
            raise ValueError("overlap_tokens must be smaller than size_tokens")
        return self


class WorkerSection(Strict):
    concurrency: int = Field(2, ge=1)
    max_attempts: int = Field(3, ge=1)
    poll_interval_s: float = 1.0
    lock_timeout_s: int = 600


class IngestionSection(Strict):
    sources: list[SourceConfig] = []
    chunker: ChunkerSection = ChunkerSection()
    embed_batch_size: int = 64
    worker: WorkerSection = WorkerSection()


class RetrievalSection(Strict):
    strategy: str = "dense"
    top_k: int = Field(8, ge=1, le=100)
    min_score: float = 0.35
    min_hits: int = Field(1, ge=1)
    thresholds_from: Literal["index", "config"] = "index"


class AnswerSection(Strict):
    prompt: str = "answer/v1"
    clarify_prompt: str = "clarify/v1"
    max_context_chunks: int = 6
    citations: Literal["required", "optional"] = "required"
    on_weak_retrieval: Literal["clarify", "refuse"] = "clarify"
    stream_mode: Literal["draft_then_final", "buffered"] = "draft_then_final"


class MemorySection(Strict):
    backend: str = "postgres"
    history_turns: int = 10
    redact_before_persist: bool = True


class InputGuards(Strict):
    max_chars: int = 4000
    pii_redaction: bool = True
    injection_check: bool = True


class ContextGuards(Strict):
    injection_check: bool = True


class OutputGuards(Strict):
    pii_redaction: bool = True


class GuardrailsSection(Strict):
    input: InputGuards = InputGuards()
    context: ContextGuards = ContextGuards()
    output: OutputGuards = OutputGuards()
    stream_holdback_chars: int = Field(64, ge=16)


class ObservabilitySection(Strict):
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    log_message_text: bool = False


# ---------------------------------------------------------------- models.yaml


class LLMProfile(Strict):
    provider: str
    model: str
    api_key_env: str | None = None
    base_url: str | None = None
    params: dict[str, Any] = {}
    price_per_mtok: dict[str, float | None] = {}


class EmbeddingProfile(Strict):
    provider: str
    model: str
    dims: int
    api_key_env: str | None = None
    base_url: str | None = None
    price_per_mtok: dict[str, float | None] = {}


class ModelCatalog(Strict):
    llm: dict[str, LLMProfile] = {}
    embeddings: dict[str, EmbeddingProfile] = {}


# ---------------------------------------------------------------- root


class Settings(Strict):
    app: AppSection = AppSection()
    auth: AuthSection = AuthSection()
    models: ModelsSection
    database: DatabaseSection = DatabaseSection()
    vector_store: VectorStoreSection = VectorStoreSection()
    ingestion: IngestionSection = IngestionSection()
    retrieval: RetrievalSection = RetrievalSection()
    answer: AnswerSection = AnswerSection()
    memory: MemorySection = MemorySection()
    guardrails: GuardrailsSection = GuardrailsSection()
    observability: ObservabilitySection = ObservabilitySection()
    orchestrator: str = "simple"
    catalog: ModelCatalog = ModelCatalog()
    # Not from YAML: where config and prompts were loaded from.
    root_dir: Path = Path(".")

    @model_validator(mode="after")
    def _models_exist(self) -> Settings:
        for role in ("answer", "cheap"):
            name = getattr(self.models, role)
            if name not in self.catalog.llm:
                raise ValueError(f"models.{role}: '{name}' is not defined under llm in models.yaml")
        if self.models.embed not in self.catalog.embeddings:
            raise ValueError(
                f"models.embed: '{self.models.embed}' is not defined under embeddings "
                "in models.yaml"
            )
        names = [s.name for s in self.ingestion.sources]
        if len(names) != len(set(names)):
            raise ValueError("ingestion.sources: source names must be unique")
        return self

    def llm_profile(self, role: str) -> LLMProfile:
        return self.catalog.llm[getattr(self.models, role)]

    def embed_profile(self) -> EmbeddingProfile:
        return self.catalog.embeddings[self.models.embed]

    def source(self, name: str) -> SourceConfig:
        for s in self.ingestion.sources:
            if s.name == name:
                return s
        raise KeyError(name)

    def database_url(self) -> str:
        url = os.environ.get(self.database.url_env)
        if not url:
            raise RuntimeError(f"Set {self.database.url_env} to a Postgres URL")
        return url


class ConfigError(RuntimeError):
    """Raised with a readable message when configuration is invalid."""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def env_overrides(environ: dict[str, str]) -> dict[str, Any]:
    """CHATBOT__RETRIEVAL__TOP_K=5 -> {"retrieval": {"top_k": 5}}. Values parse as YAML."""
    tree: dict[str, Any] = {}
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = [p.lower() for p in key[len(ENV_PREFIX) :].split("__") if p]
        if not path:
            continue
        node = tree
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = yaml.safe_load(raw) if raw != "" else ""
    return tree


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


def find_root(start: Path | None = None) -> Path:
    env_root = os.environ.get("CHATBOT_ROOT")
    if env_root:
        return Path(env_root).resolve()
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "config" / "app.yaml").exists():
            return candidate
    return here


def load_settings(
    root: Path | None = None,
    profile: str | None = None,
    environ: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    environ = dict(os.environ if environ is None else environ)
    root = (root or find_root()).resolve()
    config_dir = root / "config"
    data = _read_yaml(config_dir / "app.yaml")
    profile = profile or environ.get("CHATBOT_PROFILE") or "dev"
    profile_path = config_dir / "profiles" / f"{profile}.yaml"
    if profile != "dev" and not profile_path.exists():
        raise ConfigError(f"Profile '{profile}' not found at {profile_path}")
    data = deep_merge(data, _read_yaml(profile_path))
    data = deep_merge(data, env_overrides(environ))
    if overrides:
        data = deep_merge(data, overrides)
    data["catalog"] = _read_yaml(config_dir / "models.yaml")
    data["root_dir"] = root
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "(root)"
            lines.append(f"  {loc}: {err['msg']}")
        raise ConfigError("Invalid configuration:\n" + "\n".join(lines)) from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
