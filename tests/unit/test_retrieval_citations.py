from chatbot.core.settings import RetrievalSection
from chatbot.core.types import Calibration, Chunk, ScoredChunk
from chatbot.orchestration.citations import align, format_sources
from chatbot.retrieval.weak import assess


def hit(score: float, i: int = 0) -> ScoredChunk:
    chunk = Chunk(
        id=f"c{i}",
        document_id=f"d{i}",
        position=0,
        text=f"text {i}",
        metadata={"title": f"Doc {i}", "uri": f"file://{i}"},
        content_hash="h",
    )
    return ScoredChunk(chunk=chunk, score=score, retriever="dense")


def calib(min_score: float, min_hits: int) -> Calibration:
    return Calibration(scorer="dense|dense", min_score=min_score, min_hits=min_hits)


def test_min_score_and_min_hits_apply_together():
    cfg = RetrievalSection(min_score=0.5, min_hits=2)
    assert assess([hit(0.9), hit(0.4)], cfg, None).weak  # only one strong hit
    assert not assess([hit(0.9), hit(0.6)], cfg, None).weak
    assert assess([], cfg, None).weak


def test_calibrated_thresholds_win():
    cfg = RetrievalSection(min_score=0.5, min_hits=1)
    verdict = assess([hit(0.3)], cfg, calib(0.25, 1))
    assert verdict.calibrated and not verdict.weak
    forced = assess(
        [hit(0.3)], RetrievalSection(min_score=0.5, thresholds_from="config"), calib(0.25, 1)
    )
    assert forced.weak and not forced.calibrated


def test_uncalibrated_index_warns():
    verdict = assess([hit(0.9)], RetrievalSection(), None, warn_key="idx:dense|dense")
    assert not verdict.calibrated and verdict.warnings


def test_gate_uses_the_named_score_not_the_fused_rank():
    fused = hit(0.016).model_copy(
        update={"retriever": "hybrid", "scores": {"rrf": 0.016, "dense": 0.7}}
    )
    bm25_only = hit(0.015, 1).model_copy(
        update={"retriever": "hybrid", "scores": {"rrf": 0.015, "bm25": 9.0}}
    )
    low = hit(0.014, 2).model_copy(
        update={"retriever": "hybrid", "scores": {"rrf": 0.014, "dense": 0.1}}
    )
    cfg = RetrievalSection(strategy="hybrid", min_score=0.5, min_hits=1)
    verdict = assess([fused, bm25_only, low], cfg, None)
    assert verdict.gate == "dense" and not verdict.weak
    # The BM25-only hit has no dense score: usable once retrieval is strong, never "strong".
    assert [h.chunk.id for h in verdict.strong] == ["c0"]
    assert [h.chunk.id for h in verdict.usable] == ["c0", "c1"]
    assert assess([bm25_only], cfg, None).weak


def test_align_keeps_valid_markers_only():
    hits = [hit(0.9, 0), hit(0.8, 1)]
    text, cites = align("Refunds take 5 days [2]. Also [7] this [1][2].", hits)
    assert text == "Refunds take 5 days [2]. Also this [1][2]."
    assert [c.marker for c in cites] == [1, 2]
    assert cites[1].document_id == "d1" and cites[1].uri == "file://1"


def test_format_sources_numbers_from_one():
    out = format_sources([hit(0.9, 0), hit(0.8, 1)])
    assert out.startswith("[1] (Doc 0) text 0")
    assert "\n\n[2] (Doc 1) text 1" in out


def test_filtered_requests_need_fewer_corroborating_hits():
    """Regression: min_hits is calibrated on the whole corpus; a filter to one document's
    chunks rarely has two strong ones, so every filtered question used to clarify."""
    cfg = RetrievalSection(min_score=0.5, min_hits=2)  # min_hits_filtered defaults to 1
    hits = [hit(0.9), hit(0.2, 1), hit(0.1, 2)]
    assert assess(hits, cfg, None).weak  # unfiltered: still needs two strong hits
    verdict = assess(hits, cfg, None, filtered=True)
    assert not verdict.weak and verdict.min_hits == 1
    assert assess([hit(0.4)], cfg, None, filtered=True).weak  # min_score still applies
    calibrated = assess(hits, cfg, calib(0.5, 3), filtered=True)  # calibrated value is capped
    assert not calibrated.weak and calibrated.min_hits == 1 and calibrated.calibrated
    off = RetrievalSection(min_score=0.5, min_hits=2, min_hits_filtered=None)
    assert assess(hits, off, None, filtered=True).weak


def test_min_hits_never_exceeds_what_retrieval_could_return():
    cfg = RetrievalSection(min_score=0.5, min_hits=3, min_hits_filtered=None)
    assert assess([hit(0.9), hit(0.8)], cfg, None).weak  # pool not known to be exhausted
    verdict = assess([hit(0.9), hit(0.8)], cfg, None, available=2)
    assert not verdict.weak and verdict.min_hits == 2
    assert assess([hit(0.9), hit(0.3)], cfg, None, available=2).weak  # both must be strong
    assert assess([], cfg, None, available=0).weak  # nothing found is never strong
