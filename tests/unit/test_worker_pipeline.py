from pathlib import Path

from chatbot.ingestion.jobs.worker import Worker
from chatbot.ingestion.sources.files import FileSource
from chatbot.testing.fakes import InMemoryJobQueue


async def test_worker_retries_then_fails():
    queue = InMemoryJobQueue()
    calls = []

    async def flaky(job):
        calls.append(job.attempts)
        raise RuntimeError("provider down")

    job_id = await queue.enqueue("flaky", {}, max_attempts=3)
    await Worker(queue, {"flaky": flaky}).drain()
    job = await queue.get(job_id)
    assert calls == [1, 2, 3]
    assert job.status == "failed" and "provider down" in job.last_error


async def test_dedup_key_reuses_pending_job():
    queue = InMemoryJobQueue()
    a = await queue.enqueue("sync_source", {"source": "s"}, dedup_key="sync:s")
    b = await queue.enqueue("sync_source", {"source": "s"}, dedup_key="sync:s")
    assert a == b


async def test_pipeline_skips_unchanged_and_removes_deleted(fake_components, tmp_path: Path):
    (tmp_path / "a.md").write_text("# A\nAlpha text about apples.")
    (tmp_path / "b.md").write_text("# B\nBeta text about bananas.")
    source = FileSource("tmp", tmp_path, ["*.md"])
    pipeline = fake_components.pipeline

    first = await pipeline.sync_source(source)
    assert sorted(first.indexed) == ["tmp:a.md", "tmp:b.md"]

    second = await pipeline.sync_source(source)
    assert second.indexed == [] and sorted(second.unchanged) == ["tmp:a.md", "tmp:b.md"]

    (tmp_path / "b.md").unlink()
    (tmp_path / "a.md").write_text("# A\nAlpha text about apricots now.")
    third = await pipeline.sync_source(source)
    assert third.indexed == ["tmp:a.md"] and third.removed == ["tmp:b.md"]


async def test_bad_file_is_reported_not_fatal(fake_components, tmp_path: Path):
    (tmp_path / "ok.md").write_text("# OK\nfine")
    (tmp_path / "broken.pdf").write_bytes(b"not a pdf")
    report = await fake_components.pipeline.sync_source(FileSource("t", tmp_path, ["*"]))
    assert report.indexed == ["t:ok.md"]
    assert "t:broken.pdf" in report.failed
