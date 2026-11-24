# argocd-observability: read-only Argo CD SRE observer (CrewAI)

One member of the CrewAI SRE team. Given a scope (one app, several apps, a project or everything) it reads
Argo CD sync/health/operation state, walks the resource tree of problematic apps, and only when needed reads
events, container logs and metrics. It then returns a **structured diagnosis** (`ArgoCDHealthReport`)
that triage, remediation and reporting agents can consume.

It is **strictly read-only, enforced in code** (see [Read-only guarantees](#read-only-guarantees)).

```
src/argocd_observability/
  config/agents.yaml, config/tasks.yaml   role/goal/backstory, task prompts
  agent.py      build_argocd_observability_agent(config) -> crewai.Agent
  tasks.py      health_overview_task(agent, scope), diagnose_app_task(agent, app)
  crew.py       small standalone crew + run_observability()
  cli.py        argocd-observe
  tools/        8 typed CrewAI tools (BaseTool + pydantic args)
  backends/     ArgoCDApiBackend (mode argocd) / KubeBackend (mode kube), same normalized output
  guard.py      allow-lists for Argo CD API, Kubernetes API, Prometheus  <- the only way out
  kube_client.py  ReadOnlyApiClient: guard inside kubernetes.client.ApiClient.request()
  http.py       GET-only HTTP client (Argo CD, Prometheus)
  sanitize.py   redaction + truncation applied to every tool output
  metrics.py    Prometheus -> Argo CD /metrics scrape via API proxy -> metrics-server; text-format parser
  triage.py     deterministic rule-based triage (--no-llm), same tools, same guard
  models.py     ArgoCDHealthReport / AppDiagnosis / Evidence
deploy/
  argocd-readonly-account/   Helm values + argocd-cm / argocd-rbac-cm patches + generate-token.sh
  kube-readonly/             ServiceAccount + ClusterRole (no Secrets) + make-kubeconfig.sh
  cilium/                    optional: allow API server -> Argo CD metrics ports
  demo/broken-apps.yaml      two deliberately broken apps (ImagePullBackOff, CrashLoopBackOff)
```

## Environment this was built against (kind-dev01)

Inspected read-only before writing the code:

| | found | consequence |
|---|---|---|
| CNI | **Cilium** (not Calico), CCNP `baseline-default-deny` (ingress only from `host`) | API server proxy to Argo CD `/metrics` times out, so the agent reports the metrics scrape as unavailable. `deploy/cilium/` has the optional fix |
| Argo CD | v3.6.2, Helm chart `argo-cd` 10.18.2, `server.insecure=true`, ClusterIP only | mode argocd via `kubectl port-forward svc/argocd-server 18081:80` + `ARGOCD_PLAINTEXT=true` (+ `--skip-test-tls` for `argocd login`). Use the Helm values for the account, because manual ConfigMap patches are reverted by `helm upgrade` |
| metrics services | `argocd-metrics` / `argocd-server-metrics` services absent | the agent falls back to pod proxy `pod:8082/8083/8084` |
| Prometheus / metrics-server | none | reported as limitations |
| your kube context | `cluster-admin` | `--check` warns; the agent stays read-only |
| apps | `hello-test` OutOfSync/Missing (never synced, no auto-sync) | good live target |

## Setup

```bash
uv sync --extra dev        # or: python -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env       # set MODEL (or CREWAI_LLM) and the provider key, e.g. OPENAI_API_KEY / ANTHROPIC_API_KEY
```

The LLM is never hard-coded. `MODEL` (falling back to `CREWAI_LLM`) is a LiteLLM model string such as
`anthropic/claude-sonnet-5-5` or `openai/gpt-4o-mini`. Credentials stay in the provider's usual env vars, and
`OPENAI_API_BASE` works for a LiteLLM proxy. If neither is set, CrewAI's default model is used.

### Mode 1: kube current-context (`ARGOCD_OBS_AUTH_MODE=kube`)

Uses the **current** context of `$KUBECONFIG` / `~/.kube/config`, and never switches contexts on its own. It reads `Application`
CRs, workloads, events and logs through the K8s API. On start (`--check`, and the agent's first tool call) it runs
`SelfSubjectAccessReview`s and reports which reads work and which write/sensitive permissions the context has.

To run with least privilege (recommended, and the right setup for in-cluster runs):

```bash
kubectl apply -f deploy/kube-readonly/rbac.yaml          # you run this: SA + ClusterRole (no secrets/exec)
deploy/kube-readonly/make-kubeconfig.sh                  # writes .kube/argocd-observer.kubeconfig (git-ignored)
echo 'ARGOCD_OBS_KUBECONFIG=.kube/argocd-observer.kubeconfig' >> .env
```

For in-cluster runs, use `serviceAccountName: argocd-observer`. The client detects the in-cluster config.

### Mode 2: Argo CD read-only account (`ARGOCD_OBS_AUTH_MODE=argocd`)

Talks only to the Argo CD API server. It uses applications, resource tree, events, live resource status and
`/api/v1/applications/{name}/logs`.

0. **GitOps (preferred for kind-dev01):** the account and RBAC live in `k8s-lab/kind/argocd/values.yaml`. Apply
   with `make argocd-diff && make argocd-upgrade`, then get a token with `make argocd-observer-token`, all from
   `k8s-lab/kind`. See its README section "Read-only аккаунт Argo CD и Prometheus". Steps 1-2 below are the
   standalone alternative for other clusters.
1. Create the account `sre-observer` (apiKey only, no UI login) bound to `role:readonly`:
   ```bash
   # Helm-managed Argo CD:
   helm upgrade argocd argo/argo-cd -n argocd --reuse-values -f deploy/argocd-readonly-account/values-readonly-account.yaml
   # or patch directly (reverted by the next helm upgrade; the rbac patch REPLACES policy.csv):
   kubectl -n argocd patch cm argocd-cm      --type merge --patch-file deploy/argocd-readonly-account/argocd-cm-patch.yaml
   kubectl -n argocd patch cm argocd-rbac-cm --type merge --patch-file deploy/argocd-readonly-account/argocd-rbac-cm-patch.yaml
   ```
2. Generate a token:
   ```bash
   kubectl -n argocd port-forward svc/argocd-server 18081:80     # terminal 1 (or: make argocd-ui ARGOCD_UI_PORT=18081 in k8s-lab/kind)
   deploy/argocd-readonly-account/generate-token.sh             # terminal 2 (admin login with password prompt, then generate-token)
   ```
   Or with the CLI directly:
   ```bash
   argocd login localhost:18081 --username admin --plaintext --skip-test-tls
   argocd account generate-token --account sre-observer --id argocd-observability --expires-in 720h --server localhost:18081 --plaintext
   ```
   **Always pass `--skip-test-tls` to `argocd login` behind a port-forward to an insecure server.** Otherwise
   the CLI first sends a TLS probe, the plain-HTTP server resets it, and `kubectl port-forward` drops the whole
   tunnel ("lost connection to pod"). The next attempt then gets "connection refused". A browser opening
   `https://localhost:<port>` has the same effect, so use `http://`.
3. `.env`:
   ```
   ARGOCD_OBS_AUTH_MODE=argocd
   ARGOCD_SERVER=localhost:18081
   ARGOCD_PLAINTEXT=true        # server.insecure=true behind the port-forward; use ARGOCD_INSECURE=true for self-signed https
   ARGOCD_AUTH_TOKEN=...
   ```
`--check` then lists `can-i` results for app-scoped objects (`*/*`). It warns if the account can sync, update,
delete, run actions or exec. Expected for `sre-observer`: get applications/logs/projects `true`, everything else `false`.

`auto` (the default) picks mode argocd when `ARGOCD_SERVER` and `ARGOCD_AUTH_TOKEN` are set, and kube otherwise.
In mode argocd the kube context is only used, if available, for the optional metrics scrape and metrics-server
(set `ARGOCD_OBS_SCRAPE_METRICS=false` to avoid any Kubernetes API access).

### Metrics (all optional)

Detected once per run, with short timeouts:
1. `PROMETHEUS_URL`: instant and small range PromQL queries (`/api/v1/query`, `/api/v1/query_range`, GET only).
   On kind-dev01, Prometheus is the GitOps Application `prometheus` from `k8s-lab/kind`. It runs the server only,
   keeps 24h / 1GB, and scrapes Argo CD (8082/8083/8084) and cAdvisor. Start `make prometheus-ui` in `k8s-lab/kind`
   and set `PROMETHEUS_URL=http://localhost:9090`. When Prometheus is up, the slower API-proxy scrape below is skipped.
2. Otherwise Argo CD `/metrics` scraped through the API server proxy (`services/argocd-metrics:8082`, falling back
   to `pods/<controller>:8082`, `<server>:8083` and `<repo-server>:8084`), then parsed from the Prometheus text format.
   On kind-dev01 this needs `kubectl apply -f deploy/cilium/allow-apiserver-metrics.yaml` (your call).
3. metrics-server for pod CPU/memory.

When none is available the report says so under *Limitations*. Nothing fails.

## Running

```bash
uv run argocd-observe --check                        # auth mode, identity, permissions, metrics sources (no LLM)
uv run argocd-observe --app hello-test               # diagnose one app (LLM agent)
uv run argocd-observe --app a --app b                # several apps
uv run argocd-observe --all [--project apps]         # health overview
uv run argocd-observe --all --no-llm                 # deterministic triage, no LLM (CI / cheap first pass)
uv run argocd-observe --all --mode argocd --format json
```

Reports are written to `runs/argocd-<timestamp>/report.{json,md}` (`--out-dir`, `--no-save`). Exit code:
0 = all healthy, 1 = something needs attention, 2 = error.

### Sample output (live run on kind-dev01, `--app hello-test --mode kube`, gpt-4o-mini)

```markdown
# Argo CD health report - app hello-test

_generated 2026-11-24T05:21:34Z - auth kube (kube context 'kind-dev01' (Application CRs in namespace 'argocd')) - read-only_

The Argo CD application 'hello-test' is currently OutOfSync and has a health status of Missing. No sync
operation has ever been recorded, which is likely why its resources are missing. ...

| App | Sync | Health | Verdict | Root cause | Confidence |
|---|---|---|---|---|---|
| hello-test | OutOfSync | Missing | MISSING | The app was most likely never synced, which is why its resources are Missing. | high |

## hello-test - MISSING (never_synced)
**Failing resources:** `Service/hello`, `Deployment/hello`
**Evidence**
- [argocd_app_status] Application hello-test: `sync_status: OutOfSync, health_status: Missing`
- [k8s_event] Application/hello-test @ 2026-11-24T04:38:04Z: `Updated sync status: Unknown -> OutOfSync`
- [k8s_event] Application/hello-test @ 2026-11-24T04:38:04Z: `Updated health status: Healthy -> Missing`
**Suggested next steps** (not executed)
1. Investigate why the application has never been synced. ...

## Data sources
- kube-api (6 calls)

## Limitations
- argocd_metrics_scrape unavailable: failed: application-controller: timed out; ... (API server -> pod metrics
  port timed out: a NetworkPolicy probably blocks it; see deploy/cilium/allow-apiserver-metrics.yaml)
- metrics_server unavailable: metrics-server not installed (HTTP 404 Not Found)
- prometheus unavailable: PROMETHEUS_URL not set
```

`data_sources_used`, `limitations`, the sync/health/verdict fields, `generated_at` and `auth_mode` are filled **by code** from
a ledger the tools write to, not by the LLM. Evidence quotes that cannot be matched verbatim against tool output are
flagged under *Limitations*.

## Demo: two broken apps (you run the setup)

```bash
make demo-setup      # [CHANGES THE CLUSTER] shows the current context, asks 'yes', applies deploy/demo/broken-apps.yaml
make demo-wait       # read-only polling until ImagePullBackOff + CrashLoopBackOff appear
make demo            # LLM agent on both apps + scripts/demo_verify.py (expects image_pull and crash_loop with evidence)
make demo-offline    # same with --no-llm
make demo-teardown   # [CHANGES THE CLUSTER] asks 'yes', deletes the apps and namespaces
```

Both apps come from the public `argoproj/argocd-example-apps` (`kustomize-guestbook`) with kustomize overrides:
- `obs-demo-imagepull`: image `gcr.io/google-samples/gb-frontend:v0-does-not-exist`, giving ErrImagePull / ImagePullBackOff.
  The agent cites the pod's `Failed to pull image ...: not found` event.
- `obs-demo-crashloop`: `busybox:1.36` running `... echo 'FATAL: required environment variable REDIS_URL is not set'; exit 1`,
  giving CrashLoopBackOff. The agent cites `exitCode=1` from the pod status and the FATAL line from `previous=true` logs.

## Plugging into the SRE crew

```python
from crewai import Agent, Crew, Process, Task
from argocd_observability import (ObservabilitySettings, build_argocd_observability_agent,
                                  health_overview_task, diagnose_app_task, ArgoCDHealthReport, Scope)

observer = build_argocd_observability_agent(ObservabilitySettings())       # or (llm=..., max_iter=...)
observe  = health_overview_task(observer, Scope(project="apps"))           # output_pydantic=ArgoCDHealthReport

triage = Agent(role="Incident triage lead", goal="...", backstory="...", allow_delegation=False)
rank   = Task(description="Rank the unhealthy apps from the Argo CD report by user impact.",
              expected_output="...", agent=triage, context=[observe])

crew = Crew(agents=[observer, triage], tasks=[observe, rank], process=Process.sequential)
result = crew.kickoff()
report: ArgoCDHealthReport = observe.output.pydantic      # typed; .apps[i].evidence, .root_cause_category, ...
```

- The tasks render their own prompts, so the crew needs no kickoff `inputs`.
- Each agent carries its own runtime context (backend, metrics, ledger). Pass `context=create_context(settings)` to
  share one between agents. Call `finalize_report(report, agent_context(observer))` to fill the code-owned fields
  when you use the agent inside your own crew (`run_observability()` does it for you).
- `suggested_next_steps` are for a human or a remediation agent. This agent never executes them, and a remediation
  agent should get its *own*, separately permissioned tools.

## Tools

| tool | step | what |
|---|---|---|
| `argocd_access_check` | 0 | auth mode, identity, read permissions, write permissions (warn), metrics sources |
| `argocd_list_applications` | 1 | sync, health, operation, conditions, revision, `problem_flags`, `diagnostic_hints` |
| `argocd_get_application` | 1 | one app in detail: sources, sync policy, history, `resources_not_ok` |
| `argocd_resource_tree` | 2 | failing nodes (Deployment → ReplicaSet → Pod, Jobs, ...) with reasons, restarts, images |
| `k8s_events` | 3 | events of one resource from the tree (or of the Application) |
| `argocd_live_resource_status` | 3 | live `status` of a tree resource (exit codes, OOMKilled, conditions); env values redacted |
| `pod_logs` | 4 | tail (default 200, cap `ARGOCD_OBS_LOG_TAIL_MAX`), `previous=true` for crash loops, redacted |
| `argocd_metrics` | 5 | Argo CD metrics per app/component, pod usage, custom PromQL (Prometheus only) |

All tools take typed pydantic args and bound their output (`ARGOCD_OBS_MAX_TOOL_OUTPUT_CHARS`, list/string
shrinking). They return errors as JSON the agent can cite, and they only accept resources that belong to the
app's resource tree, so the LLM cannot read logs of arbitrary pods.

## Read-only guarantees

Enforced in `guard.py`. All three are allow-lists, so anything new or unknown is refused:

- **Argo CD API**: GET only, on `applications`, `applications/{n}`, `/resource-tree`, `/managed-resources`, `/events`,
  `/logs`, `/resource`, `/revisions/{r}/metadata`, `projects`, `session/userinfo`, `account/can-i`, `version`,
  `settings`. Refused: `refresh=` (normal or hard), `follow=true`, `kind=Secret`, terminal/exec, and every
  POST/PUT/PATCH/DELETE (sync, rollback, terminate-operation, resource actions, delete...). Clusters, repositories
  and certificates are not on the list either, since they carry credentials.
- **Kubernetes API**: the guard sits inside `ReadOnlyApiClient.request()`, the single method every call of the
  official client goes through, so even `CoreV1Api(...).delete_namespaced_pod()` is refused before any network
  I/O. Allowed: GET on read paths (Applications, pods, `pods/log`, events, workloads, ingresses, ConfigMaps,
  metrics.k8s.io, API proxy to `/metrics` only) and the `SelfSubjectAccessReview` POST, which asks a question and
  persists nothing. Refused: Secrets, exec/attach/port-forward, scale, eviction, tokens, other proxy paths, and
  every write verb.
- **Prometheus**: GET on the query endpoints only (no admin/TSDB APIs).
- **Redaction** runs before anything reaches the LLM. It covers values of sensitive keys, sensitive env vars,
  ConfigMap/Secret-style `data`, bearer/basic tokens, JWTs, common API key formats (`sk-`, `ghp_`, AWS, Google,
  Slack, GitLab), PEM private keys and `user:pass@` in URLs. It also drops `managedFields` and
  `last-applied-configuration`. The regexes are bounded (linear time), with a regression test.
- A refusal is returned to the agent as `{"refused": "..."}`, counted in the ledger, and listed in the report.

## Tests

```bash
uv run --extra dev pytest -q tests          # or: make test   (no cluster, no LLM)
```

`tests/argocd_obs/` runs against mocked Argo CD and K8s responses. It covers:
- the guard rejecting about 60 mutating or sensitive calls, including the typed kubernetes client at the transport;
- the HTTP client never sending a request;
- redaction, truncation and tail limits;
- Prometheus text parsing (escapes, NaN/±Inf, timestamps, histograms);
- tree building from ownerReferences;
- the triage finding the image-pull, crash-loop and never-synced root causes;
- the agent and task factories, composition into another `Crew`, the guardrail, and the CLI.
