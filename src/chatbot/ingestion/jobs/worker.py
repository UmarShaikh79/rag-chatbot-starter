"""Background worker: claims jobs and runs the handler registered for their kind."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from chatbot.core.interfaces import JobQueue
from chatbot.core.types import Job
from chatbot.observability.logging import new_trace_id, reset_trace_id, set_trace_id

log = logging.getLogger(__name__)

Handler = Callable[[Job], Awaitable[dict[str, Any]]]


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        handlers: dict[str, Handler],
        concurrency: int = 2,
        poll_interval_s: float = 1.0,
    ) -> None:
        self.queue = queue
        self.handlers = handlers
        self.concurrency = concurrency
        self.poll_interval_s = poll_interval_s

    async def run_once(self) -> bool:
        """Claim and run one job. Returns False when the queue had nothing to do."""
        job = await self.queue.claim(list(self.handlers))
        if job is None:
            return False
        token = set_trace_id(new_trace_id())
        try:
            log.info(
                "job started", extra={"job_id": job.id, "kind": job.kind, "attempt": job.attempts}
            )
            result = await self.handlers[job.kind](job)
            await self.queue.complete(job.id, result)
            log.info("job succeeded", extra={"job_id": job.id, "kind": job.kind})
        except Exception as exc:
            log.exception("job failed", extra={"job_id": job.id, "kind": job.kind})
            await self.queue.fail(job.id, f"{type(exc).__name__}: {exc}")
        finally:
            reset_trace_id(token)
        return True

    async def drain(self, max_jobs: int = 1000) -> int:
        """Run jobs until the queue is empty (used by tests and `chatbot ingest --wait`)."""
        done = 0
        while done < max_jobs and await self.run_once():
            done += 1
        return done

    async def _loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                busy = await self.run_once()
            except Exception:
                log.exception("worker loop error")
                busy = False
            if not busy:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_interval_s)

    async def run(self, stop: asyncio.Event) -> None:
        log.info(
            "worker started", extra={"kinds": list(self.handlers), "concurrency": self.concurrency}
        )
        await asyncio.gather(*(self._loop(stop) for _ in range(self.concurrency)))
