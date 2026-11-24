"""Resume a failed first extraction from authenticated completed model notes.

The dossier is fallible interpretation, not proof of complete reading. Every
recovery still extracts and audits the sources, within the original call/time
budget. Only this explicit recovery lineage can be followed; no candidate or
later scientific work may be discarded to return to an earlier dossier.
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from .model_policy import plan_model, receipt_matches_plan, request_matches_plan
from .storage import DataStore, digest_json, hash_file, utc_now, write_new_json
from .web_initial_recovery import _json, _parent, _safe

POLICY = "completed-dossier-extraction-recovery-v1"
EVENT = "dossier_extraction_recovery_prepared"


def _reservation(store, parent, current=None, *, reserve=False):
    import json
    with store.connection() as db:
        if reserve:
            db.execute("BEGIN IMMEDIATE")
        for row in db.execute("SELECT run_id,payload FROM run_events WHERE event=?", (EVENT,)):
            if json.loads(row[1]).get("parent_run") == parent and row[0] != current:
                raise ValueError("原抽取剩余预算已有恢复计划，请继续该计划，不能重复预留")
        if reserve:
            db.execute("INSERT INTO run_events(run_id,event,payload,created_at) VALUES (?,?,?,?)",
                (current, EVENT, json.dumps({"parent_run": parent}), utc_now()))


def _latest(store, settings, run, paper_id, current_run):
    rows = store.runs()
    date = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
    parent_time = date(next(r["created_at"] for r in rows if r["run_id"] == run.name))
    for row in rows:
        if row["kind"] != "extraction" or row["run_id"] in {run.name, current_run} or date(row["created_at"]) <= parent_time:
            continue
        other = settings.data_root / "runs" / row["run_id"]
        manifest = _json(_safe(settings, row["manifest_path"]))
        if manifest.get("paper_id") == paper_id and (store.last_event(other.name, "execution_started") is not None
                or list((other / "calls").glob("*/request.json")) or (other / "result.json").exists()
                or (other / "failure.json").exists()):
            raise ValueError("存在更晚的已执行文献运行；不能回退到旧阅读档案重新生成候选")


def _artifacts(settings, run):
    return [{"path": str(_safe(settings, p)), "sha256": hash_file(p)}
            for p in sorted(run.rglob("*")) if p.is_file()]


def _failed_call_started(run, plan, request, output):
    """Bind charged failure to its public start request; never infer a read."""
    import json
    path = run / "app_server/events.jsonl"
    before = hash_file(path)
    starts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        message = event.get("message", {})
        params = message.get("params", {})
        if (event.get("direction") == "send" and message.get("method") == "turn/start"
                and params.get("model") == request["model"] and params.get("effort") == request["reasoning_effort"]
                and [item.get("text") for item in params.get("input", []) if item.get("type") == "text"] == [request.get("prompt")]
                and isinstance(request.get("prompt"), str) and request["prompt"]
                and params.get("outputSchema") == request.get("schema")
                and (not output.get("thread_id") or params.get("threadId") == output["thread_id"])):
            starts.append(event)
    if (len(starts) != 1 or hash_file(path) != before
            or not receipt_matches_plan(plan, request, output)):
        raise ValueError("失败抽取缺少与原请求一致的公开模型调用开始证据")
    # elapsed_seconds is the adapter's monotonic, finally-written duration. UTC
    # event timestamps cannot reconstruct the silent tail of a failed call.
    # Preserve that measured duration; never reset it from a missing end event.


def _authenticate(settings, run, current_run, seen, *, allow_exhausted=False):
    from .audit_addendum_workflow import _completed_call_protocol
    from .coverage_correction import verify_structural_correction
    from .identity_resume import verify_dossier_binding
    from .reading import ReadingDossier
    from .workflow import _read_bundle, _reusable_dossier, scientific_prompt

    store, run, plan, imported, bundle, _ = _parent(settings, run)
    if run.name in seen:
        raise ValueError("阅读恢复父链存在循环")
    _reservation(store, run.name, current_run)
    terminal, failure = _json(run / "result.json"), _json(run / "failure.json")
    if (terminal.get("status") != "reading_incomplete_failure"
            or terminal.get("human_validated") is not False or terminal.get("graph_published") is not False
            or terminal.get("last_candidate_version") is not None or plan.get("reused_candidate")
            or list(run.glob("candidate*.json")) or list(run.glob("audit*.json"))
            or (run / "model_review.json").exists()
            or list((run / "reading_tools").rglob("*submitted_record.json"))
            or list((run / "calls").glob("*/record.json"))):
        raise ValueError("仅已有完整理解档案、首次抽取失败且没有任何候选的运行可从理解阶段继续")
    if hash_file(run / "taxonomy.json") != hash_file(settings.taxonomy_path):
        raise ValueError("原分类体系发生变化，不能在恢复中升级")
    for stem, filename in (("understand", "understand_prompt.txt"), ("extract", "extract_prompt.txt")):
        if (run / filename).read_text(encoding="utf-8").split("\n\nDELIVERY:", 1)[0] != scientific_prompt(settings, stem, reader_model=plan_model(plan)):
            raise ValueError("原科学指令发生变化，不能沿用旧全文理解")
    _, documents = _read_bundle(run / "sources.json")
    structural = run / "reading_structure_correction.v01.json"
    if (structural.exists() or (run / "reading_dossier.structural.v01.json").exists()) and (
            not structural.is_file() or not verify_structural_correction(run, _json(structural), documents)):
        raise ValueError("阅读档案的结构修正来源证明无效")

    descriptor = plan.get("dossier_extraction_recovery")
    previous = None
    grant_inherited = False
    if descriptor:
        receipt = _safe(settings, descriptor["path"])
        if receipt != run / "dossier_extraction_recovery.json" or hash_file(receipt) != descriptor.get("sha256"):
            raise ValueError("阅读恢复父链证明已改变")
        saved = _json(receipt)
        if store.last_event(run.name, EVENT) != {"parent_run": saved["parent_run"]}:
            raise ValueError("理解恢复子运行没有实际取得父预算预留")
        prior_run = settings.data_root / "runs" / saved["parent_run"]
        previous = _authenticate(settings, prior_run, run.name, seen | {run.name})
        if previous != saved or descriptor.get("parent_run") != saved["parent_run"]:
            raise ValueError("阅读恢复父链与原冻结选择不一致")
        origin = _safe(settings, previous["dossier_path"])
        if (not plan.get("reused_dossier") or plan["reused_dossier"].get("path") != str(origin)
                or plan["reused_dossier"].get("sha256") != hash_file(origin)
                or hash_file(run / "reading_dossier.json") != hash_file(origin)
                or plan["max_calls"] > previous["budget"]["remaining_calls"]
                or plan["reading_settings"]["total_timeout_seconds"] > previous["budget"]["remaining_seconds"]
                or plan["reading_settings"].get("max_revisions", 0) > previous["budget"]["max_revisions"]):
            raise ValueError("子运行必须保留原理解档案和父链剩余预算")
    elif allow_exhausted and plan.get("budget_continuation") and plan.get("reused_dossier"):
        from .budget_extension_workflow import verify_extension_plan
        verify_extension_plan(settings, run, plan)
        origin = _safe(settings, plan["reused_dossier"]["path"])
        if (hash_file(origin) != plan["reused_dossier"].get("sha256")
                or hash_file(run / "reading_dossier.json") != hash_file(origin)):
            raise ValueError("追加预算后的理解档案与原已认证解释不一致")
        grant_inherited = True
    else:
        if plan.get("reused_dossier"):
            raise ValueError("缺少可认证的理解恢复父链，不能重新计算预算")
        origin = run / "reading_dossier.json"
    dossier, _ = _reusable_dossier(settings, origin, bundle, plan["cohort_role"])
    if ReadingDossier.model_validate(_json(run / "reading_dossier.json")) != dossier:
        raise ValueError("当前理解档案与原已完成理解不一致")

    calls = {}
    elapsed = 0.0
    for request_path in sorted((run / "calls").glob("*/request.json")):
        envelope, output = _json(_safe(settings, request_path)), _json(_safe(settings, request_path.with_name("result.json")))
        request = envelope.get("request", {})
        stage = request.get("stage")
        if stage not in {"comprehension", "extraction"} or stage in calls:
            raise ValueError("首次抽取恢复不能丢弃重复调用、审查或修订记录")
        sources = {str(_safe(settings, p["path"])): p["sha256"] for p in request.get("sources", [])}
        if (envelope.get("fingerprint") != digest_json(request)
                or not request_matches_plan(plan, request)
                or request.get("code_identity") != plan.get("code_identity") or request.get("rule_version") != plan["rule_version"]
                or any(sources.get(str(_safe(settings, p["path"]))) != p["sha256"] for p in plan["input_paths"])
                or any(hash_file(Path(p)) != sha for p, sha in sources.items())):
            raise ValueError("理解或失败抽取调用的冻结来源、模型或身份无法认证")
        seconds = output.get("elapsed_seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("原调用耗时未知，不能重置剩余预算")
        elapsed += seconds
        if stage == "comprehension":
            from .coverage_transport import verified_dossier
            raw = _json(_safe(settings, request_path.with_name("answer.json")))
            if (previous or output.get("status") != "schema_valid_candidate"
                    or not receipt_matches_plan(plan, request, output)
                    or verified_dossier(raw, bundle, request, output) != verify_dossier_binding(run, plan)):
                raise ValueError("完整理解未由已完成的 Sol MAX 原始回答证明")
            _completed_call_protocol(run, output, raw)
        elif (output.get("status") != "failed" or not output.get("error")
                or failure.get("error") != output["error"] or request_path.with_name("answer.json").exists()
                or output.get("submission_id")):
            raise ValueError("抽取调用不是无候选的明确失败；不能丢弃已经交付的内容")
        else:
            _failed_call_started(run, plan, request, output)
        calls[stage] = output
    expected = {"extraction"} if previous or grant_inherited else {"comprehension", "extraction"}
    if (set(calls) != expected or type(terminal.get("model_calls")) is not int
            or terminal["model_calls"] != len(calls) or type(failure.get("model_calls")) is not int
            or failure["model_calls"] != len(calls)):
        raise ValueError("原运行真实调用次数不完整，不能计算恢复额度")
    limit, count = plan["reading_settings"].get("total_timeout_seconds"), plan.get("max_calls")
    if (type(limit) is not int or type(count) is not int or limit < 60 or count < 2
            or (not allow_exhausted and (elapsed > limit or len(calls) > count))):
        raise ValueError("原运行缺少有效且未超出的调用/耗时预算")
    prior = previous["budget"] if previous else None
    used_seconds = (prior["used_seconds"] if prior else 0) + elapsed
    used_calls = (prior["used_calls"] if prior else 0) + len(calls)
    original_seconds, original_calls = (prior["original_seconds"], prior["original_calls"]) if prior else (limit, count)
    # Honor any stricter child allocation as well as the original chain ceiling.
    remaining_seconds = min(math.floor(original_seconds - used_seconds), math.floor(limit - elapsed))
    remaining_calls = min(original_calls - used_calls, count - len(calls))
    if not allow_exhausted and (remaining_calls < 2 or remaining_seconds < 60):
        raise ValueError("原剩余预算不足抽取和首次审查两次调用；不能增加额度或跳过审查")
    return {"route": "dossier_extraction", "policy": POLICY, "parent_run": run.name,
        "bundle_path": str(imported), "cohort_role": plan["cohort_role"], "dossier_path": str(origin),
        "parent_artifacts": _artifacts(settings, run), "candidate_reused": False, "scientific_audit_inherited": False,
        "budget": {"policy": "lineage_remaining", "original_seconds": original_seconds, "original_calls": original_calls,
            "used_seconds": used_seconds, "used_calls": used_calls, "remaining_seconds": remaining_seconds,
            "remaining_calls": remaining_calls, "max_revisions": plan["reading_settings"].get("max_revisions", 0),
            "description": "保留已认证的全文理解，继续未完成的抽取和首次原文审查；扣除父链全部调用和实际耗时。"}}


def select(settings, run, *, current_run=None):
    selected = _authenticate(settings, run, current_run, set())
    _latest(DataStore(settings.data_root), settings, Path(run), _json(Path(run) / "plan.json")["paper_id"], current_run)
    return selected


def select_for_addition(settings, run):
    """Read-only source authentication; no new budget or execution authority."""
    return _authenticate(settings, run, None, set(), allow_exhausted=True)


def prepare_selected(settings, run, selection):
    import json
    from .workflow import prepare
    if select(settings, run) != selection:
        raise ValueError("恢复点、原始理解证明或预算已改变；未调用模型")
    bounded = settings.model_copy(deep=True)
    budget = selection["budget"]
    bounded.codex.max_calls_per_run = min(bounded.codex.max_calls_per_run, budget["remaining_calls"])
    bounded.reading.total_timeout_seconds = min(bounded.reading.total_timeout_seconds, budget["remaining_seconds"])
    bounded.codex.timeout_seconds = min(bounded.codex.timeout_seconds, bounded.reading.total_timeout_seconds)
    bounded.reading.max_revisions = min(bounded.reading.max_revisions, budget["max_revisions"])
    new = prepare(bounded, Path(selection["bundle_path"]), cohort_role=selection["cohort_role"],
        reuse_dossier=Path(selection["dossier_path"]))
    receipt = new / "dossier_extraction_recovery.json"
    write_new_json(receipt, selection)
    plan_path = new / "plan.json"
    plan = _json(plan_path)
    descriptor = {"path": str(receipt), "sha256": hash_file(receipt), "parent_run": Path(run).name}
    plan["dossier_extraction_recovery"] = descriptor
    plan["input_paths"].append({k: descriptor[k] for k in ("path", "sha256")})
    write_new_json(new / "plan.before-dossier-recovery.json", _json(plan_path))
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    store = DataStore(settings.data_root)
    store.event(new.name, "prepared", {"plan_sha256": hash_file(plan_path), "parent_run": Path(run).name})
    _reservation(store, Path(run).name, new.name, reserve=True)
    return bounded, new


def verify_dossier_recovery(settings, run, plan=None):
    """Replay the authority for every entry point, including direct CLI runs."""
    run = Path(run)
    plan = _json(run / "plan.json") if plan is None else plan
    descriptor = plan.get("dossier_extraction_recovery", {})
    receipt = _safe(settings, descriptor["path"])
    if receipt != run / "dossier_extraction_recovery.json" or hash_file(receipt) != descriptor.get("sha256"):
        raise ValueError("全文理解恢复证明已改变")
    selected = _json(receipt)
    if DataStore(settings.data_root).last_event(run.name, EVENT) != {"parent_run": selected["parent_run"]}:
        raise ValueError("当前理解恢复没有实际取得原预算预留，不能执行")
    parent = settings.data_root / "runs" / selected["parent_run"]
    if select(settings, parent, current_run=run.name) != selected:
        raise ValueError("原理解、失败记录、来源或预算已变化")
    budget, reused = selected["budget"], plan.get("reused_dossier", {})
    if (plan.get("reused_candidate") or descriptor.get("parent_run") != selected["parent_run"]
            or reused.get("path") != selected["dossier_path"]
            or reused.get("sha256") != hash_file(Path(selected["dossier_path"]))
            or hash_file(run / "reading_dossier.json") != hash_file(Path(selected["dossier_path"]))
            or plan["max_calls"] > budget["remaining_calls"]
            or plan["reading_settings"]["total_timeout_seconds"] > budget["remaining_seconds"]
            or plan["reading_settings"].get("max_revisions", 0) > budget["max_revisions"]):
        raise ValueError("理解恢复不能改变已有解释、跳过抽取或扩大原预算")
    return selected


def execute_selected(settings, plan_path, *, client, verify=True):
    from .workflow import execute_plan
    path = Path(plan_path)
    verify_dossier_recovery(settings, path.parent)
    return execute_plan(settings, path, client=client, verify=verify)
