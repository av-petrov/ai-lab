# Admission: `Deployment/shop/checkout` (new, replicas=2)

## Вердикт: ❌ **DOES_NOT_FIT** (уверенность: high)

Эффективные requests одного Pod: `{'cpu': '1500m', 'memory': '2Gi', 'pod-ips': '1', 'pods': '1'}`

| Проверка | Статус | Результат (код) | Пояснение |
|---|---|---|---|
| namespace_quota | PASS | 2 pod(s) fit the namespace quota |  |
| steady_state_placement | FAIL | 1 replica(s) cannot be placed: 0/4 nodes are available: 3 Insufficient cpu, 1 node(s) had untolerated taint {dedicated: highmem} |  |
| app_rollout | SKIPPED | steady-state placement failed |  |
| others_update_reserve | SKIPPED | steady-state placement failed |  |
| n_minus_1_after | SKIPPED | steady-state placement failed |  |

> Сумма свободных ресурсов на подходящих узлах ['w1', 'w2', 'w3'] ({'cpu': '4400m', 'memory': '27.99Gi'}; по всему кластеру {'cpu': '8100m', 'memory': '55.86Gi'}) покрывает потребность ({'cpu': '3', 'memory': '4Gi'}), но per-node проверка показывает, что приложение не размещается.

### Условия / причины

- steady_state_placement: 1 replica(s) cannot be placed: 0/4 nodes are available: 3 Insufficient cpu, 1 node(s) had untolerated taint {dedicated: highmem}

### Минимальные изменения, при которых проходит

| Вариант | Изменение | Вердикт |
|---|---|---|
| add_nodes | +2 node(s) like w3 in pool general | FITS |

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
