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


class JwtSection(Strict):
    """Bearer tokens from any OIDC provider (Auth0, Okta, Entra ID, Keycloak, Cognito, ...)."""

    issuer: str | None = None  # https://login.example.com/ ; must equal the token's iss
    audience: str | None = None  # must appear in the token's aud
    jwks_url: str | None = (
        None  # default: discovered from {issuer}/.well-known/openid-configuration
    )
    algorithms: list[str] = ["RS256", "ES256"]
    hs256_secret_env: str | None = None  # shared-secret tokens (local dev, `chatbot dev-token`)
    user_id_claim: str = "sub"
    required_scopes: list[str] = []
    admin_scope: str = "chatbot:admin"  # lets a token act for any user and use ingestion routes
    leeway_s: int = Field(30, ge=0, le=300)
    jwks_cache_s: int = Field(3600, ge=60)

    @model_validator(mode="after")
    def _algorithms(self) -> JwtSection:
        allowed = {
            "RS256",
            "RS384",
            "RS512",
            "PS256",
            "PS384",
            "PS512",
            "ES256",
            "ES384",
            "ES512",
            "EdDSA",
            "HS256",
        }
        bad = [a for a in self.algorithms if a not in allowed]
        if bad:
            raise ValueError(f"unsupported JWT algorithms: {bad} (allowed: {sorted(allowed)})")
        if "HS256" in self.algorithms and not self.hs256_secret_env:
            raise ValueError("HS256 needs hs256_secret_env (the env var holding the secret)")
        return self


class AuthSection(Strict):
    # api_key_or_jwt: either credential works (backend services use keys, end users tokens).
    mode: Literal["none", "api_key", "jwt", "api_key_or_jwt"] = "api_key"
    api_keys_env: str = "CHATBOT_API_KEYS"
    jwt: JwtSection = JwtSection()

    @model_validator(mode="after")
    def _jwt_complete(self) -> AuthSection:
        if self.mode in ("jwt", "api_key_or_jwt"):
            if not self.jwt.audience:
                raise ValueError(f"auth.mode {self.mode} needs auth.jwt.audience")
            if not self.jwt.issuer:
                raise ValueError(f"auth.mode {self.mode} needs auth.jwt.issuer")
        return self


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


class QdrantHnsw(Strict):
    m: int = Field(16, ge=4)
    ef_construct: int = Field(100, ge=4)


class QdrantSection(Strict):
    """Qdrant as the vector store (ADR 024). Postgres still keeps the index catalog."""

    url_env: str = "QDRANT_URL"  # e.g. http://localhost:6333 ; unset -> url below
    url: str = "http://localhost:6333"
    api_key_env: str | None = "QDRANT_API_KEY"  # Qdrant Cloud; ignored when the var is unset
    location: str | None = None  # ":memory:" runs Qdrant's in-process local mode (tests)
    prefer_grpc: bool = False
    timeout_s: int = Field(30, ge=1)
    hnsw: QdrantHnsw = QdrantHnsw()
    hnsw_ef: int = Field(128, ge=1)  # search-time beam width, like pgvector's ef_search
    on_disk: bool = False  # keep vectors on disk (memmap) instead of RAM
    upsert_batch: int = Field(256, ge=1)


class VectorStoreSection(Strict):
    backend: str = "pgvector"  # pgvector | qdrant | any store you register
    index: str = Field("docs", pattern=r"^[a-z][a-z0-9_]{0,40}$")
    distance: Literal["cosine"] = "cosine"
    hnsw: HnswSection = HnswSection()
    exact_scan_threshold: int = 2000
    # Postgres text search config for BM25 (english, simple, german, ...). Recorded with an
    # index when it is created; changing it needs a new index (re-ingest), like the embedder.
    text_search_config: str = Field("english", pattern=r"^[a-z_]{1,40}$")
    qdrant: QdrantSection = QdrantSection()


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


class Bm25Options(Strict):
    k1: float = Field(1.2, ge=0.0, le=3.0)  # term-frequency saturation
    b: float = Field(0.75, ge=0.0, le=1.0)  # length normalisation


