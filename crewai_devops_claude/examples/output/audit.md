# Аудит ёмкости кластера `demo`
_Снимок: 2026-11-19T08:00:00Z_

## Сводка по пулам

| Пул | Узлов | CPU % | RAM % | Pods % | IP % | Ограничивающий ресурс | Эталонный Pod | Запас (Pod) | Фрагментация |
|---|---|---|---|---|---|---|---|---|---|
| general | 3 | 61.4 | 35.7 | 4.5 | 2.0 | **cpu** | medium {'cpu': '1', 'memory': '2Gi'} | 4 | 0.0% |
| highmem | 1 | 52.6 | 53.5 | 1.8 | 0.8 | **cpu** | medium {'cpu': '1', 'memory': '2Gi'} | 3 | 0.0% |

## Резерв под обновления

- Модель: `sequential`
- Требуемый резерв (аналитически, ceil(maxSurge×replicas)×requests): `{'cpu': '4', 'memory': '32Gi', 'pod-ips': '1', 'pods': '1'}`
- Пик по симуляции на реальной раскладке: `{'cpu': '1', 'memory': '2Gi', 'pod-ips': '1', 'pods': '1'}`
- Определяется нагрузками: `{'cpu': ['Deployment/analytics/spark'], 'memory': ['Deployment/analytics/spark']}`
- Помещается топологически: **нет** (невыполнимых групп: 2, дедлоков по конструкции: 1)
  - `Deployment/analytics/spark`: spark-upd-1: 0/4 nodes are available: 4 Insufficient cpu, 4 Insufficient memory
  - `Deployment/shop/api`: api-upd-1: 0/4 nodes are available: 3 node(s) didn't match pod anti-affinity rules, 3 node(s) didn't satisfy existing pods anti-affinity rules, 1 node(s) had untolerated taint {dedicated: highmem}

## Отказ одного узла (N−1)

Итог: **DEGRADED**, худший узел: `g1` (all nodes)

| Узел | Пул | Итог | Pending после отказа | С вытеснением | Потеряны | Local PV |
|---|---|---|---|---|---|---|
| g1 | highmem | DEGRADED | analytics/spark-2c4-0 | – | – | – |
| w1 | general | DEGRADED | shop/api-7f9c-0 | – | dev/debug | – |
| w2 | general | DEGRADED | shop/api-7f9c-1 | – | – | – |
| w3 | general | DEGRADED | shop/api-7f9c-2 | – | – | data/postgres-0 |

## Узлы

| Узел | Пул | Зона | Свободно CPU | Свободно RAM | Свободно Pods | CPU % | RAM % | Самый загруженный ресурс |
|---|---|---|---|---|---|---|---|---|
| g1 | highmem | zone-a | 3700m | 27.88Gi | 108 | 52.6 | 53.5 | memory |
| w1 | general | zone-a | 2100m | 11.31Gi | 105 | 44.7 | 22.0 | cpu |
| w2 | general | zone-b | 1300m | 9.31Gi | 105 | 65.8 | 35.8 | cpu |
| w3 | general | zone-c | 1 | 7.38Gi | 105 | 73.7 | 49.1 | cpu |

## Риски

| ID | Severity | Риск | Доказательство | Рекомендация |
|---|---|---|---|---|
| R01 | critical | Loss of any of [g1] leaves pods of Deployment/analytics/spark unschedulable | n_minus_1.scenarios[g1].pending_after_failure; analytics/spark-2c4-0: 0/3 nodes are available: 3 Insufficient cpu, 3 Insufficient memory | Add N+1 headroom in pool highmem for these pods, or relax their hard placement constraints. |
| R02 | critical | Loss of any of [w1, w2, w3] leaves pods of Deployment/shop/api unschedulable | n_minus_1.scenarios[w1].pending_after_failure; n_minus_1.scenarios[w2].pending_after_failure; n_minus_1.scenarios[w3].pending_after_failure; shop/api-7f9c-0: 0/3 nodes are available: 2 node(s) didn't match pod anti-affinity rules, 2 node(s) didn't satisfy existing pods anti-affinity rules, 1 node(s) | Add N+1 headroom in pool general for these pods, or relax their hard placement constraints. |
| R03 | high | Rolling update of Deployment/analytics/spark cannot complete | update_reserve.groups_infeasible[Deployment/analytics/spark]: spark-upd-1: 0/4 nodes are available: 4 Insufficient cpu, 4 Insufficient memory | Free capacity where the surge pod can land, set maxUnavailable>=1, or relax required anti-affinity / spread. |
| R04 | high | Rolling update of Deployment/shop/api cannot complete | update_reserve.groups_infeasible[Deployment/shop/api]: api-upd-1: 0/4 nodes are available: 3 node(s) didn't match pod anti-affinity rules, 3 node(s) didn't satisfy existing pods anti-affinity rules, 1 node(s) had untolerated taint {dedicated: highmem} | Free capacity where the surge pod can land, set maxUnavailable>=1, or relax required anti-affinity / spread. |
| R05 | high | Rolling-update deadlock by construction | update_reserve.static_deadlocks: Deployment/shop/api: required podAntiAffinity on kubernetes.io/hostname (one pod per domain) with replicas=3 >= eligible domains=3 and maxUnavailable=0: the surge pod has no domain to go to | Set maxUnavailable>=1 (maxSurge=0) or add an eligible node/domain. |
| R06 | high | 2 pod(s) use more memory than requested | capacity.hidden_risks.memory_usage_above_requests: [{'pod': 'shop/worker-66b-0', 'requests': '1Gi', 'usage': '1.37Gi'}, {'pod': 'shop/worker-66b-1', 'requests': '1Gi', 'usage': '1.32Gi'}] | Raise memory requests to observed peak; these pods are first to be evicted under memory pressure. |
| R07 | medium | 1 pod(s) on w3 use local PVs and cannot move | n_minus_1.scenarios[w3].unmovable_local_pv_pods: ['data/postgres-0'] | Ensure application-level replication for local-PV workloads (data stays on the failed node). |
| R08 | medium | PDB shop/api blocks draining 3 node(s) | n_minus_1.scenarios[*].pdb_blocking_voluntary_drain: w1 (allowed=0, on node=1), w2 (allowed=0, on node=1), w3 (allowed=0, on node=1) | Increase replicas or relax minAvailable so node maintenance can proceed. |
| R09 | medium | 1 BestEffort pod(s) are invisible to the scheduler | capacity.hidden_risks.besteffort_pods: ['dev/debug'] | Set requests (LimitRange defaultRequest per namespace) so their consumption is accounted. |
| R10 | low | 1 pod(s) on w1 have no controller and are lost | n_minus_1.scenarios[w1].lost_pods: ['dev/debug'] | Run these pods under a controller (Deployment/StatefulSet). |
| R11 | low | 1 pod(s) request far more than they use | capacity.hidden_risks.requests_far_above_usage: [{'pod': 'analytics/spark-2c4-0', 'requests': {'cpu': '4', 'memory': '32Gi'}, 'usage': {'cpu': '400m', 'memory': '6Gi'}}] | Right-size requests (VPA recommendations) to recover schedulable capacity. |

## Допущения и пробелы в данных

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
