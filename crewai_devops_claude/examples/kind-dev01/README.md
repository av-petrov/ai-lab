# kind-dev01: пример запуска

Все команды выполняются из корня `crewai_devops_claude`.

## 0. Поднять кластер и окружение

```bash
colima start                                   # docker для kind
kubectl --context kind-dev01 get nodes         # API должен отвечать

/opt/homebrew/bin/python3.13 -m venv .venv     # crewai требует Python < 3.14
.venv/bin/pip install -e '.[dev]'
```

Права доступа: kubeconfig kind даёт cluster-admin, но коллектор только читает (list/get).
Для запуска «как в проде» под отдельным ServiceAccount: `kubectl apply -f k8s/capacity-reader-rbac.yaml`.

## 1. Снимок кластера (read-only)

```bash
.venv/bin/k8s-capacity collect --config examples/kind-dev01/run_audit.yaml --out runs/kind-dev01-snapshot.json
```

В снимок попадают только поля, нужные планировщику: requests/limits, селекторы, affinity,
tolerations, тома, приоритеты. env, секреты и образы не сохраняются.

Что ожидать в `data_gaps` на kind:
- нет metrics-server → нет usage. Установка:
  `kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml`,
  затем добавить в Deployment аргумент `--kubelet-insecure-tls`;
- CNI в kind-dev01 — Cilium с `ipam: kubernetes`: лимит IP берётся из podCIDR узла (/24), режим `podcidr` верен;
- control-plane без pool-метки выделяется в отдельный пул `control-plane` (у него taint NoSchedule);
- PV от `rancher.io/local-path` (StorageClass `standard`) — hostPath с nodeAffinity, поэтому
  считаются local PV: при N−1 такие Pod не переезжают. Это верно для kind.

## 2. Аудит только кодом (без LLM)

```bash
.venv/bin/k8s-capacity audit --config examples/kind-dev01/run_audit.yaml \
  --snapshot runs/kind-dev01-snapshot.json --no-llm
```

Отчёт: `runs/<timestamp>-audit-kind-dev01/report.md` и `report.json`.

## 3. Admission только кодом

```bash
# масштабирование argocd-repo-server 1 -> 3 (Pod без requests -> FITS_WITH_CONDITIONS)
.venv/bin/k8s-capacity admission --config examples/kind-dev01/run_admission.yaml \
  --snapshot runs/kind-dev01-snapshot.json --no-llm

# новое приложение с anti-affinity «один на узел»
.venv/bin/k8s-capacity admission --config examples/kind-dev01/run_admission.yaml \
  --spec examples/kind-dev01/new_app.yaml --snapshot runs/kind-dev01-snapshot.json --no-llm

# готовый манифест
.venv/bin/k8s-capacity admission --config examples/kind-dev01/run_admission.yaml \
  --manifest examples/manifest_cache.yaml --change-type new --snapshot runs/kind-dev01-snapshot.json --no-llm
```

## 4. С агентами через LiteLLM в этом же кластере

LiteLLM сейчас не установлен (namespace `litellm` пуст). Установка — в `../../../lite-llm-kind/litellm-values.yaml`.

```bash
kubectl --context kind-dev01 -n litellm port-forward svc/litellm 4000:4000 &

export OPENAI_API_BASE=http://localhost:4000
export OPENAI_API_KEY=<LiteLLM master key или virtual key>
export CREWAI_LLM=openai/qwen3.8-27b-local

.venv/bin/k8s-capacity audit --config examples/kind-dev01/run_audit.yaml --snapshot runs/kind-dev01-snapshot.json
.venv/bin/k8s-capacity admission --config examples/kind-dev01/run_admission.yaml \
  --spec examples/kind-dev01/new_app.yaml --snapshot runs/kind-dev01-snapshot.json
```

Или через CrewAI CLI:

```bash
CAPACITY_MODE=audit CAPACITY_CONFIG=examples/kind-dev01/run_audit.yaml \
CAPACITY_SNAPSHOT=runs/kind-dev01-snapshot.json crewai run
```

Модели нужен tool calling (OpenAI-совместимые `tools`). Если qwen за `llm.dops.ru` его не
отдаёт, агенты не смогут вызвать инструменты — запускайте с `--no-llm`. Детерминированная
часть отчёта от LLM не зависит.

В LLM уходят имена namespace, Pod и узлов (компактные результаты инструментов), но не сам снимок.