class HybridOptions(Strict):
    """Run several retrievers and fuse their rankings with reciprocal rank fusion (ADR 019)."""

    retrievers: list[str] = Field(["dense", "bm25"], min_length=2)
    candidates: int = Field(20, ge=1, le=200)  # hits each retriever returns before fusion
    rrf_k: int = Field(60, ge=1)
    weights: dict[str, float] = {}  # per retriever, default 1.0

    @model_validator(mode="after")
    def _distinct(self) -> HybridOptions:
        if len(set(self.retrievers)) != len(self.retrievers):
            raise ValueError("hybrid.retrievers must not repeat a retriever")
        unknown = set(self.weights) - set(self.retrievers)
        if unknown:
            raise ValueError(f"hybrid.weights names retrievers not in the list: {sorted(unknown)}")
        return self


class HydeOptions(Strict):
    """Embed a hypothetical answer instead of the bare question (one cheap-model call)."""

    enabled: bool = False
    prompt: str = "hyde/v1"
    model_role: Literal["answer", "cheap"] = "cheap"
    apply_to: list[str] = ["dense"]  # which retrievers get the hypothetical text
    include_query: bool = True  # embed "question + hypothetical answer", not the answer alone


class QueryRewriteOptions(Strict):
    """Rewrite a follow-up ("and in the US?") into a standalone search query."""

    enabled: bool = False
    prompt: str = "rewrite_query/v1"
    model_role: Literal["answer", "cheap"] = "cheap"
    only_with_history: bool = True  # skip the call on a conversation's first message
    max_chars: int = Field(400, ge=20)  # longer rewrites are ignored (the original is used)


class RerankOptions(Strict):
    enabled: bool = False
    model: str = "cohere:rerank"  # a name under rerankers in config/models.yaml
    candidates: int = Field(20, ge=1, le=200)  # retrieved hits handed to the reranker


class RetrievalSection(Strict):
    strategy: str = "dense"  # dense | bm25 | hybrid | any retriever you register
    top_k: int = Field(8, ge=1, le=100)
    bm25: Bm25Options = Bm25Options()
    hybrid: HybridOptions = HybridOptions()
    hyde: HydeOptions = HydeOptions()
    query_rewrite: QueryRewriteOptions = QueryRewriteOptions()
    rerank: RerankOptions = RerankOptions()
    # Which score min_score applies to. auto: rerank when reranking, else dense when a dense
    # retriever runs, else bm25. Never a fused RRF score: ranks say nothing about relevance.
    gate: Literal["auto", "dense", "bm25", "rerank"] = "auto"
    min_score: float = 0.35  # provisional until calibrated (ADR 011)
    min_hits: int = Field(1, ge=1)
    # Requests with filters search a slice of the corpus, so fewer chunks can corroborate an
    # answer: min_hits is lowered to this for them (min_score still applies). null: no change.
    min_hits_filtered: int | None = Field(1, ge=1)
    thresholds_from: Literal["index", "config"] = "index"

    def retriever_names(self) -> list[str]:
        return list(self.hybrid.retrievers) if self.strategy == "hybrid" else [self.strategy]

    def gate_signal(self) -> str:
        if self.gate != "auto":
            return self.gate
        if self.rerank.enabled:
            return "rerank"
        names = self.retriever_names()
        return "dense" if "dense" in names or len(names) > 1 else names[0]

    @model_validator(mode="after")
    def _consistent(self) -> RetrievalSection:
        names = self.retriever_names()
        if self.gate == "rerank" and not self.rerank.enabled:
            raise ValueError("retrieval.gate is rerank but retrieval.rerank.enabled is false")
        if self.gate in ("dense", "bm25") and self.gate not in names:
            raise ValueError(f"retrieval.gate {self.gate} is not one of the retrievers {names}")
        if self.hyde.enabled and not set(self.hyde.apply_to) & set(names):
            raise ValueError(
                f"retrieval.hyde.apply_to {self.hyde.apply_to} matches none of {names}"
            )
        return self


class AnswerSection(Strict):
    prompt: str = "answer/v2"
    clarify_prompt: str = "clarify/v1"
    max_context_chunks: int = 6
    citations: Literal["required", "optional"] = "required"
    on_weak_retrieval: Literal["clarify", "refuse"] = "clarify"
    stream_mode: Literal["draft_then_final", "buffered"] = "draft_then_final"


