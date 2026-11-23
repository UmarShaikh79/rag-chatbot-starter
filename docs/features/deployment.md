# Deployment

How to run the template beyond your laptop: what processes it needs, the Docker image, Docker
Compose for one machine, the Kubernetes example for a cluster, and a checklist for going to
production. The Kubernetes details are in [deploy/k8s/README.md](../../deploy/k8s/README.md).

Back to the [docs home](../README.md) · See also: [Kubernetes example](../../deploy/k8s/README.md)

## At a glance

| | |
| --- | --- |
| **What it does** | Runs Postgres (with pgvector), a one-off migration, the API (`chatbot serve`) and the worker (`chatbot worker`), plus optional Ollama, Qdrant and an OpenTelemetry Collector. |
| **Run with** | `make up` (Compose); `kubectl apply -k deploy/k8s/overlays/dev` or `…/prod` (Kubernetes) |
| **Main files** | [`docker/Dockerfile`](../../docker/Dockerfile), [`docker/compose.yaml`](../../docker/compose.yaml), [`Makefile`](../../Makefile), [`deploy/k8s/`](../../deploy/k8s) (`base/`, `overlays/dev`, `overlays/prod`, `components/otel-collector`), [`config/profiles/prod.yaml`](../../config/profiles/prod.yaml), [`.env.example`](../../.env.example) |
| **Depends on** | Docker; for Kubernetes, `kubectl` (Kustomize is built in) and a cluster; a Postgres with pgvector ≥ 0.8 |
| **Decisions** | ADR [004](../adr/004-postgres-for-everything-first.md) (Postgres for everything first), ADR [030](../adr/030-kubernetes-example-with-kustomize.md) (the Kubernetes example), ADR [009](../adr/009-hosted-and-local-defaults.md) (hosted and local defaults) |

## What it does

The bot is two processes over one database:

| Process | Command | Does | Scale by |
| --- | --- | --- | --- |
| API | `chatbot serve` | HTTP and SSE; answers chat; enqueues ingestion and memory jobs | More replicas behind a load balancer (stateless) |
| Worker | `chatbot worker` | Runs [background jobs](background-jobs.md): source syncs, pushed documents, memory refresh | More replicas (jobs are claimed with `SKIP LOCKED`), or `ingestion.worker.concurrency` |
| Migrate | `chatbot migrate` | Applies database migrations; holds an advisory lock, so concurrent runs migrate once | Runs before the API starts |
| Postgres | `pgvector/pgvector:0.8.1-pg17` | Vectors, BM25, the index catalog, jobs, memory, usage | A managed Postgres with pgvector in production |

Optional: Ollama (local models), Qdrant (another vector store), an OpenTelemetry Collector and
Jaeger. The image is the same for every process; only the command differs.

The Compose stack running on a development machine (real `docker ps`, columns trimmed):

```text
docker-api-1              Up 7 hours (healthy)   0.0.0.0:8000->8000/tcp
docker-worker-1           Up 6 hours             8000/tcp
docker-postgres-1         Up 10 hours (healthy)  0.0.0.0:5432->5432/tcp
docker-qdrant-1           Up 10 hours            0.0.0.0:6333-6334->6333-6334/tcp
docker-otel-collector-1   Up 10 hours            0.0.0.0:4318->4318/tcp, 0.0.0.0:8889->8889/tcp
docker-jaeger-1           Up 10 hours            0.0.0.0:16686->16686/tcp
```

## How it works

**The image** ([`docker/Dockerfile`](../../docker/Dockerfile)): `python:3.12-slim` with `uv`;
dependencies are installed first (cached layer) with optional extras from the `UV_EXTRAS` build
argument; then the code, `config/`, `prompts/`, `migrations/`, `samples/` and `evals/` are
copied; it runs as user `10001`; the default command is `chatbot serve` on port 8000. About
0.6 GB, 1.6 GB with Presidio.

