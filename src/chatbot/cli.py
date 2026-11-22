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


async def _components(role: str = "cli"):
    from chatbot.core import container
    from chatbot.db import make_pool
    from chatbot.observability.tracing import configure_telemetry

    settings = get_settings()
    settings.check_credentials()  # one readable error for every missing key, before any client
    configure_telemetry(settings, role)  # no-op unless otel or langfuse is on (ADR 026)
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


def _preflight_serve() -> None:
    """What would otherwise fail inside uvicorn's startup, with a traceback: missing API keys,
    DATABASE_URL, API-key hashes or JWT secret, Langfuse keys. Each raises ConfigError, which
    `main` prints as one message."""
    import os

    from chatbot.auth.api_key import Authenticator
    from chatbot.observability.tracing import langfuse_endpoint

    settings = get_settings()
    settings.check_credentials()
    settings.database_url()
    Authenticator(settings.auth)
    if settings.observability.langfuse.enabled:
        langfuse_endpoint(settings, os.environ)


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    _preflight_serve()
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

    c = await _components("worker")
    obs = c.settings.observability
    configure_logging(obs.log_level, obs.log_format)
    wcfg = c.settings.ingestion.worker
    worker = Worker(
        c.queue, make_handlers(c), wcfg.concurrency, wcfg.poll_interval_s, c.usage_store
    )
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
            from chatbot.observability.instrument import metered_operation

            reports = {}
            for name in names:
                async with metered_operation(f"ingest:{name}", c.usage_store):
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
        await Worker(c.queue, make_handlers(c), usage_store=c.usage_store).drain()
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
    settings.check_credentials()
    pool = make_pool(settings)
    await pool.open(wait=True, timeout=30)
    # Eval runs are not billed to anyone: no usage records (metrics and traces still flow).
    return container.build(
        settings,
        pool,
        memory=InMemoryMemoryStore(),
        queue=InMemoryJobQueue(),
        usage_store=None,
    )


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

    settings = get_settings()
    root, evals = settings.root_dir, settings.evals
    profile = _profile()
    baseline_path = root / evals.baselines_dir / f"{profile}.json"
    rules = None
    if args.gate:
        # Before the run, not after: a profile with no gate must not cost a full eval.
        try:
            rules = gates.load_rules(root / evals.gate, profile)
        except ValueError as exc:
            sys.exit(f"{exc}. Add the profile to {evals.gate}, or run without --gate.")
    report, _ = asyncio.run(_eval(args))
    # The report is written before the gate is checked, so a failing run is still on disk.
    print(json.dumps({"summary": report.summary, "meta": report.meta}, indent=2))
    if args.out:
        Path(args.out).write_text(report.model_dump_json(indent=2))
        print(f"full report: {args.out}", file=sys.stderr)
    result = None
    if rules is not None:
        result = gates.check(report.summary, rules, gates.load_baseline(baseline_path))
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


async def _reindex(args: argparse.Namespace) -> dict[str, Any]:
    from chatbot.core.container import make_source
    from chatbot.ingestion.reindex import reindex

    c = await _components()
    try:
        settings = c.settings
        sources = [make_source(settings, s.name) for s in settings.ingestion.sources]
        report = await reindex(
            c.store,
            c.pipeline,
            sources,
            settings.vector_store.index,
            backend=settings.vector_store.backend,
            restart=args.restart,
            allow_missing=args.allow_missing,
            activate=args.activate,
            catch_up=not args.no_catch_up,
        )
    finally:
        await c.close()
    out = report.model_dump(mode="json", exclude={"passes"}) | {"summary": report.summary()}
    if report.missing and not report.ready:
        print(
            f"{len(report.missing)} documents in the live index have no stored text and come "
            "from no configured source, so they could not be rebuilt (listed under "
            "'missing'). Push them again through POST /v1/ingest with "
            f"CHATBOT__VECTOR_STORE__INDEX={report.staging}, then rerun; or rerun with "
            "--allow-missing to leave them out.",
            file=sys.stderr,
        )
    elif not report.activated:
        print(
            f"Built {report.new_index} behind '{report.staging}'. Next:\n"
            f"  CHATBOT__VECTOR_STORE__INDEX={report.staging} chatbot calibrate --apply\n"
            f"  CHATBOT__VECTOR_STORE__INDEX={report.staging} chatbot eval --gate\n"
            "  chatbot index activate        # then restart the API and workers",
            file=sys.stderr,
        )
    else:
        print(
            f"'{report.alias}' now points at {report.new_index}. Calibrate it "
            "(`chatbot calibrate --apply`) and restart the API and workers with this config.",
            file=sys.stderr,
        )
    return out


def cmd_reindex(args: argparse.Namespace) -> None:
    out = asyncio.run(_reindex(args))
    print(json.dumps(out, indent=2, default=str))
    if out["missing"] and not out["ready"]:
        sys.exit(1)


