from chatbot.core.settings import RetrievalSection
from chatbot.core.types import Chunk, IndexInfo, ScoredChunk
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


def index(min_score=None, min_hits=None) -> IndexInfo:
    return IndexInfo(
        id=1,
        name="n",
        alias="a",
        embedding_model="m",
        dims=3,
        chunker_version="v",
        min_score=min_score,
        min_hits=min_hits,
    )


def test_min_score_and_min_hits_apply_together():
    cfg = RetrievalSection(min_score=0.5, min_hits=2)
    assert assess([hit(0.9), hit(0.4)], cfg, index()).weak  # only one strong hit
    assert not assess([hit(0.9), hit(0.6)], cfg, index()).weak
    assert assess([], cfg, index()).weak


def test_calibrated_index_thresholds_win():
    cfg = RetrievalSection(min_score=0.5, min_hits=1)
    verdict = assess([hit(0.3)], cfg, index(min_score=0.25, min_hits=1))
    assert verdict.calibrated and not verdict.weak
    forced = assess(
        [hit(0.3)], RetrievalSection(min_score=0.5, thresholds_from="config"), index(0.25, 1)
    )
    assert forced.weak and not forced.calibrated


def test_uncalibrated_index_warns():
    verdict = assess([hit(0.9)], RetrievalSection(), index())
    assert not verdict.calibrated and verdict.warnings


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