class SummaryOptions(Strict):
    """Rolling summary per conversation: turns that leave the history window are folded in."""

    enabled: bool = True
    prompt: str = "summarize/v1"
    refresh_after_turns: int = Field(4, ge=1)  # fold once this many turns have left the window
    max_chars: int = Field(1500, ge=200)


class LongTermOptions(Strict):
    """Facts about a user (preferences, context) kept across conversations, keyed by user_id."""

    enabled: bool = True
    prompt: str = "extract_facts/v1"
    max_facts_per_user: int = Field(50, ge=1)
    facts_in_prompt: int = Field(20, ge=0)
    max_fact_chars: int = Field(200, ge=20)


class MemorySection(Strict):
    backend: str = "postgres"
    history_turns: int = 10
    redact_before_persist: bool = True
    # Where summaries and fact extraction run: worker (a background job after each turn) or
    # inline (before the response returns; simpler, slower).
    run_in: Literal["worker", "inline"] = "worker"
    summary: SummaryOptions = SummaryOptions()
    long_term: LongTermOptions = LongTermOptions()


class InputGuards(Strict):
    max_chars: int = 4000
    pii_redaction: bool = True
    injection_check: bool = True


class ContextGuards(Strict):
    injection_check: bool = True


class OutputGuards(Strict):
    pii_redaction: bool = True
    prompt_leak_check: bool = True  # refuse answers that repeat the system prompt verbatim
    groundedness_check: bool = False  # judge the answer against its sources (one more call)


class GroundednessOptions(Strict):
    """How answers are judged against their sources: the output check and the eval metric."""

    judge: Literal["llm", "lexical"] = "llm"  # lexical: word overlap, offline and free
    model_role: Literal["answer", "cheap"] = "cheap"
    prompt: str = "judge_groundedness/v1"
    min_score: float = Field(0.7, ge=0.0, le=1.0)  # share of supported claims
    on_fail: Literal["clarify", "refuse", "warn"] = "clarify"
    on_error: Literal["allow", "block"] = "allow"  # if the judge call fails


class PresidioOptions(Strict):
    language: str = "en"
    spacy_model: str = "en_core_web_sm"  # en_core_web_lg is more accurate, ~400 MB
    entities: list[str] = [
        "PERSON",
        "LOCATION",
        "EMAIL_ADDRESS",
        "PHONE_NUMBER",
        "CREDIT_CARD",
        "IBAN_CODE",
        "US_SSN",
        "IP_ADDRESS",
    ]
    score_threshold: float = Field(0.5, ge=0.0, le=1.0)


class PiiSection(Strict):
    # Detectors run together and their spans are merged. regex: built in, no downloads.
    # presidio: names and addresses too (install the `presidio` extra and a spaCy model).
    detectors: list[str] = Field(["regex"], min_length=1)
    # Detectors for answers (output guard, streamed drafts, stored answers). Answers are built
    # from your own documents, where names, places and button labels are content: NER models
    # such as presidio mislabel them ("Request refund" -> "[PERSON] refund"). null: detectors.
    output_detectors: list[str] | None = Field(["regex"], min_length=1)
    allow: list[str] = []  # exact strings never redacted, e.g. your public support address
    presidio: PresidioOptions = PresidioOptions()


class LlmInjectionOptions(Strict):
    model_role: Literal["answer", "cheap"] = "cheap"
    prompt: str = "guard_injection/v1"
    stages: list[Literal["input", "context"]] = ["input"]  # one model call per checked text
    on_error: Literal["allow", "block"] = "allow"  # if the classifier call fails


class InjectionSection(Strict):
    # heuristic: built-in rules on normalised text. llm: a classifier prompt on the cheap model.
    detectors: list[str] = Field(["heuristic"], min_length=1)
    llm: LlmInjectionOptions = LlmInjectionOptions()


class GuardrailsSection(Strict):
    input: InputGuards = InputGuards()
    context: ContextGuards = ContextGuards()
    output: OutputGuards = OutputGuards()
    pii: PiiSection = PiiSection()
    injection: InjectionSection = InjectionSection()
    groundedness: GroundednessOptions = GroundednessOptions()
    stream_holdback_chars: int = Field(64, ge=16)


