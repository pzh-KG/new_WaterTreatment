"""Authenticated web routes for unstarted reading and system-ID correction.

Selection is read-only. Preparation creates a new immutable run from the same
registered import. No route truncates sources, calls a model, or publishes data.
"""
from __future__ import annotations

import json
from pathlib import Path

from .model_policy import plan_model, request_matches_plan, require_active_model
from .storage import DataStore, digest_json, hash_file, utc_now, write_new_json

POLICY = "preprovider-initial-reading-recovery-v1"


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _safe(settings, path):
    path = Path(path)
    resolved = path.resolve(strict=True)
    if path != resolved or not resolved.is_relative_to(settings.data_root):
        raise ValueError("恢复证据路径变化或不在配置的数据目录中")
    return resolved


def _parent(settings, run):
    from .workflow import _read_bundle
    run = _safe(settings, run)
    store = DataStore(settings.data_root)
    rows = [r for r in store.runs() if r["run_id"] == run.name]
    if (run.parent != settings.data_root / "runs" or len(rows) != 1
            or rows[0]["kind"] != "extraction" or Path(rows[0]["manifest_path"]) != run / "manifest.json"):
        raise ValueError("原运行不是此数据目录内已登记的抽取运行")
    plan, manifest = _json(run / "plan.json"), _json(run / "manifest.json")
    prepared = store.last_event(run.name, "prepared")
    if (not prepared or prepared.get("plan_sha256") != hash_file(run / "plan.json")
            or plan.get("run_id") != run.name or manifest.get("run_id") != run.name
            or manifest.get("paper_id") != plan.get("paper_id")
            or plan.get("cohort_role") not in {"development", "unassigned"}
            or plan.get("rule_version") != settings.rules_version
            or plan.get("workflow") != "active_reading"):
        raise ValueError("原计划身份、来源登记、研究队列或科学规则无法认证")
    require_active_model(settings.codex)
    if not settings.reading.enabled or plan_model(plan) is None:
        raise ValueError("恢复需要已登记的 Sol MAX 原始模型配置及完整阅读流程")
    frozen = {str(_safe(settings, x["path"])): x["sha256"]
              for x in [*plan["input_paths"], *plan.get("reading_artifacts", [])]}
    if frozen.get(str(run / "sources.json")) != hash_file(run / "sources.json"):
        raise ValueError("原来源快照未被计划冻结")
    if any(hash_file(Path(path)) != sha for path, sha in frozen.items()):
        raise ValueError("原计划冻结输入发生变化")
    imported = _safe(settings, manifest["source_bundle"])
    import_row = [r for r in store.runs() if r["run_id"] == imported.parent.name and r["kind"] == "import"]
    import_event = store.last_event(imported.parent.name, "import_completed")
    if (imported.name != "sources.json" or imported.parent.parent != settings.data_root / "runs"
            or len(import_row) != 1 or not import_event
            or import_event.get("bundle_sha256") != hash_file(imported)
            or manifest.get("source_bundle_sha256") != hash_file(imported)
            or _json(imported) != _json(run / "sources.json")):
        raise ValueError("原始导入快照与当前运行来源不一致")
    bundle, _ = _read_bundle(imported)
    if bundle.get("paper_id") != plan["paper_id"]:
        raise ValueError("来源文献身份与原计划不一致")
    return store, run, plan, imported, bundle, frozen


def _reservation(store, parent_id, current=None, *, reserve=False):
    with store.connection() as db:
        if reserve:
            db.execute("BEGIN IMMEDIATE")
        for row in db.execute("SELECT run_id,payload FROM run_events WHERE event='initial_recovery_prepared'"):
            if json.loads(row[1]).get("parent_run") == parent_id and row[0] != current:
                raise ValueError("原始阅读预算已由后续恢复计划保留，请使用该计划，不能重复启动")
        if reserve:
            db.execute("INSERT INTO run_events (run_id,event,payload,created_at) VALUES (?,?,?,?)",
                (current, "initial_recovery_prepared", json.dumps({"parent_run": parent_id}), utc_now()))


