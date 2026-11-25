> **Argo CD observability agent** (read-only Argo CD SRE observer, `argocd-observe`): see [README.argocd-observability.md](README.argocd-observability.md).

# k8s_capacity: анализ ёмкости Kubernetes на CrewAI (read-only)

Система отвечает не на вопрос «сколько свободно CPU/RAM», а на практические вопросы:

1. Можно ли сейчас развернуть приложение с заданными ресурсами?
2. Гарантирован ли rolling update работающих приложений при выбранной модели обновлений?
3. Сколько резервировать под обновления?
4. Какой ресурс первым ограничит рост: CPU, RAM, число Pod, IP-адреса, CSI-слоты или ёмкость отдельных узлов?
5. Что будет при потере одного worker node?

Два режима:

- **audit** — отчёт с рейтингом рисков;
- **admission** — вердикт `FITS` / `FITS_WITH_CONDITIONS` / `DOES_NOT_FIT` по пяти проверкам. При отказе указывается минимальное изменение, после которого изменение проходит.

## 1. Допущения об окружении

| Параметр | Допущение по умолчанию | Где меняется |
|---|---|---|
| Kubernetes | ≥ 1.29 (native sidecar `restartPolicy: Always`, `matchLabelKeys`, `nodeAffinityPolicy`) | — |
| Managed / on-prem | Любой. Пул узла определяется по `node.kubernetes.io/pool`, `eks.amazonaws.com/nodegroup`, `cloud.google.com/gke-nodepool`, `kubernetes.azure.com/agentpool`, `karpenter.sh/nodepool` | `pool_label` |
| CNI / IPAM | `podcidr`: лимит IP узла = размер podCIDR − 3. Поддержаны также `aws-vpc-cni` (таблица IP на тип инстанса), `cilium-cluster-pool` (CiliumNode), `static`. Если режим неизвестен, проверка IP помечается «не проверено» | `ipam` |
| Autoscaler | Нет, число узлов фиксировано | `autoscaler.node_groups` |
| Источники данных | Kubernetes API (только get/list). Usage берётся из Prometheus (cpu p95 и memory max за 7 дней), иначе из metrics-server (мгновенный снимок) | `prometheus_url` |
| Модель обновлений | `sequential`: приложения обновляются по одному. Требуется N−1. Резерв под обновления других обязателен | `update_concurrency`, `require_*` |
| Scoring планировщика | `LeastAllocated` (дефолт kube-scheduler) | `scoring` |
| N−1 и приоритеты | Pod с priority < 0 могут остаться Pending после отказа без провала проверки | `n_minus_1_min_priority` |
| CrewAI / LLM | CrewAI 1.15.x. LLM задаётся через `CREWAI_LLM` (LiteLLM-строка, можно через LiteLLM proxy) и опционально `CREWAI_LLM_LEAD` / `CREWAI_LLM_ANALYST` | `.env` |

## 2. Архитектура

### Принцип

LLM ничего не считает. Все расчёты выполняет детерминированный пакет `engine/`, который не зависит от CrewAI. Агенты вызывают инструменты, интерпретируют результаты и формулируют выводы. Вердикт и severity рисков определяются в коде. Guardrail не даёт LLM поменять вердикт. Финальные `report.md` и `report.json` собирает код: числа берутся из вычислений, текст — из ответа LLM.

### Агенты (3)

| Агент | Зона ответственности | Инструменты |
|---|---|---|
| `capacity_modeler` | Снимок кластера, модель ёмкости по requests, limiting resource, фрагментация, скрытый спрос, what-if размещения | `collect_cluster_snapshot`, `capacity_model`, `simulate_placement` |
| `resilience_analyst` | Rolling update (surge, квоты, дедлоки), резерв по модели одновременности, N−1, полный admission | `rollout_reserve`, `simulate_node_failure`, `simulate_admission`, `simulate_placement` |
| `capacity_lead` | Связать результаты в решение: реестр рисков, вердикт с обоснованием, поиск минимального исправления | `computed_risk_register`, `search_remediations`, `simulate_placement` |

Почему агентов три, а не четыре:

- Отдельный «сборщик данных» ничего не решает, он только вызывает один инструмент. Поэтому сбор объединён с моделью ёмкости: оценка полноты данных и есть первая часть моделирования.
- Rolling update и N−1 разделяют одну механику (контроллер плюс планировщик на реальной раскладке), поэтому их ведёт один аналитик.
- Lead не пересказывает чужие выводы. У него собственные инструменты (реестр рисков, поиск исправлений) и задача связать причины: например, одно anti-affinity правило объясняет сразу дедлок, провал N−1 и блокировку drain.
- `allow_delegation: false` у всех. Делегирование в sequential-пайплайне порождает именно пересказы.