**Configuration at runtime:** `CHATBOT_PROFILE` picks `config/profiles/<name>.yaml`;
`CHATBOT__SECTION__KEY` variables override single settings; secrets (`OPENAI_API_KEY`,
`DATABASE_URL`, `CHATBOT_API_KEYS`, …) come from the environment, never from YAML
([configuration](../configuration.md)). `chatbot serve` checks keys, `DATABASE_URL` and API-key
hashes before starting and stops with one message if one is missing.

**Compose** ([`docker/compose.yaml`](../../docker/compose.yaml)): `postgres` (with a health check
and a volume), `migrate` (runs once), `api` (port 8000, starts after `migrate` succeeds) and
`worker`, all from one build that reads `../.env`. Optional services sit behind profiles
(`ollama`, `qdrant`, `otel`), which the `make up-*` targets turn on together with the matching
extra. `samples/` is mounted read-only, so you can edit sample documents without rebuilding.

**Kubernetes** ([`deploy/k8s/`](../../deploy/k8s/README.md)), plain YAML with Kustomize:

- `base/`: the API Deployment (2 replicas, migrations in an init container, `/livez` and
  `/readyz` probes, a pause before shutdown so streams finish) and Service, the worker
  Deployment (finishes its job on SIGTERM), a nightly CronJob that syncs every source, a
  PodDisruptionBudget, a ServiceAccount without a token; non-root, read-only root filesystem.
- `overlays/dev`: Postgres in the cluster, the `offline` profile, `auth.mode: none`, for kind,
  minikube, k3d or Docker Desktop.
- `overlays/prod`: your image and Secret, a HorizontalPodAutoscaler, an ingress with proxy
  buffering off and 300 s reads (for SSE), a network policy, the `restricted` Pod Security level.
- `components/otel-collector`: a Collector in the namespace, tracing turned on.

## Inputs and outputs

**In:** an `.env` (Compose) or a ConfigMap and Secret (Kubernetes) with the profile, the
settings you override and the secrets; your documents (in the image, on a volume, or pushed
through `POST /v1/ingest`).

**Out:** the API on port 8000 (Compose) or behind your ingress; `/healthz`, `/livez`, `/readyz`
for probes; logs on stdout; traces and metrics to your OTLP endpoint if enabled.

## Turn it on, change it, turn it off

**One machine, with Docker:**

```bash
cp .env.example .env                 # fill in OPENAI_API_KEY, or set CHATBOT_PROFILE=offline
uv run chatbot hash-key              # put the hash in CHATBOT_API_KEYS; keep the key
make up                              # Postgres, migrate, API, worker
make ingest                          # sync the configured sources
make calibrate-apply                 # thresholds for this index
```

Add-ons: `make up-ollama` (local models), `make up-qdrant` (Qdrant), `make up-otel` (Collector
and Jaeger). Other extras in the image: `UV_EXTRAS="--extra langgraph --extra rerank" make up`.
`make down` stops everything; the database volume stays until `docker volume rm`.