async def _index_admin(args: argparse.Namespace) -> Any:
    from chatbot.core.interfaces import IndexAdmin
    from chatbot.stores.catalog import staging_alias

    c = await _components()
    try:
        store = c.store
        if not isinstance(store, IndexAdmin):
            raise ChatbotError(f"{type(store).__name__} does not support index administration")
        alias = c.settings.vector_store.index
        if args.action == "list":
            return [s.model_dump(mode="json") for s in await store.list_indexes()]
        if args.action == "activate":
            info = await store.activate(alias, staging_alias(alias), force=args.force)
            print(
                f"'{alias}' now points at {info.name}. Restart the API and workers with the "
                "configuration it was built with. Undo with `chatbot index rollback`.",
                file=sys.stderr,
            )
            return info.model_dump(mode="json")
        if args.action == "rollback":
            info = await store.rollback(alias)
            print(f"'{alias}' points at {info.name} again.", file=sys.stderr)
            return info.model_dump(mode="json")
        if args.action == "unalias":
            if args.name == alias and not args.force:
                raise ChatbotError(
                    f"'{alias}' is the alias this configuration serves (vector_store.index); "
                    "removing it takes chat and ingestion offline. Pass --force if you mean it."
                )
            names = await store.remove_alias(args.name)
            if names:
                print(
                    f"Removed '{args.name}'. Its indexes are kept: `chatbot index drop "
                    f"{names[0]}` deletes one that no other alias uses.",
                    file=sys.stderr,
                )
            return {"removed": args.name, "indexes": names}
        if args.action == "drop":
            await store.drop_index(args.name)
            return {"dropped": args.name}
        raise ChatbotError(f"unknown action {args.action}")
    finally:
        await c.close()


def cmd_index(args: argparse.Namespace) -> None:
    print(json.dumps(asyncio.run(_index_admin(args)), indent=2, default=str))


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


async def _usage(args: argparse.Namespace) -> Any:
    from datetime import UTC, datetime, timedelta

    from chatbot.db import make_pool
    from chatbot.observability.accounting import PostgresUsageStore

    settings = get_settings()
    pool = make_pool(settings)
    await pool.open(wait=True, timeout=30)
    try:
        since = datetime.now(UTC) - timedelta(days=args.days) if args.days else None
        return await PostgresUsageStore(pool).report(args.by, since=since, user_id=args.user)
    finally:
        await pool.close()


def cmd_usage(args: argparse.Namespace) -> None:
    report = asyncio.run(_usage(args))
    if args.json:
        print(report.model_dump_json(indent=2))
        return
    cols = ("ops", "prompt", "completion", "embedding")
    print(f"{args.by:<28} {cols[0]:>6} " + " ".join(f"{c:>10}" for c in cols[1:]) + " cost_usd")
    for row in [*report.rows, report.total.model_copy(update={"key": "TOTAL"})]:
        cost = "n/a" if row.cost_usd is None else f"{row.cost_usd:.4f}"
        if row.unpriced_calls:
            cost += "*"
        print(
            f"{row.key or '(none)':<28} {row.operations:>6} {row.prompt_tokens:>10} "
            f"{row.completion_tokens:>10} {row.embedding_tokens:>10} {cost:>12}"
        )
    if report.total.unpriced_calls:
        print(
            f"* {report.total.unpriced_calls} calls had no price: set price_per_mtok in "
            "config/models.yaml (ADR 028)",
            file=sys.stderr,
        )


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

    ri = sub.add_parser(
        "reindex",
        help="build a new index for the current config beside the live one (ADR 023)",
    )
    ri.add_argument(
        "--activate", action="store_true", help="switch the alias over when the build is ready"
    )
    ri.add_argument(
        "--restart", action="store_true", help="start a fresh build instead of resuming one"
    )
    ri.add_argument(
        "--allow-missing",
        action="store_true",
        help="mark ready even if some documents could not be rebuilt",
    )
    ri.add_argument(
        "--no-catch-up", action="store_true", help="skip the second pass (nothing writes now)"
    )
    ri.set_defaults(func=cmd_reindex)

    ix = sub.add_parser("index", help="list, activate, roll back, unalias or drop physical indexes")
    ix_sub = ix.add_subparsers(dest="action", required=True)
    ix_sub.add_parser("list", help="every index with its aliases and status")
    act = ix_sub.add_parser("activate", help="point the alias at the index behind <alias>_next")
    act.add_argument("--force", action="store_true", help="activate an index not marked ready")
    ix_sub.add_parser("rollback", help="point the alias back at the previous index")
    unalias = ix_sub.add_parser(
        "unalias", help="remove an alias (a test index, a retired corpus); its indexes stay"
    )
    unalias.add_argument("name")
    unalias.add_argument("--force", action="store_true", help="even the configured alias")
    drop = ix_sub.add_parser("drop", help="delete an index no alias points at")
    drop.add_argument("name")
    ix.set_defaults(func=cmd_index)

    hk = sub.add_parser("hash-key", help="create an API key, or hash an existing one")
    hk.add_argument("key", nargs="?")
    hk.set_defaults(func=cmd_hash_key)

    dt = sub.add_parser("dev-token", help="sign a local HS256 JWT for trying auth.mode: jwt")
    dt.add_argument("--sub", required=True, help="the user ID the token stands for")
    dt.add_argument("--ttl", type=int, default=3600, help="lifetime in seconds")
    dt.add_argument("--admin", action="store_true", help="add the admin scope")
    dt.set_defaults(func=cmd_dev_token)

    us = sub.add_parser("usage", help="tokens and cost from usage records (ADR 028)")
    us.add_argument("--by", choices=["day", "user", "model", "operation"], default="day")
    us.add_argument("--days", type=int, default=30, help="look back this many days (0: all)")
    us.add_argument("--user", help="only this user_id")
    us.add_argument("--json", action="store_true")
    us.set_defaults(func=cmd_usage)

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
    finally:
        from chatbot.observability.tracing import shutdown_telemetry

        shutdown_telemetry()  # flush spans and metrics before the process exits


if __name__ == "__main__":
    main()
