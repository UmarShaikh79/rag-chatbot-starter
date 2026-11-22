"""What ``chatbot eval`` and ``chatbot calibrate`` do, as functions you can call from tests
or your own scripts with any set of components."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from chatbot.core.container import Components
from chatbot.core.errors import ChatbotError
from chatbot.core.interfaces import CalibrationStore
from chatbot.core.registry import registry
from chatbot.core.types import Calibration
from chatbot.evals.calibrate import CalibrationResult, sweep
from chatbot.evals.dataset import dataset_fingerprint, load_dataset
from chatbot.evals.runner import EvalReport, run_answers, run_retrieval, summarize


def _dataset_path(c: Components, dataset: str | None) -> Path:
    path = Path(dataset or c.settings.evals.dataset)
    return path if path.is_absolute() else c.settings.root_dir / path


def faithfulness_judge(c: Components) -> Any:
    kind = c.settings.evals.faithfulness_judge
    if kind == "off":
        return None
    gcfg = c.settings.guardrails.groundedness
    llms = {"answer": c.answer_llm, "cheap": c.cheap_llm}
    return registry.create("groundedness_judge", kind, gcfg, llms, c.prompts)


async def evaluate(
    c: Components, dataset: str | None = None, retrieval_only: bool = False
) -> EvalReport:
    path = _dataset_path(c, dataset)
    items = load_dataset(path)
    cfg = c.settings
    index = await c.retriever.index() if hasattr(c.retriever, "index") else None
    if index is None:
        raise ChatbotError("the index is empty: run `chatbot ingest` before `chatbot eval`")
    texts: dict[str, str] = {}
    retrieval = await run_retrieval(
        c.retriever, items, cfg.retrieval.top_k, cfg.evals.concurrency, texts
    )
    answers = []
    if not retrieval_only:
        answers = await run_answers(
            c.orchestrator, faithfulness_judge(c), items, cfg.evals.concurrency, texts
        )
    calibration = None
    if hasattr(c.retriever, "calibration"):
        calibration = await c.retriever.calibration(index)
    return EvalReport(
        summary=summarize(retrieval, answers, cfg.retrieval.top_k, cfg.evals.hit_at),
        retrieval=retrieval,
        answers=answers,
        meta={
            "dataset": dataset_fingerprint(path),
            "index": index.name,
            "embedding_model": index.embedding_model,
            "scorer": getattr(c.retriever, "scorer", None),
            "calibrated": calibration is not None,
            "answer_model": c.answer_llm.model_id,
            "faithfulness_judge": cfg.evals.faithfulness_judge,
        },
    )


async def calibrate(
    c: Components, dataset: str | None = None, apply: bool = False
) -> tuple[CalibrationResult, Calibration]:
    path = _dataset_path(c, dataset)
    items = load_dataset(path)
    cfg = c.settings
    index = await c.retriever.index() if hasattr(c.retriever, "index") else None
    if index is None:
        raise ChatbotError("the index is empty: run `chatbot ingest` before `chatbot calibrate`")
    rows = await run_retrieval(c.retriever, items, cfg.retrieval.top_k, cfg.evals.concurrency)
    opts = cfg.evals.calibration
    result = sweep(
        rows,
        cfg.answer.max_context_chunks,
        opts.target_answer_recall,
        opts.max_min_hits,
        opts.min_unanswerable,
    )
    calibration = Calibration(
        scorer=getattr(c.retriever, "scorer", "dense|dense"),
        min_score=result.best.min_score,
        min_hits=result.best.min_hits,
        metrics={
            "answer_recall": result.best.answer_recall,
            "abstention": result.best.abstention,
            "margin": result.best.margin,
            "met_target": result.met_target,
            "target_answer_recall": result.target_answer_recall,
            "n_answerable": result.n_answerable,
            "n_unanswerable": result.n_unanswerable,
            "top_k": cfg.retrieval.top_k,
        },
        dataset=dataset_fingerprint(path),
    )
    if apply:
        if not isinstance(c.store, CalibrationStore):
            raise ChatbotError(f"{type(c.store).__name__} cannot store calibrations")
        await c.store.save_calibration(index, calibration)
    return result, calibration