def select_initial(settings, run, *, current_run=None):
    """Only a measured prompt-size failure before any provider execution qualifies."""
    from .scope_policy import first_audit_prompt_transition
    store, run, plan, imported, bundle, frozen = _parent(settings, run)
    _reservation(store, run.name, current_run)
    terminal, failure = _json(run / "result.json"), _json(run / "failure.json")
    if (terminal.get("status") != "reading_incomplete_failure" or terminal.get("model_calls") != 0
            or failure.get("model_calls") != 0 or terminal.get("human_validated") is not False
            or terminal.get("graph_published") is not False or terminal.get("last_candidate_version") is not None
            or plan.get("reused_candidate") or plan.get("reused_dossier")
            or list(run.glob("candidate*.json")) or list(run.glob("audit*.json"))
            or list(run.glob("reading_dossier*.json")) or (run / "model_review.json").exists()
            or any(p.is_file() for p in (run / "reading_tools").rglob("*"))
            or any(p.is_file() and p.stat().st_size for p in (run / "app_server").rglob("*"))):
        raise ValueError("仅首次模型调用前的输入超限可以恢复初次阅读；现有阅读或候选不能绕过")
    requests = list((run / "calls").glob("*/request.json"))
    if len(requests) != 1:
        raise ValueError("初次阅读恢复要求唯一的未调用模型预检记录")
    envelope = _json(requests[0]); request = envelope["request"]
    result = _json(requests[0].with_name("result.json"))
    prompt = request.get("prompt")
    size = result.get("prompt_size", {})
    cap = size.get("max_utf8_bytes")
    if (not isinstance(prompt, str) or type(cap) is not int or cap < 1
            or size.get("utf8_bytes") != len(prompt.encode("utf-8"))
            or size.get("characters") != len(prompt) or len(prompt.encode("utf-8")) <= cap
            or request.get("prompt_size") != size):
        raise ValueError("输入超限记录与实际提示文本不一致")
    error = (f"ModelCallError: prompt exceeds conservative UTF-8 budget {cap} bytes: "
             f"{size['utf8_bytes']} bytes / {size['characters']} characters; input was not truncated")
    sources = {str(_safe(settings, x["path"])): x["sha256"] for x in request.get("sources", [])}
    source_inputs = {str(_safe(settings, x["path"])): x["sha256"] for x in plan["input_paths"]}
    if (envelope.get("fingerprint") != digest_json(request) or request.get("stage") != "comprehension"
            or not request_matches_plan(plan, request)
            or request.get("code_identity") != plan.get("code_identity")
            or request.get("rule_version") != plan["rule_version"]
            or any(sources.get(p) != h for p, h in source_inputs.items())
            or any(hash_file(Path(p)) != h for p, h in sources.items())
            or result.get("status") != "failed" or result.get("elapsed_seconds") != 0
            or result.get("effective_model") is not None or result.get("usage") != []
            or any(result.get(k) for k in ("thread_id", "turn_id", "server_selected_model", "server_selected_effort"))
            or result.get("error") != error or failure.get("error") != error
            or any(requests[0].with_name(n).exists() for n in ("answer.json", "record.json"))):
        raise ValueError("预检失败缺少未执行模型及同源输入证明，不能归零已消耗预算")
    if hash_file(run / "taxonomy.json") != hash_file(settings.taxonomy_path):
        raise ValueError("原分类体系发生变化，不能在恢复中悄悄升级")
    transitions = []
    for stem, filename in (("understand", "understand_prompt.txt"), ("extract", "extract_prompt.txt")):
        original = (run / filename).read_text(encoding="utf-8").split("\n\nDELIVERY:", 1)[0]
        change = first_audit_prompt_transition(settings, stem, original, plan_model(plan))
        if change:
            transitions.append(change)
    seconds, calls = plan["reading_settings"].get("total_timeout_seconds"), plan.get("max_calls")
    if type(seconds) is not int or seconds < 60 or type(calls) is not int or calls < 3:
        raise ValueError("原预算不足初次阅读、抽取和审查三个阶段")
    return {"route": "initial_reading", "parent_run": run.name, "bundle_path": str(imported),
        "cohort_role": plan["cohort_role"], "policy": POLICY,
        **({"first_audit_prompt_transitions": transitions} if transitions else {}),
        "parent_artifacts": [{"path": str(_safe(settings, p)), "sha256": hash_file(p)}
                             for p in sorted(run.rglob("*")) if p.is_file()],
        "budget": {"policy": "parent_remaining", "original_seconds": seconds, "used_seconds": 0,
            "remaining_seconds": seconds, "original_calls": calls, "used_calls": 0,
            "remaining_calls": calls, "preprovider_attempts": 1,
            "max_revisions": plan["reading_settings"].get("max_revisions", 0),
            "description": "原任务在首次模型调用前因输入超限停止；复用原导入，从未执行的阅读阶段开始，保留原预算。"},
        "scientific_audit_inherited": False, "candidate_reused": False}


