# Admission: `Deployment/shop/api` (resize, replicas=3)

## Вердикт: ❌ **DOES_NOT_FIT** (уверенность: high)

Эффективные requests одного Pod: `{'cpu': '700m', 'memory': '1Gi', 'pod-ips': '1', 'pods': '1'}`

| Проверка | Статус | Результат (код) | Пояснение |
|---|---|---|---|
| namespace_quota | PASS | 3 pod(s) fit the namespace quota |  |
| steady_state_placement | PASS | all replicas placed: {'w1': 1, 'w2': 1, 'w3': 1} |  |
| app_rollout | FAIL | rollout of this change gets stuck: api-resize-1: 0/4 nodes are available: 3 node(s) didn't match pod anti-affinity rules, 3 node(s) didn't satisfy existing pods anti-affinity rules, 1 node(s) had untolerated taint {dedicated: highmem} |  |
| others_update_reserve | PASS | update reserve of other workloads preserved (2 group(s) were already infeasible before the change) |  |
| n_minus_1_after | PASS | N-1 not degraded by the change for pods with priority >= 0 (after: DEGRADED, before: DEGRADED); pre-existing node-failure risk, see audit |  |

### Условия / причины

- app_rollout: rollout of this change gets stuck: api-resize-1: 0/4 nodes are available: 3 node(s) didn't match pod anti-affinity rules, 3 node(s) didn't satisfy existing pods anti-affinity rules, 1 node(s) had untolerated taint {dedicated: highmem}

### Минимальные изменения, при которых проходит

| Вариант | Изменение | Вердикт |
|---|---|---|
| strategy | maxSurge=0, maxUnavailable=1 (rollout without surge, -1 replica during update) | FITS |
| replicas | replicas 3 -> 2 | FITS |
| add_nodes | +1 node(s) like w3 in pool general | FITS |

### Допущения

- Фит считается по requests (как kube-scheduler), а не по фактическому потреблению; usage — только для рисков.
- free = allocatable − Σ requests всех нетерминальных Pod на узле (включая DaemonSet, static и системные Pod).
- Эффективный request Pod = max(max(init + ранее стартовавшие sidecar), Σ containers + Σ sidecar) + overhead.
- Выбор узла среди подходящих эмулирует scoring по умолчанию; при равенстве — по имени узла (реальный планировщик выбирает случайно).
- Пересоздание Pod при N−1 — в порядке приоритета, затем по имени; вытесненные preemption Pod повторно не планируются.
- Rolling update моделируется до перехода новых Pod в Ready; время старта/readiness не учитывается.
- Модель одновременных обновлений: sequential
- IPAM: {"mode": "podcidr", "reserved_per_cidr": 3}
- Autoscaler: нет (новые узлы не появляются)
- Требование N−1: обязательно; резерв под обновления других: обязателен
