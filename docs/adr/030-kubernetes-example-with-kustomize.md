# ADR 030: A Kubernetes example with Kustomize

Status: Accepted (milestone 5); Docker Compose stays the standard way to run it

## Decision

`deploy/k8s` is an example, not a chart: a Kustomize base (API Deployment and Service,
worker Deployment, nightly ingest CronJob, PodDisruptionBudget, ConfigMap generated from
literals) and two overlays. `dev` runs Postgres with pgvector in the cluster and the
offline profile, for kind or minikube. `prod` expects a managed Postgres and a Secret you
create, and adds an HPA, an ingress set up for SSE, a network policy and zone spreading. An
optional Kustomize component adds an OpenTelemetry Collector and turns tracing on.

Choices in the manifests:

- Migrations run as an init container of the API. `chatbot migrate` takes a Postgres
  advisory lock, so replicas starting together migrate once.
- Liveness (`/livez`) never touches the database; readiness (`/readyz`) does. A database
  outage takes pods out of rotation instead of restarting them all.
- Pods run as the image's non-root user with a read-only root filesystem, no capabilities and
  the `restricted` Pod Security level; `/tmp` is an emptyDir.
- Grace periods leave time for open SSE streams (API) and the job in hand (worker).

CI renders both overlays and validates them with kubeconform.

## Alternatives considered

- A Helm chart: templating and values files are more to learn and to own for a template
  that will be edited anyway; Kustomize ships with kubectl.
- A migration Job: Jobs are immutable, so re-applying needs a delete or a generated name.
- Running Postgres in the cluster for production: possible with an operator, but a managed
  database is the safer default to show.

## Consequences

- Not tested against a live cluster in CI; the manifests are validated against the
  Kubernetes 1.33 schemas. `preStop.sleep` needs Kubernetes 1.30 or later.
- Images: build with the extras you enable (`--build-arg UV_EXTRAS="--extra otel"`).