def select_identity(settings, run):
    """Reuse exact completed model outputs; change controller-owned IDs only."""
    from .identity_resume import authenticate_identity_parent, check_identity_reservation
    store, run, plan, imported, bundle, _ = _parent(settings, run)
    check_identity_reservation(store, run.name)
    _, candidate, proof = authenticate_identity_parent(settings, run, bundle, plan["cohort_role"])
    budget = proof["budget"]
    return {"route": "identity_candidate", "parent_run": run.name, "bundle_path": str(imported),
        "candidate_path": str(run / "candidate.v00.json"), "cohort_role": plan["cohort_role"],
        "identity_proof": proof, "observation_count": len(candidate.observations),
        "budget": {"policy": "parent_remaining", "original_seconds": budget["original_total_seconds"],
            "used_seconds": budget["used_model_seconds"], "remaining_seconds": budget["remaining_seconds"],
            "original_calls": budget["original_max_calls"], "used_calls": budget["used_calls"],
            "remaining_calls": budget["remaining_calls"],
            "max_revisions": plan["reading_settings"].get("max_revisions", 0),
            "description": "只更正已认证结果的系统文献编号，保留科学内容和阅读档案，从首次原文审查继续并扣除旧耗时。"},
        "scientific_audit_inherited": False, "candidate_reused": True}


def select_unfinished(settings, run, *, current_run=None):
    """Resume the interrupted first stage using its spent allowance and public source work."""
    from .budget_grants import authenticate_unfinished_reading, retained_stage_budget
    from .dossier_extraction_resume import _latest
    from .interrupted_reading import public_progress
    proof = authenticate_unfinished_reading(settings, run)
    store = DataStore(settings.data_root)
    plan = _json(run / "plan.json")
    # An older completed version remains scientifically replayable after a later
    # legitimate run. Enforce recency while choosing or starting unspent work.
    if not current_run or not (settings.data_root / "runs" / current_run / "execution_started.json").exists():
        _latest(store, settings, run, plan["paper_id"], current_run)
    _reservation(store, run.name, current_run)
    balance = retained_stage_budget(settings, run)
    if balance["remaining_calls"] < 3 or balance["remaining_seconds"] < 60:
        raise ValueError("本篇剩余额度不足以完成首次理解、抽取和首次审查")
    result_path, = (run / "calls").glob("*/result.json")
    progress = public_progress(run, _json(result_path), include_crops=True)
    return {**proof, "policy":"interrupted-first-reading-v1", "public_progress":progress,
            "budget":{**balance,"max_revisions":plan["reading_settings"]["max_revisions"]},
            "scientific_audit_inherited":False}