### Почему `Process.sequential`

Поток данных — фиксированный конвейер. Hierarchical-менеджер добавил бы недетерминированную маршрутизацию и токены, а арбитрировать ему нечего, потому что числа считает код. Зависимости задаются явно через `context`.

### Поток данных

```
                    ┌────────────── run dir (CAPACITY_RUN_DIR) ───────────────┐
 RunConfig ──main──►│ run_config.json   snapshot.json   results/<tool>.json   │
                    └───────▲───────────────▲────────────────────▲────────────┘
 K8s API (get/list) ─┐      │               │ read               │ write (полный JSON)
 Prometheus/metrics ─┴─► collector ─────────┘                    │
                                                                 │ LLM видит компактный JSON
                                                                 │ + result_file + snapshot_digest
 AUDIT:     capacity_modeler ──► resilience_analyst ──► resilience_analyst ──► capacity_lead
            (baseline)           (rollout reserve)      (N−1)                  (AuditNarrative)
 ADMISSION: capacity_modeler ──► resilience_analyst ──► capacity_lead
            (baseline+what-if)   (5 проверок)           (remediation + AdmissionNarrative, guardrail)
                                                                 │
                                    report.py: computed (код) + narrative (LLM) → report.json / report.md
```

Большой JSON никогда не проходит через LLM. Инструменты читают снимок с диска, а агенту возвращают компактную выжимку со ссылкой `result_file`. Так каждое число прослеживается до файла и снимка (`snapshot_digest`).

## 3. Файлы

```
src/k8s_capacity/
  config/agents.yaml      роли, goal/backstory с предметными правилами
  config/tasks.yaml       4 задачи audit + 3 задачи admission, переменные CrewAI
  crew.py                 CapacityCrew(mode=...) с @agent/@task/@crew, sequential
  main.py                 CLI: audit | admission | collect; --no-llm; run() для `crewai run`
  models.py               AdmissionSpec, RunConfig, Computed/Narrative/Report модели
  report.py               сборка отчётов, рендер markdown, guardrails
  runtime.py              run-директория, кеш контекста, сохранение результатов
  tools/collector.py      read-only сбор: Kubernetes API + Prometheus/metrics-server
  tools/capacity_tools.py 8 BaseTool-обёрток над engine
  engine/                 детерминированное ядро (без CrewAI)
    quantity.py           quantity → целые (millicores/bytes)
    selectors.py          labelSelector, nodeSelectorTerms, taints/tolerations
    model.py              эффективные requests (init/sidecar/overhead), PodShape, PVC-топология
    state.py              ClusterState: фильтры планировщика, scoring, preemption, квоты, PDB
    snapshot.py           снимок → ClusterState + индексы workload
    capacity.py           модель ёмкости, limiting resource, фрагментация, скрытые риски
    rollout.py            эмуляция контроллеров Deployment/StatefulSet/DaemonSet, резерв, дедлоки
    failure.py            N−1, preemption, PDB, autoscaler
    admission.py          5 проверок, вердикт, поиск исправлений, LimitRange
    audit.py              сборка аудита + правила рисков (severity)
k8s/capacity-reader-rbac.yaml   ClusterRole только get/list
examples/                        run-конфиги, AdmissionSpec, манифест, output/ — готовые отчёты
tests/                           26 тестов; fixtures/make_demo_cluster.py — выдуманный кластер
```

### Фильтры планировщика в симуляторе

Реализованы:

- NodeUnschedulable, NodeName, TaintToleration (`NoSchedule`/`NoExecute`);
- NodeAffinity (nodeSelector и required terms с In/NotIn/Exists/DoesNotExist/Gt/Lt/matchFields);
- NodePorts (конфликт hostPort);
- NodeResourcesFit по всем ресурсам allocatable: cpu, memory, pods, ephemeral-storage, hugepages, extended. Дополнительно синтетические `pod-ips` и `attachable-volumes/<csi-driver>`;
- VolumeBinding/VolumeZone (nodeAffinity PV, allowedTopologies у WaitForFirstConsumer, свободные local PV);
- VolumeRestrictions (RWO-claim на одном узле);
- PodTopologySpread (DoNotSchedule, minDomains, nodeAffinityPolicy/nodeTaintsPolicy, matchLabelKeys);
- InterPodAffinity (required affinity, anti-affinity, симметричный anti-affinity существующих Pod, namespaceSelector).

Preferred-правила, ScheduleAnyway, PreferNoSchedule и системный spread по умолчанию участвуют в scoring, но не в фильтрации. Preemption эмулирует DefaultPreemption: минимальный набор жертв с reprieve и ранжирование по нарушениям PDB и приоритетам.

Помечается «не проверено»:

