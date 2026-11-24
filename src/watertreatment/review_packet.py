"""Small source-mapped review packets; never a substitute for scientific validation."""
from __future__ import annotations

import json
from pathlib import Path

from .domain import FINAL_ROLES, ExtractionRecord
from .reading import ReadingAudit


def _fact(fact):
    value = fact.value if fact.value is not None else fact.note or fact.raw or "未给出值"
    return f"{value} [{fact.status.value}]"


def select_observations(candidate: ExtractionRecord):
    """Prefer the final system's explicit pair, then distinct stability experiments.

    This is a reproducible review sample, not an accuracy score or a ranking of
    scientific evidence. Source-order ties remain stable; controls cannot displace
    available final-system results simply because they appear earlier in JSON.
    """
    materials = {m.id: m for m in candidate.materials}
    experiments = {e.id: e for e in candidate.experiments}
    observations = {o.id: o for o in candidate.observations}

    def is_final(observation):
        if observation.material_id:
            material = materials.get(observation.material_id)
            return bool(material and material.role in FINAL_ROLES)
        experiment = experiments.get(observation.experiment_id)
        return bool(experiment and any(materials.get(mid) and materials[mid].role in FINAL_ROLES
                                       for mid in experiment.material_ids))

    final = [o for o in candidate.observations if is_final(o)]
    selected = []
    coupled = sorted((e for e in candidate.experiments if e.purpose.value == "coupled"),
                     key=lambda e: not any(materials.get(mid) and materials[mid].role.value == "primary_final"
                                           for mid in e.material_ids))
    for experiment in coupled:
        for pair in experiment.pairs:
            members = [observations.get(pair.left_observation_id), observations.get(pair.right_observation_id)]
            if all(o is not None and is_final(o) for o in members):
                selected.extend(members)
                break
        if selected:
            break
    stability = [o for o in final if experiments.get(o.experiment_id)
                 and experiments[o.experiment_id].purpose.value == "stability"]
    stability_groups = set()
    stability_experiments = set()
    for observation in stability:
        text = (observation.metric_kind + " " + str(observation.metric.value)).casefold()
        group = ("water" if any(word in text for word in ("evap", "condensate")) else
                 "leaching" if "leach" in text else
                 "pollutant" if experiments[observation.experiment_id].targets or
                    any(word in text for word in ("catal", "degrad", "treatment")) else "other")
        if group not in stability_groups:
            selected.append(observation)
            stability_groups.add(group)
            stability_experiments.add(observation.experiment_id)
            if len(stability_groups) == 2:
                break
    for observation in stability:
        if len(stability_experiments) >= 2:
            break
        if observation.experiment_id not in stability_experiments:
            selected.append(observation)
            stability_experiments.add(observation.experiment_id)
    remaining = sorted(final, key=lambda o: (o.importance != "core",
                       not (experiments.get(o.experiment_id) and experiments[o.experiment_id].targets)))
    selected += [o for o in remaining if o not in selected]
    selected += [o for o in candidate.observations if o not in selected]
    return list({o.id: o for o in selected}.values())[:6]