**A cluster:** try `overlays/dev` on a local cluster first, then follow the production steps in
[deploy/k8s/README.md](../../deploy/k8s/README.md#production): build and push the image with your
extras, set the image and ingress host in `overlays/prod`, create the database and the Secret,
`kubectl apply -k deploy/k8s/overlays/prod`.

**Production checklist:**

| Item | Why | How |
| --- | --- | --- |
| `CHATBOT_PROFILE=prod` (or your own profile) | JSON logs, no message text in logs | `config/profiles/prod.yaml` |
| A managed Postgres with pgvector ≥ 0.8, backed up | Holds everything: vectors, memory, jobs, usage | RDS, Cloud SQL, Azure, Neon, Supabase…; `sslmode=require` |
| Secrets from a secret manager | Keys never in images or Git | External Secrets, Sealed Secrets, your platform's store |
| Auth: API keys for backends, JWTs for users | `auth.mode: none` is for local use | [Authentication](authentication.md) |
| At least one worker running | Ingestion jobs and memory refresh | Compose `worker`, the k8s worker Deployment |
| Proxy buffering off for `/v1/chat`, long read timeouts | Streaming | The prod ingress does it; nginx `proxy_buffering off` |
| Calibrate after the first ingest and after model changes | Thresholds belong to the index | `chatbot calibrate --apply` (in a pod: `kubectl exec deploy/chatbot-api -- chatbot calibrate --apply`) |
| Your golden set and gate in CI | Catch regressions before deploying | [Evals](evals.md) |
| Prices for your models | Cost reports | [Usage and cost](usage-and-cost.md) |
| Traces and alerts | See and fix problems | [Observability](observability.md) |
| `api.docs: false` if the API is public; `api.cors` only for your origins | Smaller surface | [HTTP API](http-api.md) |
| `pii.allow` set to your product names | Presidio otherwise removes them from questions | [Guardrails](guardrails.md) |
| A retention policy for turns and usage | Privacy | [Memory](memory.md) |

**Changing models or stores in production** is a re-index beside the live index, then a
switch, as part of the deploy ([re-indexing](reindexing.md)).

**Turning parts off:** don't start the optional profiles; remove the CronJob if you push
documents through the API instead of syncing sources.

## Extend it

- **Another platform** (ECS, Cloud Run, Fly.io, a VM with systemd): run the same image three
  ways: `chatbot migrate` once per release, `chatbot serve` behind an HTTPS load balancer with
  `/readyz` as the health check and buffering off, `chatbot worker` as a long-running process.
  Pass configuration as environment variables.
- **Helm:** the manifests are plain YAML; template the image, host and settings if your
  platform requires charts.
- **Your documents in the image:** copy them next to `samples/` and add a `files` source in
  `config/app.yaml`; or mount a volume at the source's `path`; or push them through the API.
- **More extras:** add them to `UV_EXTRAS` at build time (`--extra presidio` also needs
  `SPACY_MODEL_URL`; see [guardrails](guardrails.md#turn-it-on-change-it-turn-it-off)).

## Test it

```bash
make k8s-render                                   # render both overlays (needs kubectl)
uv run pytest tests/unit/test_docker_image.py tests/unit/test_k8s_manifests.py
```

| Test or check | Covers |
| --- | --- |
| [`test_docker_image.py`](../../tests/unit/test_docker_image.py) | the directories needed at runtime are copied into the image, and the eval paths live in them |
| [`test_k8s_manifests.py`](../../tests/unit/test_k8s_manifests.py) | the ingress hosts are valid DNS names |
| CI job `k8s` | renders both overlays and validates them against the Kubernetes 1.33 schemas (kubeconform) |
| By hand on kind (Kubernetes 1.37) | the dev overlay: migrations, probes, a database outage, a rolling restart under load, the CronJob; a server dry run of the prod overlay |

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| `api` exits at once | A preflight check failed (missing key, `DATABASE_URL`, API-key hash) | `docker compose logs api`: the last line says what's missing. |
| `type "vector" does not exist` | Postgres without pgvector | Use the `pgvector/pgvector` image, or a managed Postgres with pgvector ≥ 0.8. |
| Ingestion jobs stay `queued` | No worker running | Start the worker (`docker compose up worker`, the k8s worker Deployment). |
| Streams arrive all at once in production | A proxy buffers them | Turn buffering off for `/v1/chat`; raise read timeouts. |
| Pods restart when the database is down | Liveness probe on `/readyz` | Use `/livez` for liveness, `/readyz` for readiness (the manifests do). |
| An optional backend fails with "not installed" | The image lacks the extra | Rebuild with `UV_EXTRAS`. |
| 409 after deploying new model settings | The index was built with the old embedding model | Re-index and activate as part of the deploy ([re-indexing](reindexing.md)). |
| A prod dry run fails with `namespaces "chatbot" not found` | The namespace must exist first | `kubectl apply -f deploy/k8s/overlays/prod/namespace.yaml`. |

## Related

- Cluster details and production steps: [deploy/k8s/README.md](../../deploy/k8s/README.md).
- Settings and profiles: [configuration](../configuration.md).
- The worker: [background jobs](background-jobs.md).
- Operating Postgres at scale: [operating pgvector](../operations/pgvector.md).
- Changing models safely: [re-indexing](reindexing.md).