- лимит IP при неизвестном IPAM;
- CSI-лимиты без CSINode;
- PVC с Immediate binding (зону выбирает provisioner);
- скоупы квот Terminating/NotTerminating/CrossNamespacePodAffinity;
- object-count квоты кроме `count/pods`.

## 4. Запуск

```bash
uv venv && uv pip install -e '.[dev]'        # Python 3.10–3.13
cp .env.example .env                          # CREWAI_LLM=..., OPENAI_API_KEY / OPENAI_API_BASE

# офлайн на выдуманном кластере, только код (без LLM): удобно для CI
k8s-capacity audit     --config examples/run_audit.yaml --no-llm
k8s-capacity admission --config examples/run_admission.yaml --no-llm
k8s-capacity admission --config examples/run_admission.yaml --spec examples/admission_api_resize.yaml
k8s-capacity admission --config examples/run_admission.yaml --manifest examples/manifest_cache.yaml

# живой кластер: уберите snapshot_file из конфига (или сначала сохраните снимок)
kubectl apply -f k8s/capacity-reader-rbac.yaml
k8s-capacity collect --config examples/run_audit.yaml --out snapshot.json
k8s-capacity audit --config examples/run_audit.yaml --snapshot snapshot.json

# через CLI CrewAI
CAPACITY_MODE=admission CAPACITY_CONFIG=examples/run_admission.yaml crewai run

pytest -q
```

Пример запуска из кода:

```python
from k8s_capacity.main import load_config, execute
cfg = load_config("examples/run_admission.yaml", "admission")
run_dir = execute(cfg)                 # с LLM; execute(cfg, use_llm=False) — только код
print((run_dir / "report.md").read_text())
```

## 4a. Проверка агента с LLM (самостоятельно)

1. **Окружение** (Python 3.10–3.13; crewai не ставится на 3.14+):
   ```bash
   /opt/homebrew/bin/python3.13 -m venv .venv
   .venv/bin/pip install -e '.[dev]'
   cp .env.example .env        # прописать CREWAI_LLM и OPENAI_API_KEY
   ```
   `.env` лежит в корне проекта и загружается автоматически. Модель задаётся строкой LiteLLM, например `CREWAI_LLM=openai/gpt-4o-mini`.

2. **Предполётная проверка** (один короткий вызов LLM, кластер только читается):
   ```bash
   .venv/bin/k8s-capacity doctor --kube-context kind-dev01
   ```
   Все строки должны быть `OK`. Особенно важна строка `tool calling`: без неё агенты не могут вызывать инструменты.

3. **Регрессия без LLM** (должно быть `28 passed`): `.venv/bin/pytest -q`

4. **Полный прогон на демо-кластере.** Он дешёвый, а результат заранее известен:
   ```bash
   .venv/bin/k8s-capacity admission --config examples/run_admission.yaml --spec examples/admission_api_resize.yaml
   ```
   Ожидается вердикт `DOES_NOT_FIT` и рекомендация `maxSurge=0, maxUnavailable=1`. Сверьте с `examples/output/admission_api_resize.md`. Отличаться должны только текстовые колонки «Пояснение» и «Рекомендация».

5. **Аудит живого кластера:**
   ```bash
   .venv/bin/k8s-capacity collect --config examples/kind-dev01/run_audit.yaml --out runs/kind-dev01-snapshot.json
   .venv/bin/k8s-capacity audit --config examples/kind-dev01/run_audit.yaml --snapshot runs/kind-dev01-snapshot.json
   ```

6. **Что проверить в `runs/<время>-<режим>-<кластер>/`:**
   ```bash
   R=$(ls -td runs/*/ | head -1)
   ls $R/results                                   # какие инструменты реально вызывали агенты
   .venv/bin/python -c "import json,sys; r=json.load(open('$R/report.json')); n=r['narrative']; \
     print('narrative:', bool(n)); print('verdict code/LLM:', r['computed'].get('verdict'), (n or {}).get('verdict'))"
   ```
   - `narrative: True`: LLM вернул валидную Pydantic-модель. Если `False`, отчёт собран только кодом.
   - В `results/` для audit есть `collect_cluster_snapshot`, `capacity_model`, `rollout_reserve_*`, `simulate_node_failure`, `audit`; для admission — `simulate_admission` и, если вердикт не FITS, `search_remediations`. Если какого-то файла нет, агент пропустил инструмент и мог «посчитать в уме».
   - Вердикты кода и LLM совпадают (это гарантирует guardrail; при расхождении в rationale появится пометка «исправлено»).
   - Числа в `report.md` идентичны прогону с `--no-llm` на том же снимке. Изменилось — ищите ошибку в коде, а не в LLM.

Подробный лог шагов агентов печатается в консоль (`verbose: true`). Агенту доступны только инструменты чтения, команд записи в кластер в коде нет.

