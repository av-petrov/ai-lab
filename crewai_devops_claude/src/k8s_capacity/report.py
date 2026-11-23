"""Final report assembly: computed (code) + narrative (LLM) -> report.json + report.md.

All numbers in the markdown come from the computed part. The narrative only adds
explanations, and the verdict shown is always the computed one.
"""

# no `from __future__ import annotations` here: CrewAI inspects the guardrails'
# return annotation at runtime and needs a real Tuple[bool, Any].
import json
from typing import Any, Tuple

from . import runtime
from .engine.admission import search_remediations, simulate_admission
from .engine.audit import run_audit
from .models import (
    AdmissionComputed,
    AdmissionNarrative,
    AdmissionReport,
    AuditComputed,
    AuditNarrative,
    AuditReport,
    NodeRow,
    PoolSummary,
    RemediationOption,
    Risk,
)

BASE_ASSUMPTIONS = [
    "Фит считается по requests (как kube-scheduler), а не по фактическому потреблению; usage — только для рисков.",
    "free = allocatable − Σ requests всех нетерминальных Pod на узле (включая DaemonSet, static и системные Pod).",
    "Эффективный request Pod = max(max(init + ранее стартовавшие sidecar), Σ containers + Σ sidecar) + overhead.",
    "Выбор узла среди подходящих эмулирует scoring по умолчанию; при равенстве — по имени узла (реальный планировщик выбирает случайно).",
    "Пересоздание Pod при N−1 — в порядке приоритета, затем по имени; вытесненные preemption Pod повторно не планируются.",
    "Rolling update моделируется до перехода новых Pod в Ready; время старта/readiness не учитывается.",
]


def _config_assumptions(cfg) -> list[str]:
    out = [f"Модель одновременных обновлений: {cfg.update_concurrency}"
           + (f" (группы: {cfg.custom_update_groups})" if cfg.custom_update_groups else "")]
    out.append(f"IPAM: {json.dumps(cfg.ipam, ensure_ascii=False)}")
    out.append("Autoscaler: " + (f"{cfg.autoscaler.kind}, группы {[g.name for g in cfg.autoscaler.node_groups]}"
                                 if cfg.autoscaler.enabled else "нет (новые узлы не появляются)"))
    out.append(f"Требование N−1: {'обязательно' if cfg.require_n_minus_1 else 'желательно'}; "
               f"резерв под обновления других: {'обязателен' if cfg.require_update_reserve else 'желателен'}")
    return out


def _assumptions(cfg, narrative) -> list[str]:
    return BASE_ASSUMPTIONS + _config_assumptions(cfg) + (narrative.assumptions if narrative else [])


# =========================================================================== audit
def build_audit_report(narrative: AuditNarrative | None) -> AuditReport:
    cfg = runtime.config()
    ctx = runtime.context()
    res = run_audit(ctx, cfg.audit_config())
    runtime.save_result("audit_final", res)
    nodes = [NodeRow(**{k: n[k] for k in NodeRow.model_fields}) for n in res["capacity"]["nodes"]]
    risks = [Risk(**r) for r in res["risks"]]
    if narrative:
        for i, r in enumerate(narrative.additional_risks, 1):
            risks.append(r.model_copy(update={"id": f"A{i:02d}", "source": "analyst"}))
    n1 = res["n_minus_1"]
    computed = AuditComputed(
        cluster=res["cluster"], collected_at=res["collected_at"],
        pools=[PoolSummary(**p) for p in res["summary"]["pools"]],
        update_reserve={**res["summary"]["update_reserve"],
                        "groups_infeasible": res["update_reserve"]["groups_infeasible"],
                        "static_deadlock_details": res["update_reserve"]["static_deadlocks"]},
        n_minus_1={**res["summary"]["n_minus_1"],
                   "scenarios": [{k: s[k] for k in ("node", "pool", "verdict", "pending_after_failure",
                                                    "rescheduled_with_preemption", "lost_pods",
                                                    "unmovable_local_pv_pods", "autoscaler")}
                                 for s in n1["scenarios"]]},
        nodes=nodes, risks=risks, data_gaps=res["data_gaps"] + res["capacity"]["unverified"],
    )
    return AuditReport(computed=computed, narrative=narrative, assumptions=_assumptions(cfg, narrative))


