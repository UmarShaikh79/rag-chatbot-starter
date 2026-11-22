"""Eval runner, metrics, calibration sweep and CI gate, on in-memory components."""

import json

import pytest

from chatbot.evals import metrics as m
from chatbot.evals.calibrate import sweep
from chatbot.evals.commands import calibrate, evaluate
from chatbot.evals.dataset import GoldenItem, load_dataset
from chatbot.evals.gate import GateRules, check, load_rules
from chatbot.evals.runner import RetrievalRow, RetrievedHit
from tests.conftest import ROOT
from tests.unit.test_retrieval_quality import build


def test_document_level_metrics():
    docs = ["a", "a", "b", "c"]
    assert m.hit_at(docs, ["b"], 1) == 0.0 and m.hit_at(docs, ["b"], 2) == 1.0
    assert m.recall_at(docs, ["b", "z"], 3) == 0.5
    assert m.reciprocal_rank(docs, ["c"]) == pytest.approx(1 / 3)
    assert m.includes_all("Refunds take 5 to 10 Business Days", ["business days"]) == 1.0
    assert m.citation_precision(["a", "b"], ["a"]) == 0.5
    assert m.percentile([1, 2, 3, 4, 100], 0.5) == 3


def test_golden_dataset_is_valid_and_balanced():
    items = load_dataset(ROOT / "evals/datasets/golden.jsonl")
    assert len(items) >= 30
    assert sum(not i.answerable for i in items) >= 10  # enough to measure abstention


def test_dataset_rejects_inconsistent_items(tmp_path):
    with pytest.raises(ValueError, match="expected_documents"):
        GoldenItem(id="x", question="q", answerable=True)
    bad = tmp_path / "d.jsonl"
    line = json.dumps({"id": "a", "question": "q", "answerable": False})
    bad.write_text(line + "\n" + line + "\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_dataset(bad)


def row(qid: str, answerable: bool, scores: list[float], expected_first: bool = True):
    hits = [
        RetrievedHit(
            document_id="good" if (i == 0 and expected_first) else f"other{i}",
            chunk_id=f"{qid}-{i}",
            score=s,
            gate_score=s,
        )
        for i, s in enumerate(scores)
    ]
    return RetrievalRow(
        id=qid, answerable=answerable, expected=["good"] if answerable else [], hits=hits
    )


def test_sweep_separates_answerable_from_unanswerable():
    rows = [
        row("a1", True, [0.8, 0.3]),
        row("a2", True, [0.7, 0.6]),
        row("a3", True, [0.65, 0.2]),
        row("u1", False, [0.4, 0.3]),
        row("u2", False, [0.35, 0.1]),
    ]
    result = sweep(rows, max_context=4, target_answer_recall=1.0, min_unanswerable=2)
    best = result.best
    assert best.answer_recall == 1.0 and best.abstention == 1.0 and best.min_hits == 1
    assert 0.4 < best.min_score < 0.65  # midway between the score groups
    assert result.met_target and not result.warnings


def test_sweep_uses_min_hits_when_one_strong_hit_is_not_enough():
    # Unanswerable questions have one high-scoring stray chunk; answerable ones have two.
    rows = [
        row("a1", True, [0.8, 0.75]),
        row("a2", True, [0.7, 0.72]),
        row("u1", False, [0.9, 0.1]),
        row("u2", False, [0.85, 0.2]),
    ]
    result = sweep(rows, max_context=4, target_answer_recall=1.0, min_unanswerable=2)
    assert result.best.min_hits == 2 and result.best.abstention == 1.0


def test_sweep_warns_when_target_is_out_of_reach():
    rows = [row("a1", True, [0.8], expected_first=False), row("u1", False, [0.2])]
    result = sweep(rows, max_context=4, target_answer_recall=0.9, min_unanswerable=5)
    assert not result.met_target
    assert any("retrieval misses" in w for w in result.warnings)
    assert any("only 1 unanswerable" in w for w in result.warnings)


def test_gate_floors_ceilings_and_baseline_drop():
    rules = GateRules(min={"retrieval.hit@3": 0.9, "answer.faithfulness": 0.8}, max={"errors": 0})
    rules = rules.model_copy(update={"max_drop": 0.05})
    ok = check({"retrieval.hit@3": 0.95, "errors": 0}, rules, {"retrieval.hit@3": 0.97})
    assert ok.passed and ok.skipped == ["answer.faithfulness"]
    dropped = check({"retrieval.hit@3": 0.91, "errors": 0}, rules, {"retrieval.hit@3": 1.0})
    assert not dropped.passed and "baseline" in dropped.failures[0]
    low = check({"retrieval.hit@3": 0.5, "errors": 2}, rules)
    assert len(low.failures) == 2


def test_committed_gate_covers_every_profile():
    for profile in ("offline", "dev", "prod"):
        assert load_rules(ROOT / "evals/gate.yaml", profile).min


async def test_evaluate_scores_retrieval_and_answers():
    c = await build()
    report = await evaluate(c)
    s = report.summary
    assert s["count.questions"] == len(report.retrieval) == len(report.answers)
    assert s["retrieval.hit@3"] >= 0.95
    assert s["answer.faithfulness"] >= 0.9  # the extractive fake quotes its sources
    assert 0.0 <= s["abstention.unanswerable_rate"] <= 1.0
    assert report.meta["scorer"] == "dense|dense" and report.meta["calibrated"] is False
    only = await evaluate(c, retrieval_only=True)
    assert only.answers == [] and "answer.answer_rate" not in only.summary


async def test_calibrate_stores_thresholds_for_the_pipeline():
    c = await build(retrieval={"strategy": "bm25"})
    result, calibration = await calibrate(c)
    index = await c.retriever.index()
    assert await c.store.get_calibration(index, "bm25|bm25") is None  # dry run stores nothing
    result, calibration = await calibrate(c, apply=True)
    stored = await c.store.get_calibration(index, "bm25|bm25")
    assert stored.min_score == calibration.min_score and stored.min_hits >= 1
    assert stored.dataset.startswith("golden.jsonl@")
    assert result.best.answer_recall >= 0.9
    report = await evaluate(c, retrieval_only=True)
    assert report.meta["calibrated"] is True
