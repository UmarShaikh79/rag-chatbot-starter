from pathlib import Path

import httpx

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


async def test_permanent_errors_are_not_retried():
    """A 403 (key without access to the model) fails the same way every time: one attempt."""
    queue = InMemoryJobQueue()
    calls = []
    request = httpx.Request("POST", "https://api.example.com/v1/embeddings")

    async def forbidden(job):
        calls.append(job.attempts)
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("403 Forbidden", request=request, response=response)

    job_id = await queue.enqueue("embed", {}, max_attempts=3)
    await Worker(queue, {"embed": forbidden}).drain()
    job = await queue.get(job_id)
    assert calls == [1]
    assert job.status == "failed" and "403" in job.last_error


async def test_job_errors_keep_no_key_fragments():
    """last_error is returned by GET /v1/jobs/{id}; OpenAI's 401 repeats part of the key."""
    queue = InMemoryJobQueue()

    async def bad_key(job):
        raise RuntimeError(
            "Error code: 401 - Incorrect API key provided: sk-inval**********************wxyz."
        )

    job_id = await queue.enqueue("embed", {}, max_attempts=1)
    await Worker(queue, {"embed": bad_key}).drain()
    job = await queue.get(job_id)
    assert (
        job.last_error == "RuntimeError: Error code: 401 - Incorrect API key provided: [redacted]."
    )


async def test_rate_limits_are_still_retried():
    queue = InMemoryJobQueue()
    calls = []
    request = httpx.Request("POST", "https://api.example.com/v1/embeddings")

    async def limited(job):
        calls.append(job.attempts)
        response = httpx.Response(429, request=request)
        raise httpx.HTTPStatusError("429 Too Many Requests", request=request, response=response)

    await queue.enqueue("embed", {}, max_attempts=3)
    await Worker(queue, {"embed": limited}).drain()
    assert calls == [1, 2, 3]


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