def prepare_selected(settings, run, selection):
    """Reauthenticate the frozen selection, then prepare without provider calls."""
    from .workflow import prepare
    route = selection.get("route")
    selector = {"initial_reading": select_initial, "unfinished_reading": select_unfinished,
                "identity_candidate": select_identity}.get(route)
    if selector is None or selector(settings, run) != selection:
        raise ValueError("恢复点、原始证据或预算已改变；未调用模型")
    bounded = settings.model_copy(deep=True)
    budget = selection["budget"]
    bounded.codex.max_calls_per_run = min(bounded.codex.max_calls_per_run, budget["remaining_calls"])
    bounded.reading.total_timeout_seconds = min(bounded.reading.total_timeout_seconds, budget["remaining_seconds"])
    bounded.codex.timeout_seconds = min(bounded.codex.timeout_seconds, bounded.reading.total_timeout_seconds)
    bounded.reading.max_revisions = min(bounded.reading.max_revisions, budget["max_revisions"])
    options = {"reuse_identity_corrected_run": Path(run)} if route == "identity_candidate" else {}
    new_run = prepare(bounded, Path(selection["bundle_path"]), cohort_role=selection["cohort_role"], **options)
    if route in {"initial_reading","unfinished_reading"}:
        receipt = new_run / "initial_recovery.json"
        write_new_json(receipt, selection)
        plan_path = new_run / "plan.json"
        plan = _json(plan_path)
        descriptor = {"path": str(receipt), "sha256": hash_file(receipt)}
        plan["initial_recovery"] = descriptor
        plan["input_paths"].append(descriptor)
        if route == "unfinished_reading":
            for item in selection["public_progress"]["source_views"][:12]:
                plan["input_paths"].append({"path":item["image_path"],"sha256":item["image_sha256"]})
        # The newly prepared run has not executed. Preserve its initial plan as
        # well as the final frozen recovery contract and register the latter.
        write_new_json(new_run / "plan.before-initial-recovery.json", _json(plan_path))
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        store = DataStore(settings.data_root)
        store.event(new_run.name, "prepared", {"plan_sha256": hash_file(plan_path), "parent_run": Path(run).name})
        _reservation(store, Path(run).name, new_run.name, reserve=True)
    return bounded, new_run


def verify_initial_recovery(settings, run, plan=None):
    """Read-only proof replay shared by web and direct core/CLI execution."""
    run = _safe(settings, run)
    plan = _json(run / "plan.json") if plan is None else plan
    descriptor = plan.get("initial_recovery", {})
    path = _safe(settings, descriptor["path"])
    if path != run / "initial_recovery.json" or hash_file(path) != descriptor.get("sha256"):
        raise ValueError("初次阅读恢复证明已变化")
    selection = _json(path)
    parent = settings.data_root / "runs" / selection["parent_run"]
    select = select_unfinished if selection.get("route") == "unfinished_reading" else select_initial
    if select(settings, parent, current_run=run.name) != selection:
        raise ValueError("初次阅读原始失败、来源或预算已变化")
    # Absence of a competing reservation is not ownership. A plan retained
    # after losing a preparation race must never start through another entry.
    reservation = DataStore(settings.data_root).last_event(run.name, "initial_recovery_prepared")
    if reservation != {"parent_run": parent.name}:
        raise ValueError("当前初次阅读恢复计划尚未持有原运行预算预留")
    budget = selection["budget"]
    if (plan.get("reused_candidate") or plan.get("reused_dossier")
            or plan["max_calls"] > budget["remaining_calls"]
            or plan["reading_settings"]["total_timeout_seconds"] > budget["remaining_seconds"]):
        raise ValueError("初次阅读恢复不能伪造已读内容或扩大原预算")


def execute_selected(settings, plan_path, *, client, verify=True):
    """Replay recovery authorities before allowing ordinary workflow execution."""
    from .workflow import execute_plan
    plan_path = Path(plan_path)
    plan, run = _json(plan_path), plan_path.parent
    if plan.get("initial_recovery"):
        verify_initial_recovery(settings, run, plan)
    elif not plan.get("identity_correction"):
        raise ValueError("没有受支持的初次阅读或身份恢复证明")
    return execute_plan(settings, plan_path, client=client, verify=verify)