def review_packet(run: Path, candidate: ExtractionRecord, audit: ReadingAudit | None,
                  result: dict, *, destination: Path | None = None):
    """Write a new packet, allowing improved exports without rewriting old runs."""
    lines = ["少量人工抽查包（模型候选，非人工已验证数据）", f"文献: {candidate.paper.title.value}",
             f"DOI: {candidate.paper.doi.value}", f"状态: {result['status']}", f"来源运行: {run}",
             "先核对最终材料，再核对下面最多6项结果；同时从原文主要性能图反向查漏。",
             "优先呈现最终体系的显式性能对及稳定性；这组抽查不能代表整篇准确率。", "", "来源文件:"]
    if audit and audit.review_scope in {"citation_delta", "scientific_delta"}:
        lines.insert(5, "本次为证据引用及关联范围的局部复核，其他范围继承已绑定的前一完整审查；不是全篇重新审查。")
    bundle_path = run / "sources.json"
    if bundle_path.exists():
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        for doc in bundle["documents"]:
            source = doc["source"]
            original = source.get("metadata", {}).get("original_path", source["path"])
            lines += [f"- {source['source_id']} ({source['role']}, {source['format']}): {original}",
                      f"  冻结原件: {source['path']}"]
    materials = {m.id: m for m in candidate.materials}
    experiments = {e.id: e for e in candidate.experiments}
    substances = {s.id: s for s in candidate.substances}
    evidence = {e.id: e for e in candidate.evidence}
    lines.append("\n最终材料:")
    for material in candidate.materials:
        if material.role in FINAL_ROLES:
            lines.append(f"- {material.id}: {_fact(material.name)} ({material.role.value})")
    lines.append("\n抽查结果:")
    for number, observation in enumerate(select_observations(candidate), 1):
        material = materials.get(observation.material_id)
        lines += [f"{number}. {observation.id}: {observation.metric.value} = {_fact(observation.value)}; 单位={_fact(observation.unit)}",
                  f"  材料: {material.name.value if material else observation.material_id}; 实验: {observation.experiment_id}; 数值来源: {observation.origin.value}"]
        experiment = experiments.get(observation.experiment_id)
        if experiment:
            lines.append(f"  实验目的: {experiment.purpose.value}; 同步运行证据: {experiment.joint_evidence.level.value}")
            for target in experiment.targets:
                substance = substances.get(target.substance_id)
                lines.append(f"  污染物: {substance.name.value if substance else target.substance_id}; 浓度={_fact(target.concentration)} {_fact(target.unit)}")
            for reaction in experiment.reaction_inputs:
                substance = substances.get(reaction.substance_id)
                lines.append(f"  反应投入: {reaction.role}; {substance.name.value if substance else reaction.substance_id}; 来源={reaction.source.value}; 用量={_fact(reaction.amount)} {_fact(reaction.unit)}")
            for energy in experiment.energy_inputs:
                parameters = "; ".join(f"{p.name}={_fact(p.fact)} {_fact(p.unit)}" for p in energy.parameters)
                lines.append(f"  能量条件: {energy.kind}; {parameters}")
            for pair in experiment.pairs:
                if observation.id in {pair.left_observation_id, pair.right_observation_id}:
                    lines.append(f"  数值配对: {pair.level.value}; {pair.left_observation_id} / {pair.right_observation_id}; 未决条件: {'; '.join(pair.unresolved_conditions)}")
        lines.append(f"  观测时间: {_fact(observation.time_window.start)} → {_fact(observation.time_window.end)} {_fact(observation.time_window.unit)}; {_fact(observation.time_window.reference)}")
        ids = list(dict.fromkeys(observation.value.evidence_ids + observation.evidence_ids))
        for evidence_id in ids[:2]:
            item = evidence.get(evidence_id)
            if item:
                lines.append(f"  证据 {item.source_id} / {item.block_id}; {item.locator.model_dump(exclude_none=True)}; 原文: {item.quote}")
        lines += ["  限制: " + item for item in observation.limitations[:2]]
    pending = []
    if audit:
        pending += [f"{f.record_path}: {f.explanation}" for f in audit.findings
                    if f.needs_revision or f.verdict in {"unsupported", "unresolved", "not_checked"}]
        pending += audit.potential_omissions + audit.coverage_limitations
        pending += ["局部补抽（" + ", ".join(item.coverage_ids) + "; 受限用途: "
                    + ", ".join(item.affected_query_types) + "）: " + item.recommended_action
                    for item in audit.quantitative_followups]
    pending += result.get("reading_check_issues", [])
    lines.append("\n待处理问题（最多列出8项，完整内容见来源运行中的audit、validation与result文件）:")
    lines += ["- " + item for item in pending[:8]] or ["- 当前检查未列出待处理项；仍须原文抽查，不能解释为已经人工验证。"]
    if len(pending) > 8:
        lines.append(f"另有 {len(pending)-8} 项，详见完整审查记录。")
    target = destination or run / "human_spot_check.txt"
    with target.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines))
    return target
