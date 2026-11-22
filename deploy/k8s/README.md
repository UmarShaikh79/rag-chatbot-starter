# Kubernetes example

A starting point, not a chart (ADR 030). Docker Compose stays the simplest way to run the
template; use this when you deploy to a cluster. Everything is plain YAML put together with
Kustomize, which ships with `kubectl`.

```
deploy/k8s/
  base/                        API (Deployment + Service), worker, nightly ingest CronJob, PDB, config
  overlays/dev/                local cluster: Postgres + pgvector inside, offline profile, no keys
  overlays/prod/               managed Postgres, your Secret, HPA, ingress for SSE, network policy
  components/otel-collector/   optional: an OpenTelemetry Collector, tracing turned on
```

## Try it on a local cluster (kind, minikube, k3d, Docker Desktop)

```bash
docker build -f docker/Dockerfile -t chatbot:dev .
kind load docker-image chatbot:dev            # minikube: minikube image load chatbot:dev
kubectl apply -k deploy/k8s/overlays/dev
kubectl -n chatbot-dev rollout status deploy/chatbot-api

kubectl -n chatbot-dev exec deploy/chatbot-api -- chatbot ingest --wait
kubectl -n chatbot-dev port-forward svc/chatbot-api 8000:80 &
curl -s localhost:8000/v1/chat -H "Content-Type: application/json" \
  -d '{"message": "How long is the refund window for annual plans?"}'
```

The dev overlay uses the `offline` profile (no API key, quoted answers) and `auth.mode: none`.
To use OpenAI there, set `CHATBOT_PROFILE=dev` in its `configMapGenerator` and
`OPENAI_API_KEY` in its `secretGenerator`. Remove it with
`kubectl delete -k deploy/k8s/overlays/dev` (the Postgres volume goes with the namespace).

## Production

1. Build and push an image with the extras you use:
   `docker build -f docker/Dockerfile --build-arg UV_EXTRAS="--extra otel" -t ghcr.io/you/chatbot:0.5.0 .`
2. Edit `overlays/prod`: the image name and tag, the ingress host (`chatbot.example.com` in
   `ingress.yaml`, twice), and any `CHATBOT__...` settings in the `configMapGenerator`.
3. Create the database (Postgres with pgvector >= 0.8; Cloud SQL, RDS, Azure Database for
   PostgreSQL, Neon, Supabase and others ship it) and the Secret:

   ```bash
   uv run chatbot hash-key          # give the key to your client app, keep the hash for below
   kubectl create namespace chatbot
   kubectl -n chatbot create secret generic chatbot-secrets \
     --from-literal=DATABASE_URL='postgresql://user:pass@host:5432/chatbot?sslmode=require' \
     --from-literal=OPENAI_API_KEY=sk-... \
     --from-literal=CHATBOT_API_KEYS=<the hash>
   ```

   Better: sync it from your secret manager with External Secrets or Sealed Secrets.
4. `kubectl apply -k deploy/k8s/overlays/prod`

What the manifests already do:

- **Migrations** run in an init container before each API pod starts. `chatbot migrate`
  holds a Postgres advisory lock, so pods starting together migrate once.
- **Probes**: `/livez` (process only) for liveness, `/readyz` (database) for readiness, so a
  database outage takes pods out of the load balancer instead of restarting them.
- **Streaming**: the ingress turns proxy buffering off and allows 300 s reads; pods get 60 s
  to finish open streams on shutdown, after a 5 s pause that lets the Service drop them.
- **Worker**: finishes the job it holds on SIGTERM (120 s grace); a job cut off is retried.
  Run more replicas for more throughput: jobs are claimed with `SKIP LOCKED`.
- **Nightly sync**: the `chatbot-ingest` CronJob enqueues a sync of every configured source at
  03:00 UTC; unchanged documents are skipped. Suspended in dev.
- **Security**: non-root user 10001, read-only root filesystem, no capabilities,
  `RuntimeDefault` seccomp, no service account token, and the `restricted` Pod Security level
  on the prod namespace. The network policy lets only `ingress-nginx` reach the API.

Not included, on purpose: Ollama (GPU nodes, model storage) and Qdrant (use Qdrant Cloud or
its Helm chart), a service mesh, and backups. Uploaded files for the `files` source must be
in the image or on a volume you mount at the source's `path`.

## Tracing and metrics

Uncomment `components: [../../components/otel-collector]` in an overlay. It runs a Collector
in the namespace, turns `observability.otel` on and points the app at it. The Collector prints
traces (`debug`) and serves metrics at `:8889/metrics` for Prometheus; add your tracing backend
under `exporters` in `components/otel-collector/collector.yaml`. For Langfuse instead, set
`CHATBOT__OBSERVABILITY__LANGFUSE__ENABLED=true` and the three `LANGFUSE_*` values (keys in
the Secret). See [docs/observability.md](../../docs/observability.md).

## Check the manifests

```bash
kubectl kustomize deploy/k8s/overlays/prod             # render
kubectl apply -f deploy/k8s/overlays/prod/namespace.yaml
kubectl apply -k deploy/k8s/overlays/prod --dry-run=server
```

A server dry run checks everything against your cluster's API server, including the
`restricted` Pod Security level on the namespace. The namespace has to exist first: a dry run
does not create it, so without the second line every namespaced object fails with
`namespaces "chatbot" not found`.

CI renders both overlays and validates them against the Kubernetes 1.33 schemas with
kubeconform, which checks fields but not values such as host names;
`tests/unit/test_k8s_manifests.py` covers the ingress hosts. The dev overlay was run by hand
on kind (Kubernetes 1.37): migrations, probes, a database outage, a rolling restart under
load and the CronJob. The prod overlay passes a server dry run there. `preStop.sleep` needs
Kubernetes 1.30 or later.