class CalibrationOptions(Strict):
    """How ``chatbot calibrate`` picks min_score and min_hits (ADR 011, ADR 021)."""

    target_answer_recall: float = Field(0.9, ge=0.0, le=1.0)  # answerable questions kept
    max_min_hits: int = Field(3, ge=1, le=10)  # min_hits values tried: 1..this
    min_unanswerable: int = Field(5, ge=1)  # fewer unanswerable questions -> warning


class EvalsSection(Strict):
    dataset: str = "evals/datasets/golden.jsonl"
    gate: str = "evals/gate.yaml"
    baselines_dir: str = "evals/baselines"
    hit_at: list[int] = [1, 3]  # hit@k reported besides hit@top_k
    faithfulness_judge: Literal["llm", "lexical", "off"] = "llm"
    concurrency: int = Field(4, ge=1, le=32)
    calibration: CalibrationOptions = CalibrationOptions()


class OtelOptions(Strict):
    """OpenTelemetry traces and metrics over OTLP/HTTP (ADR 026; `uv sync --extra otel`).

    Where they go is the standard OTel environment: OTEL_EXPORTER_OTLP_ENDPOINT,
    OTEL_EXPORTER_OTLP_HEADERS, OTEL_SERVICE_NAME, OTEL_RESOURCE_ATTRIBUTES.
    """

    enabled: bool = False
    service_name: str | None = None  # default: app.name (OTEL_SERVICE_NAME wins)
    traces: bool = True
    metrics: bool = True
    metrics_interval_s: float = Field(30.0, ge=1.0)
    sample_ratio: float = Field(1.0, ge=0.0, le=1.0)  # share of new traces kept
    # Prompts, answers and retrieved text on spans. Off: spans carry sizes, scores, models,
    # tokens and cost only. The text is the redacted text the guards produced either way.
    capture_text: bool = False
    console: bool = False  # also print finished spans to stdout (local debugging)


class LangfuseOptions(Strict):
    """Langfuse as a tracing backend, fed over its OTLP endpoint (ADR 027). Needs the same
    packages as otel (`uv sync --extra langfuse`), not the Langfuse SDK."""

    enabled: bool = False
    base_url_env: str = "LANGFUSE_BASE_URL"  # unset -> base_url below
    base_url: str = "https://cloud.langfuse.com"  # EU cloud; https://us.cloud.langfuse.com, ...
    public_key_env: str = "LANGFUSE_PUBLIC_KEY"
    secret_key_env: str = "LANGFUSE_SECRET_KEY"
    # Langfuse is for reading conversations, so text is on by default here. It is the
    # redacted text (guardrails run first), but it does leave your infrastructure and
    # forget_user cannot delete it there (ADR 015, ADR 027).
    capture_text: bool = True
    send_user_id: bool = True  # the caller's user_id as the Langfuse user


class UsageOptions(Strict):
    """Token and cost accounting (ADR 028). Prices come from price_per_mtok in models.yaml."""

    persist: bool = True  # one usage_records row per chat turn, job or CLI run


class ObservabilitySection(Strict):
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    log_message_text: bool = False
    otel: OtelOptions = OtelOptions()
    langfuse: LangfuseOptions = LangfuseOptions()
    usage: UsageOptions = UsageOptions()


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
    params: dict[str, Any] = {}  # provider options, e.g. Ollama's query/document prefixes
    price_per_mtok: dict[str, float | None] = {}


class RerankerProfile(Strict):
    provider: str  # cohere | cross_encoder | lexical | your own
    model: str
    api_key_env: str | None = None
    base_url: str | None = None
    params: dict[str, Any] = {}


class ModelCatalog(Strict):
    llm: dict[str, LLMProfile] = {}
    embeddings: dict[str, EmbeddingProfile] = {}
    rerankers: dict[str, RerankerProfile] = {}


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
    evals: EvalsSection = EvalsSection()
    observability: ObservabilitySection = ObservabilitySection()
    orchestrator: str = "simple"  # simple | langgraph (ADR 029) | any you register
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
        rerank = self.retrieval.rerank
        if rerank.enabled and rerank.model not in self.catalog.rerankers:
            raise ValueError(
                f"retrieval.rerank.model: '{rerank.model}' is not defined under rerankers "
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

    def reranker_profile(self) -> RerankerProfile:
        return self.catalog.rerankers[self.retrieval.rerank.model]

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