## 5. Демо-кластер и примеры результата

Генерация: `tests/fixtures/make_demo_cluster.py`. Кластер состоит из 4 узлов:

- пул `general`: w1, w2, w3 (3800m / 14.5Gi allocatable, зоны a/b/c);
- пул `highmem`: g1 (7800m / 60Gi, taint `dedicated=highmem:NoSchedule`).

Готовые отчёты (`--no-llm`, детерминированная часть) лежат в `examples/output/`:

| Сценарий | Итог | Что показывает |
|---|---|---|
| `audit.md` | N−1 = DEGRADED, 11 рисков | Дедлок `shop/api`: anti-affinity «один на узел», 3 реплики на 3 узлах, maxUnavailable 0. Rolling update `spark` невозможен. Local PV у `postgres`. PDB блокирует drain. BestEffort-Pod |
| `admission_checkout_new.md` | DOES_NOT_FIT → «+2 узла general» | **На подходящих узлах суммарно 4400m ≥ 3000m, но 2 Pod по 1500m не размещаются**: per node свободно 2100/1300/1000m |
| `admission_api_resize.md` | DOES_NOT_FIT → maxSurge=0/maxUnavailable=1 | Итоговое состояние помещается, но rollout изменения упирается в дедлок anti-affinity |
| `admission_cache_manifest.md` | DOES_NOT_FIT → replicas 3→2 | Zone spread DoNotSchedule: при потере w1 Pod некуда поставить, потому что единственный другой узел зоны a закрыт taint |

При запуске с LLM в `report.json` появляется `narrative` (`AuditNarrative` / `AdmissionNarrative`): объяснение каждой проверки, рекомендованное изменение с компромиссами, findings с `evidence_refs`, уровень уверенности и допущения.

## 6. Что не покрыто и как добавить

| Не покрыто | Как добавить |
|---|---|
| Argo Rollouts / Flagger (canary, blue-green) | Новый `RolloutSim`-режим: canary = surge на весь шаг, blue-green = ×2 реплик |
| HPA: реплики меняются во время rollout и при N−1 | Брать `maxReplicas` HPA как худший случай в `reserve_analysis` и N−1 (опция политики) |
| Karpenter: подбор типа инстанса, consolidation | В `_autoscaler_recovery` перебирать типы из NodePool requirements, а не копию упавшего узла |
| Отказ зоны (N−zone) и двух узлов | `simulate_node_failure` по списку узлов, где список — все узлы зоны (функция уже принимает state) |
| Время старта и readiness, `minReadySeconds`, `progressDeadlineSeconds` | Добавить тики времени в `run_rollouts` (сейчас новые Pod сразу Ready) |
| DRA (ResourceClaims), GPU sharing/MIG, NUMA / Topology Manager | Новые фильтры в `ClusterState.filter`; до этого GPU считается как extended resource |
| Calico IPAM blocks / пул IP кластера | Cluster-level ресурс `pod-ips` с общим пулом и проверка исчерпания блоков |
| Scheduler profiles и кастомные плагины, `MostAllocated` per profile | Чтение KubeSchedulerConfiguration и выбор scoring по `schedulerName` |
| Квоты по object count (`count/deployments` и т. п.), скоупы Terminating | Расширить `QuotaLedger.pod_usage` и `_applies` |
| Ресурсы `kube-reserved` при изменении флагов kubelet | Сейчас берётся текущий allocatable. Для what-if — параметр «новый allocatable пула» |
| Масштаб > ~200 узлов и 10k Pod | Индексы Pod по label/namespace в `ClusterState`, сэмплирование N−1 уже есть (`n_minus_1_max_nodes`) |

## 7. Самопроверка по критериям качества

- **Вердикт без per-node проверки невозможен.** `steady_state_placement` размещает каждую реплику фильтрами планировщика. Сумма по кластеру показывается только как пояснение (`naive_sum_says_fits`). Тест: `test_sum_is_enough_but_does_not_fit`.
- **Rolling update учитывает surge, квоту и топологию.** Эмуляция контроллера на реальной раскладке; Pending-Pod списываются с квоты. Тесты: `test_quota_charged_for_surge`, `test_anti_affinity_deadlock_static_and_simulated`, `test_resize_*`.
- **N−1 учитывает DaemonSet, local PV, PDB и приоритеты.** Тесты: `test_node_failure_rules`, `test_preemption_reported_separately`, `test_autoscaler_recovery`.
- **Расчёты в коде.** `engine/` не импортирует CrewAI. Guardrail отклоняет ответ LLM с вердиктом, отличным от вычисленного (`test_admission_guardrail_blocks_verdict_override`).
- **Read-only.** Коллектор использует только `list_*` / `get_code`; RBAC — только `get`/`list`. Записывать в кластер нечем.