def render_audit_md(rep: AuditReport) -> str:
    c, n = rep.computed, rep.narrative
    L = [f"# Аудит ёмкости кластера `{c.cluster}`", f"_Снимок: {c.collected_at}_", ""]
    if n:
        L += ["## Резюме", n.executive_summary, ""]
    L += ["## Сводка по пулам", "",
          "| Пул | Узлов | CPU % | RAM % | Pods % | IP % | Ограничивающий ресурс | Эталонный Pod | Запас (Pod) | Фрагментация |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for p in c.pools:
        u = p.utilization_pct
        L.append(f"| {p.pool} | {p.schedulable_nodes} | {u.get('cpu', '–')} | {u.get('memory', '–')} | "
                 f"{u.get('pods', '–')} | {u.get('pod-ips', 'н/д')} | **{p.limiting_resource}** | "
                 f"{p.reference_pod} | {p.headroom_reference_pods} | {p.fragmentation_pct}% |")
    ur = c.update_reserve
    L += ["", "## Резерв под обновления", "",
          f"- Модель: `{ur['concurrency_model']}`",
          f"- Требуемый резерв (аналитически, ceil(maxSurge×replicas)×requests): `{ur['required_analytic']}`",
          f"- Пик по симуляции на реальной раскладке: `{ur['required_simulated_peak']}`",
          f"- Определяется нагрузками: `{ur.get('dominated_by')}`",
          f"- Помещается топологически: **{'да' if ur['fits_topologically'] else 'нет'}** "
          f"(невыполнимых групп: {ur['infeasible_groups']}, дедлоков по конструкции: {ur['static_deadlocks']})"]
    for g in ur["groups_infeasible"]:
        for s in g["stuck"]:
            L.append(f"  - `{s['workload']}`: {s['reason']}")
    nm = c.n_minus_1
    L += ["", "## Отказ одного узла (N−1)", "",
          f"Итог: **{nm['overall']}**, худший узел: `{nm['worst_node']}` ({nm['coverage']})", "",
          "| Узел | Пул | Итог | Pending после отказа | С вытеснением | Потеряны | Local PV |", "|---|---|---|---|---|---|---|"]
    for s in nm["scenarios"]:
        L.append(f"| {s['node']} | {s['pool']} | {s['verdict']} | "
                 f"{', '.join(p['pod'] for p in s['pending_after_failure']) or '–'} | "
                 f"{', '.join(p['pod'] for p in s['rescheduled_with_preemption']) or '–'} | "
                 f"{', '.join(s['lost_pods']) or '–'} | {', '.join(s['unmovable_local_pv_pods']) or '–'} |")
    L += ["", "## Узлы", "", "| Узел | Пул | Зона | Свободно CPU | Свободно RAM | Свободно Pods | CPU % | RAM % | Самый загруженный ресурс |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in c.nodes:
        flag = "" if r.ready and r.schedulable else " ⚠"
        L.append(f"| {r.node}{flag} | {r.pool} | {r.zone or '–'} | {r.free.get('cpu')} | {r.free.get('memory')} | "
                 f"{r.free.get('pods')} | {r.utilization_pct.get('cpu')} | {r.utilization_pct.get('memory')} | {r.most_used_resource} |")
    L += ["", "## Риски", "", "| ID | Severity | Риск | Доказательство | Рекомендация |", "|---|---|---|---|---|"]
    for r in c.risks:
        src = " _(аналитик)_" if r.source == "analyst" else ""
        L.append(f"| {r.id} | {r.severity} | {r.title}{src} | {'; '.join(r.evidence)[:300]} | {r.recommendation} |")
    if n:
        L += ["", "## Ключевые выводы", ""]
        for f in n.key_findings:
            L.append(f"- {f.statement} _(уверенность: {f.confidence}; источники: {', '.join(f.evidence_refs)})_")
        L += ["", "## Приоритетные действия", ""] + [f"{i}. {a}" for i, a in enumerate(n.prioritized_actions, 1)]
        if n.open_questions:
            L += ["", "## Открытые вопросы", ""] + [f"- {q}" for q in n.open_questions]
    L += ["", "## Допущения и пробелы в данных", ""] + [f"- {a}" for a in rep.assumptions] + \
         [f"- ⚠ {g}" for g in c.data_gaps]
    return "\n".join(L) + "\n"


# =========================================================================== admission
def build_admission_report(narrative: AdmissionNarrative | None) -> AdmissionReport:
    cfg = runtime.config()
    ctx = runtime.context()
    spec = cfg.admission.to_engine()
    res = simulate_admission(ctx, spec, cfg.policy())
    remediation = []
    if res["verdict"] != "FITS":
        rem = runtime.load_result("search_remediations")
        if rem is None:
            rem = search_remediations(ctx, spec, cfg.policy())
        remediation = [RemediationOption(**o) for o in rem["options"]]
    computed = AdmissionComputed(
        workload=res["workload"], change_type=res["change_type"], replicas=res["replicas"],
        pod_effective_requests=res["pod_effective_requests"], verdict=res["verdict"], conditions=res["conditions"],
        confidence=res["confidence"], checks=res["checks"], not_verified=res["not_verified"], remediation=remediation,
    )
    if narrative is not None and narrative.verdict != computed.verdict:
        narrative = narrative.model_copy(update={
            "verdict_rationale": f"[исправлено: вердикт кода {computed.verdict}] " + narrative.verdict_rationale,
            "verdict": computed.verdict})
    return AdmissionReport(computed=computed, narrative=narrative, assumptions=_assumptions(cfg, narrative))


def render_admission_md(rep: AdmissionReport) -> str:
    c, n = rep.computed, rep.narrative
    icon = {"FITS": "✅", "FITS_WITH_CONDITIONS": "⚠️", "DOES_NOT_FIT": "❌"}[c.verdict]
    L = [f"# Admission: `{c.workload}` ({c.change_type}, replicas={c.replicas})", "",
         f"## Вердикт: {icon} **{c.verdict}** (уверенность: {c.confidence})", "",
         f"Эффективные requests одного Pod: `{c.pod_effective_requests}`", ""]
    if n:
        L += [n.verdict_rationale, ""]
    expl = {e.check: e.explanation for e in (n.checks if n else [])}
    L += ["| Проверка | Статус | Результат (код) | Пояснение |", "|---|---|---|---|"]
    for ch in c.checks:
        L.append(f"| {ch.check} | {ch.status} | {ch.summary} | {expl.get(ch.check, '')} |")
    sp = next((ch for ch in c.checks if ch.check == "steady_state_placement"), None)
    naive = (sp.details or {}).get("cluster_free_sum_would_suggest") if sp else None
    if naive and naive.get("naive_sum_says_fits") and sp.status == "FAIL":
        L += ["", f"> Сумма свободных ресурсов на подходящих узлах {naive['eligible_nodes']} "
                  f"({naive['eligible_free']}; по всему кластеру {naive['cluster_free']}) покрывает потребность "
                  f"({naive['needed']}), но per-node проверка показывает, что приложение не размещается."]
    if c.conditions:
        L += ["", "### Условия / причины", ""] + [f"- {x}" for x in c.conditions]
    if c.remediation:
        L += ["", "### Минимальные изменения, при которых проходит", "", "| Вариант | Изменение | Вердикт |", "|---|---|---|"]
        for o in c.remediation:
            L.append(f"| {o.option} | {o.change} | {o.verdict} |")
    if n and n.recommended_change:
        L += ["", f"**Рекомендация:** {n.recommended_change}"]
    if c.not_verified:
        L += ["", "### Не проверено", ""] + [f"- {x}" for x in c.not_verified]
    L += ["", "### Допущения", ""] + [f"- {a}" for a in rep.assumptions]
    return "\n".join(L) + "\n"


# =========================================================================== guardrails
def admission_guardrail(output: Any) -> Tuple[bool, Any]:
    """The LLM verdict must equal the verdict computed by simulate_admission."""
    nar = getattr(output, "pydantic", None)
    if nar is None:
        return False, "Output must match the AdmissionNarrative schema."
    computed = runtime.load_result("simulate_admission")
    if computed is None:
        return False, "Call simulate_admission first; the verdict comes from code."
    if nar.verdict != computed["verdict"]:
        return False, (f"verdict {nar.verdict} differs from the computed verdict {computed['verdict']}. "
                       "Use the computed verdict; explain it, do not override it.")
    if {c.check for c in nar.checks} != {c["check"] for c in computed["checks"]}:
        return False, "Explain every one of the five checks exactly once."
    return True, output


def audit_guardrail(output: Any) -> Tuple[bool, Any]:
    nar = getattr(output, "pydantic", None)
    if nar is None:
        return False, "Output must match the AuditNarrative schema."
    bad = [f.statement[:60] for f in nar.key_findings if not f.evidence_refs]
    if bad:
        return False, f"Every finding needs evidence_refs into tool output; missing for: {bad}"
    return True, output
