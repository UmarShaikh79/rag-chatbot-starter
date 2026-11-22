"""Command line: ``chatbot <command>``. Run ``chatbot --help`` for the list."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from typing import Any

from chatbot.core.errors import ChatbotError
from chatbot.core.settings import ConfigError, get_settings, load_settings


async def _components():
    from chatbot.core import container
    from chatbot.db import make_pool

    settings = get_settings()
    pool = make_pool(settings)
    await pool.open(wait=True, timeout=30)
    return container.build(settings, pool)


def cmd_migrate(_: argparse.Namespace) -> None:
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    cfg = Config(str(settings.root_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(settings.root_dir / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url().replace("%", "%%"))
    command.upgrade(cfg, "head")
    print("database is up to date")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run(
        "chatbot.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_config=None,
        access_log=False,  # TraceMiddleware logs each request with its trace ID
    )


async def _worker() -> None:
    from chatbot.ingestion.jobs.handlers import make_handlers
    from chatbot.ingestion.jobs.worker import Worker
    from chatbot.observability.logging import configure_logging

    c = await _components()
    obs = c.settings.observability
    configure_logging(obs.log_level, obs.log_format)
    wcfg = c.settings.ingestion.worker
    worker = Worker(c.queue, make_handlers(c), wcfg.concurrency, wcfg.poll_interval_s)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await worker.run(stop)
    finally:
        await c.close()


def cmd_worker(_: argparse.Namespace) -> None:
    asyncio.run(_worker())


async def _ingest(args: argparse.Namespace) -> dict[str, Any]:
    from chatbot.core.container import make_source
    from chatbot.ingestion.jobs.handlers import SYNC_SOURCE, make_handlers
    from chatbot.ingestion.jobs.worker import Worker

    c = await _components()
    try:
        names = [args.source] if args.source else [s.name for s in c.settings.ingestion.sources]
        if args.inline:
            reports = {}
            for name in names:
                report = await c.pipeline.sync_source(
                    make_source(c.settings, name), force=args.force
                )
                reports[name] = report.model_dump(mode="json")
            return reports
        jobs = {
            name: await c.queue.enqueue(
                SYNC_SOURCE,
                {"source": name, "force": args.force},
                dedup_key=f"{SYNC_SOURCE}:{name}",
            )
            for name in names
        }
        if not args.wait:
            return {"queued": jobs}
        await Worker(c.queue, make_handlers(c)).drain()
        out = {}
        for name, job_id in jobs.items():
            job = await c.queue.get(job_id)
            out[name] = job.model_dump(mode="json") if job else None
        return out
    finally:
        await c.close()


def cmd_ingest(args: argparse.Namespace) -> None:
    print(json.dumps(asyncio.run(_ingest(args)), indent=2, default=str))


async def _ask(args: argparse.Namespace) -> None:
    from chatbot.core.types import ChatRequest

    c = await _components()
    try:
        req = ChatRequest(
            message=args.question, user_id=args.user, conversation_id=args.conversation
        )
        if args.stream:
            async for event in c.orchestrator.stream(req):
                if event.event == "delta":
                    print(event.data["text"], end="", flush=True)
                else:
                    print(f"\n\n[{event.event}]")
                    print(json.dumps(event.data, indent=2))
        else:
            resp = await c.orchestrator.run(req)
            print(json.dumps(resp.model_dump(mode="json"), indent=2))
    finally:
        await c.close()


def cmd_ask(args: argparse.Namespace) -> None:
    asyncio.run(_ask(args))


async def _eval_components():
    """Like _components, but conversations and memory jobs stay in memory: an eval run
    leaves nothing behind in the database except what you ask it to store."""
    from chatbot.core import container
    from chatbot.db import make_pool
    from chatbot.testing.fakes import InMemoryJobQueue, InMemoryMemoryStore

    settings = get_settings()
    pool = make_pool(settings)
    await pool.open(wait=True, timeout=30)
    return container.build(settings, pool, memory=InMemoryMemoryStore(), queue=InMemoryJobQueue())


def _profile() -> str:
    import os

    return os.environ.get("CHATBOT_PROFILE") or "dev"


async def _eval(args: argparse.Namespace):
    from chatbot.evals.commands import evaluate

    c = await _eval_components()
    try:
        return await evaluate(c, args.dataset, args.retrieval_only), c.settings
    finally:
        await c.close()


def cmd_eval(args: argparse.Namespace) -> None:
    import os
    from pathlib import Path

    from chatbot.evals import gate as gates

    report, settings = asyncio.run(_eval(args))
    root, evals = settings.root_dir, settings.evals
    profile = _profile()
    baseline_path = root / evals.baselines_dir / f"{profile}.json"
    result = None
    if args.gate:
        rules = gates.load_rules(root / evals.gate, profile)
        result = gates.check(report.summary, rules, gates.load_baseline(baseline_path))
    print(json.dumps({"summary": report.summary, "meta": report.meta}, indent=2))
    if args.out:
        Path(args.out).write_text(report.model_dump_json(indent=2))
        print(f"full report: {args.out}", file=sys.stderr)
    if args.update_baseline:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps({"summary": report.summary, "meta": report.meta}, indent=2) + "\n"
        )
        print(f"baseline written: {baseline_path}", file=sys.stderr)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as fh:
            fh.write(gates.markdown(report.summary, result, f"Evals ({profile} profile)"))
    if result is not None:
        if not result.passed:
            print("eval gate FAILED:\n  " + "\n  ".join(result.failures), file=sys.stderr)
            sys.exit(1)
        print("eval gate passed", file=sys.stderr)


async def _calibrate(args: argparse.Namespace) -> int:
    from chatbot.evals.commands import calibrate

    c = await _eval_components()
    try:
        result, calibration = await calibrate(c, args.dataset, apply=args.apply)
    finally:
        await c.close()
    print(
        json.dumps(
            {
                "scorer": calibration.scorer,
                "min_score": calibration.min_score,
                "min_hits": calibration.min_hits,
                "answer_recall": result.best.answer_recall,
                "abstention": result.best.abstention,
                "met_target": result.met_target,
                "dataset": calibration.dataset,
                "frontier": [p.model_dump() for p in result.frontier],
                "stored": args.apply,
            },
            indent=2,
        )
    )
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if not args.apply:
        print("dry run: add --apply to store these thresholds with the index", file=sys.stderr)
    return 1 if args.check and not result.met_target else 0


def cmd_calibrate(args: argparse.Namespace) -> None:
    sys.exit(asyncio.run(_calibrate(args)))


def cmd_hash_key(args: argparse.Namespace) -> None:
    from chatbot.auth.api_key import hash_key, new_key

    if args.key:
        print(hash_key(args.key))
        return
    key, digest = new_key()
    print(f"API key (give to the client):   {key}")
    print(f"Hash (put in CHATBOT_API_KEYS): {digest}")


def cmd_dev_token(args: argparse.Namespace) -> None:
    """Sign an HS256 token for trying auth.mode: jwt locally, without an identity provider."""
    import os

    from chatbot.auth.jwt import sign_dev_token

    jwt_cfg = load_settings().auth.jwt
    env = jwt_cfg.hs256_secret_env
    secret = os.environ.get(env or "")
    if not env or not secret:
        print(
            "Set auth.jwt.hs256_secret_env (and add HS256 to auth.jwt.algorithms), then put a "
            "secret of 32+ characters in that env var.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not jwt_cfg.issuer or not jwt_cfg.audience:
        print("Set auth.jwt.issuer and auth.jwt.audience first.", file=sys.stderr)
        sys.exit(2)
    scopes = [jwt_cfg.admin_scope] if args.admin else []
    print(sign_dev_token(secret, args.sub, jwt_cfg.issuer, jwt_cfg.audience, args.ttl, scopes))


def cmd_check_config(_: argparse.Namespace) -> None:
    settings = load_settings()
    print(json.dumps(settings.model_dump(mode="json", exclude={"catalog"}), indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="chatbot", description="RAG chatbot starter")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)
    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)
    sub.add_parser("worker", help="run the background job worker").set_defaults(func=cmd_worker)

    ingest = sub.add_parser("ingest", help="sync configured sources into the index")
    ingest.add_argument("--source", help="source name (default: all configured sources)")
    ingest.add_argument("--force", action="store_true", help="re-embed unchanged documents")
    mode = ingest.add_mutually_exclusive_group()
    mode.add_argument("--wait", action="store_true", help="enqueue, then process jobs here")
    mode.add_argument("--inline", action="store_true", help="skip the queue, ingest directly")
    ingest.set_defaults(func=cmd_ingest)

    ask = sub.add_parser("ask", help="ask a question from the terminal")
    ask.add_argument("question")
    ask.add_argument("--user")
    ask.add_argument("--conversation")
    ask.add_argument("--stream", action="store_true")
    ask.set_defaults(func=cmd_ask)

    ev = sub.add_parser("eval", help="run the golden dataset and score retrieval and answers")
    ev.add_argument("--dataset", help="JSONL file (default: evals.dataset)")
    ev.add_argument("--retrieval-only", action="store_true", help="skip answer generation")
    ev.add_argument("--gate", action="store_true", help="exit 1 below evals/gate.yaml floors")
    ev.add_argument("--out", help="write the full per-question report (JSON) here")
    ev.add_argument(
        "--update-baseline", action="store_true", help="save this run as the profile's baseline"
    )
    ev.set_defaults(func=cmd_eval)

    cal = sub.add_parser(
        "calibrate", help="sweep min_score and min_hits on the golden set (ADR 011)"
    )
    cal.add_argument("--dataset", help="JSONL file (default: evals.dataset)")
    cal.add_argument("--apply", action="store_true", help="store the result with the index")
    cal.add_argument(
        "--check", action="store_true", help="exit 1 when the answer-recall target is missed"
    )
    cal.set_defaults(func=cmd_calibrate)

    hk = sub.add_parser("hash-key", help="create an API key, or hash an existing one")
    hk.add_argument("key", nargs="?")
    hk.set_defaults(func=cmd_hash_key)

    dt = sub.add_parser("dev-token", help="sign a local HS256 JWT for trying auth.mode: jwt")
    dt.add_argument("--sub", required=True, help="the user ID the token stands for")
    dt.add_argument("--ttl", type=int, default=3600, help="lifetime in seconds")
    dt.add_argument("--admin", action="store_true", help="add the admin scope")
    dt.set_defaults(func=cmd_dev_token)

    sub.add_parser("check-config", help="print the merged configuration").set_defaults(
        func=cmd_check_config
    )

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)
    except ChatbotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
