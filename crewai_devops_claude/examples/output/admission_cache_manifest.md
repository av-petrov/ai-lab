# Admission: `StatefulSet/shop/cache` (new, replicas=3)

## Вердикт: ❌ **DOES_NOT_FIT** (уверенность: high)

Эффективные requests одного Pod: `{'cpu': '250m', 'memory': '1Gi', 'pod-ips': '1', 'pods': '1'}`

| Проверка | Статус | Результат (код) | Пояснение |
|---|---|---|---|
| namespace_quota | PASS | 3 pod(s) fit the namespace quota |  |
| steady_state_placement | PASS | all replicas placed: {'w1': 1, 'w2': 1, 'w3': 1} |  |
| app_rollout | PASS | next rolling update of the application after deploy completes with surge 0 |  |
| others_update_reserve | PASS | update reserve of other workloads preserved (2 group(s) were already infeasible before the change) |  |
| n_minus_1_after | FAIL | node failure tolerance degrades on 1 node(s), e.g. loss of w1: DEGRADED -> DEGRADED, pending pods 1 -> 2 (new: shop/cache-0) |  |

### Условия / причины

- n_minus_1_after: node failure tolerance degrades on 1 node(s), e.g. loss of w1: DEGRADED -> DEGRADED, pending pods 1 -> 2 (new: shop/cache-0)

### Минимальные изменения, при которых проходит

| Вариант | Изменение | Вердикт |
|---|---|---|
| replicas | replicas 3 -> 2 | FITS |

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
