"""Append-only, source-bound grants for one paper's retained processing chain.

Granting execution resources never authenticates scientific contents. Callers
must independently authenticate the selected checkpoint, then bind the grant to
one new run. Original plans, calls, failures and scientific outputs stay intact.
"""
from __future__ import annotations

import json
import math
import uuid
from pathlib import Path

from .model_policy import plan_model, request_matches_plan
from .budget_policy import BudgetPolicy, default_policy, verify_initial_policy
from .storage import DataStore, digest_json, hash_file, safe_name, utc_now, write_new_json

RESERVED = "budget_grant_reserved_v1"
BOUND = "budget_grant_bound_v1"
ADOPTED = "budget_policy_adopted_v1"


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _safe(settings, path):
    path = Path(path)
    resolved = path.resolve(strict=True)
    if path != resolved or settings.data_root.resolve() not in resolved.parents:
        raise ValueError("预算依据不在配置数据目录内，或已变为链接")
    return resolved


def _run(settings, value):
    path = Path(value)
    if not path.is_absolute():
        path = settings.data_root / "runs" / safe_name(str(value))
    path = _safe(settings, path)
    if path.parent != (settings.data_root / "runs").resolve():
        raise ValueError("预算父运行必须属于已配置的数据目录")
    store = DataStore(settings.data_root)
    row = next((row for row in store.runs() if row["run_id"] == path.name), None)
    if row is None or row["kind"] != "extraction" or Path(row["manifest_path"]) != path / "manifest.json":
        raise ValueError("预算父运行不是已登记的文献抽取")
    plan = _json(path / "plan.json")
    if (plan.get("run_id") != path.name
            or (store.last_event(path.name, "prepared") or {}).get("plan_sha256") != hash_file(path / "plan.json")
            or plan_model(plan) is None):
        raise ValueError("预算运行计划、登记或 Sol MAX 配置无法认证")
    return path, plan


def parent_ref(settings, run, plan):
    """Prefer the immediate retained continuation, never the original dossier."""
    if plan.get("budget_continuation"):
        return plan["budget_continuation"]["parent_run"]
    manifest = _json(run / "manifest.json")
    if manifest.get("parent_run"):
        return manifest["parent_run"]
    for key in ("partial_remaining_parent", "dossier_extraction_recovery", "initial_recovery",
                "identity_correction", "reused_candidate", "coverage_correction_parent",
                "partial_audit_parent", "audit_addendum_parent", "addendum_parent", "resumed_full_audit"):
        value = plan.get(key)
        if not isinstance(value, dict):
            continue
        if key == "partial_remaining_parent" and value.get("failed_run"):
            return value["failed_run"]
        if value.get("parent_run"):
            return value["parent_run"]
        if value.get("path") and value.get("sha256"):
            proof = _safe(settings, value["path"])
            if hash_file(proof) != value["sha256"]:
                raise ValueError("预算祖链证明哈希已改变")
            return _json(proof).get("parent_run")
    if plan.get("reused_dossier"):
        return plan["reused_dossier"].get("parent_run")
    return None


def _budget_source_hash(settings, run, plan, seen=()):
    value = hash_file(_safe(settings, run / "sources.json"))
    if plan.get("workflow") in {"retained-issue-source-review-v1", "source-reviewer-verbatim-text-v1"}:
        prior, ancestor = _run(settings, plan["parent_run"])
        if (run.name in seen or len(seen) >= 64 or prior.name >= run.name
                or ancestor["paper_id"] != plan["paper_id"] or hash_file(_safe(settings, prior / "sources.json")) != value):
            raise ValueError("Issue-only review has a different paper, source or invalid budget lineage")
        return _budget_source_hash(settings, prior, ancestor, (*seen, run.name))
    if plan.get("coverage_visual_inputs") and value != plan.get("coverage_correction_parent", {}).get("source_bundle_sha256"):
        # Verify exact occurrence images from the same originals. Only then can
        # an augmented rendering manifest spend the original paper's allowance.
        from .coverage_correction_workflow import _authenticate_plan
        _, _, _, coverage = _authenticate_plan(settings, run / "plan.json")
        return coverage.receipt["source_bundle_sha256"]
    return value


def lineage_snapshot(settings, parent_run):
    """Replay every ancestor's actual adapter usage; absence is not zero."""
    store = DataStore(settings.data_root)
    items, seen = [], set()
    current = parent_run
    paper_id, bundle_sha = None, None
    while current:
        run, plan = _run(settings, current)
        if run.name in seen or len(seen) >= 64:
            raise ValueError("预算祖链存在循环或过长，不能推测剩余额度")
        seen.add(run.name)
        source_hash = _budget_source_hash(settings, run, plan)
        paper_id = paper_id or plan["paper_id"]
        bundle_sha = bundle_sha or source_hash
        if plan["paper_id"] != paper_id or source_hash != bundle_sha:
            raise ValueError("预算祖链跨越了不同文献或正文/SI来源")
        calls, used, used_calls = [], 0.0, 0
        for request_path in sorted((run / "calls").glob("*/request.json")):
            envelope = _json(_safe(settings, request_path))
            request = envelope.get("request", {})
            result_path = _safe(settings, request_path.with_name("result.json"))
            output = _json(result_path)
            seconds = output.get("elapsed_seconds")
            if (envelope.get("fingerprint") != digest_json(request)
                    or not request_matches_plan(plan, request)
                    or isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                    or not math.isfinite(seconds) or seconds < 0):
                raise ValueError("原调用身份或真实耗时未知，不能追加或重置预算")
            used += seconds
            size = request.get("prompt_size") or {}
            preprovider = (output.get("status") == "failed" and seconds == 0
                and type(size.get("utf8_bytes")) is int and type(size.get("max_utf8_bytes")) is int
                and size["utf8_bytes"] > size["max_utf8_bytes"] and output.get("prompt_size") == size
                and not any(output.get(key) for key in ("server_selected_model", "effective_model", "usage", "thread_id", "turn_id")))
            count_for_call = 0 if preprovider else 1
            used_calls += count_for_call
            calls.append({"path": str(request_path), "request_sha256": hash_file(request_path),
                "result_sha256": hash_file(result_path), "elapsed_seconds": seconds,
                "stage": request.get("stage"), "model_calls": count_for_call})
        # A recorded provider count must agree with the retained call ledger.
        for name in ("result.json", "failure.json"):
            if not (run / name).exists():
                continue
            terminal = _json(_safe(settings, run / name))
            count = terminal.get("model_calls")
            if count is not None and (type(count) is not int or count != used_calls):
                raise ValueError("原调用次数与保存记录不一致")
        if (store.last_event(run.name, "execution_started") is not None or (run / "execution_started.json").exists()) and not calls:
            terminal = _json(run / "result.json") if (run / "result.json").is_file() else {}
            if terminal.get("model_calls") != 0:
                raise ValueError("已开始运行但缺少可核验用量，不能当作零消耗")
        items.append({"run_id": run.name, "plan_sha256": hash_file(run / "plan.json"),
            "sources_sha256": source_hash, "used_seconds": used, "used_calls": used_calls,
            "calls": calls, "terminal_hashes": {name: hash_file(run / name)
                for name in ("result.json", "failure.json") if (run / name).is_file()}})
        current = parent_ref(settings, run, plan)
    items.reverse()
    return {"root_run": items[0]["run_id"], "parent_run": items[-1]["run_id"],
        "paper_id": paper_id, "source_bundle_sha256": bundle_sha, "runs": items,
        "used_seconds": sum(item["used_seconds"] for item in items),
        "used_calls": sum(item["used_calls"] for item in items)}


def _events(store, event, db=None):
    if db is None:
        with store.connection() as connection:
            return _events(store, event, connection)
    return [(row[0], json.loads(row[1])) for row in db.execute(
        "SELECT run_id,payload FROM run_events WHERE event=? ORDER BY id", (event,))]


def _policy(settings, snapshot, supplied=None):
    root, plan = _run(settings, snapshot["root_run"])
    frozen = verify_initial_policy(settings, root, plan)
    adopted = DataStore(settings.data_root).last_event(root.name, ADOPTED)
    if frozen:
        actual = frozen["policy"]
    elif adopted:
        if adopted["source_bundle_sha256"] != snapshot["source_bundle_sha256"]:
            raise ValueError("已登记追加预算政策的来源已变化")
        actual = adopted["policy"]
    elif supplied is not None:
        actual = BudgetPolicy.model_validate(supplied).model_dump(mode="json")
        if (actual["initial_seconds"] != plan["reading_settings"]["total_timeout_seconds"]
                or actual["initial_calls"] != plan["max_calls"]):
            raise ValueError("旧运行的初始预算必须保留原冻结值")
    else:
        actual = default_policy(settings)
        actual.update(initial_seconds=plan["reading_settings"]["total_timeout_seconds"], initial_calls=plan["max_calls"])
        actual["max_total_seconds"] = max(actual["initial_seconds"], actual["max_total_seconds"])
        actual["max_total_calls"] = max(actual["initial_calls"], actual["max_total_calls"])
    actual = BudgetPolicy.model_validate(actual).model_dump(mode="json")
    if supplied is not None and (frozen or adopted) and actual != BudgetPolicy.model_validate(supplied).model_dump(mode="json"):
        raise ValueError("本篇已经冻结预算政策，不能在续处理中提高累计上限")
    return actual, bool(frozen or adopted)


def _grant_records(settings, root, *, db=None):
    store = DataStore(settings.data_root)
    output = []
    for owner, event in _events(store, RESERVED, db):
        if event.get("root_run") != root:
            continue
        path = _safe(settings, event["path"])
        if hash_file(path) != event["sha256"]:
            raise ValueError("追加预算授权记录已变化")
        grant = _json(path)
        if grant["parent_run"] != owner or grant["grant_id"] != event["grant_id"]:
            raise ValueError("追加预算登记与授权不一致")
        if grant["root_run"] == root:
            output.append((event, grant))
    return output


def _no_unselected_descendants(settings, snapshot, *, current_run=None):
    chain = {item["run_id"] for item in snapshot["runs"]}
    store = DataStore(settings.data_root)
    rows = store.runs()
    parent_time = next(row["created_at"] for row in rows if row["run_id"] == snapshot["parent_run"])
    for row in rows:
        if row["kind"] != "extraction" or row["run_id"] in chain or row["run_id"] == current_run:
            continue
        path = settings.data_root / "runs" / row["run_id"]
        if not (path / "plan.json").is_file():
            continue
        plan = _json(path / "plan.json")
        if plan.get("paper_id") != snapshot["paper_id"]:
            continue
        related = parent_ref(settings, path, plan) in chain
        executed = (store.last_event(path.name, "execution_started") is not None
            or bool(list((path / "calls").glob("*/request.json")))
            or (path / "result.json").exists() or (path / "failure.json").exists())
        if related or (row["created_at"] > parent_time and executed):
            raise ValueError("已有未纳入当前恢复链的后续运行；必须从最新成果继续，不能漏计消费")


def preview_grant(settings, parent_run, policy=None, mode="manual"):
    if mode not in {"manual", "automatic"}:
        raise ValueError("追加预算需要明确手动或批次自动授权")
    snapshot = lineage_snapshot(settings, parent_run)
    _no_unselected_descendants(settings, snapshot)
    policy, frozen = _policy(settings, snapshot, policy)
    if mode == "automatic" and not frozen:
        raise ValueError("旧任务没有批次预授权政策，不能自动追加额度")
    grants = _grant_records(settings, snapshot["root_run"])
    if any(grant["parent_run"] == snapshot["parent_run"] for _, grant in grants):
        raise ValueError("本恢复点已有追加额度预留，请使用对应后续任务")
    chain = {item["run_id"] for item in snapshot["runs"]}
    bound = {event["grant_id"]: owner for owner, event in _events(DataStore(settings.data_root), BOUND)}
    if any(bound.get(grant["grant_id"]) not in chain for _, grant in grants):
        raise ValueError("同篇仍有未归入当前恢复链的预算预留，不能并行追加")
    auto_count = sum(grant["mode"] == "automatic" for _, grant in grants)
    if mode == "automatic" and auto_count >= policy["max_auto_additions"]:
        raise ValueError("已达到本批次允许的单篇自动补充次数")
    authorized_seconds = policy["initial_seconds"] + sum(grant["added_seconds"] for _, grant in grants)
    authorized_calls = policy["initial_calls"] + sum(grant["added_calls"] for _, grant in grants)
    added_seconds = min(policy["addition_seconds"], policy["max_total_seconds"] - authorized_seconds)
    added_calls = min(policy["addition_calls"], policy["max_total_calls"] - authorized_calls)
    available_seconds = math.floor(authorized_seconds + added_seconds - snapshot["used_seconds"])
    available_calls = authorized_calls + added_calls - snapshot["used_calls"]
    if added_seconds < 0 or added_calls < 0 or not (added_seconds or added_calls) or available_seconds < 60 or available_calls < 1:
        raise ValueError("单篇累计上限已到，或剩余额度不足一次受限续处理")
    preview = {"root_run": snapshot["root_run"], "parent_run": snapshot["parent_run"],
        "paper_id": snapshot["paper_id"], "policy": policy, "mode": mode,
        "used_seconds": snapshot["used_seconds"], "used_calls": snapshot["used_calls"],
        "authorized_seconds": authorized_seconds, "authorized_calls": authorized_calls,
        "added_seconds": added_seconds, "added_calls": added_calls,
        "allocated_seconds": min(21600, available_seconds), "allocated_calls": min(20, available_calls),
        "auto_additions_used": auto_count, "max_auto_additions": policy["max_auto_additions"],
        "usage_snapshot": snapshot, "usage_sha256": digest_json(snapshot),
        "source_bundle_sha256": snapshot["source_bundle_sha256"]}
    preview["fingerprint"] = digest_json(preview)
    return preview


def reserve_grant(settings, parent_run, preview, selection_fingerprint, mode="manual"):
    if not isinstance(selection_fingerprint, str) or len(selection_fingerprint) != 64:
        raise ValueError("追加额度必须绑定已核验的恢复阶段")
    store = DataStore(settings.data_root)
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        current = preview_grant(settings, parent_run, preview["policy"], mode)
        if current != preview:
            raise ValueError("已有成果、用量或追加预算政策已改变，请重新查看方案")
        identifier = "grant-" + uuid.uuid4().hex
        path = settings.data_root / "budget_grants" / (identifier + ".json")
        payload = {**preview, "grant_id": identifier, "selection_fingerprint": selection_fingerprint, "created_at": utc_now()}
        write_new_json(path, payload)
        descriptor = {"grant_id": identifier, "parent_run": preview["parent_run"], "root_run": preview["root_run"],
            "path": str(path), "sha256": hash_file(path)}
        if store.last_event(preview["root_run"], ADOPTED) is None:
            root, root_plan = _run(settings, preview["root_run"])
            if not root_plan.get("budget_policy"):
                adopted = {"policy": preview["policy"], "source_bundle_sha256": preview["source_bundle_sha256"], "manual_authorization": True}
                db.execute("INSERT INTO run_events(run_id,event,payload,created_at) VALUES(?,?,?,?)",
                    (root.name, ADOPTED, json.dumps(adopted), utc_now()))
        db.execute("INSERT INTO run_events(run_id,event,payload,created_at) VALUES(?,?,?,?)",
            (preview["parent_run"], RESERVED, json.dumps(descriptor), utc_now()))
    return descriptor


def _load_grant(settings, descriptor):
    path = _safe(settings, descriptor["path"])
    if path.parent != settings.data_root / "budget_grants" or hash_file(path) != descriptor["sha256"]:
        raise ValueError("追加预算证明的位置或内容已改变")
    grant = _json(path)
    if any(descriptor.get(key) != grant.get(key) for key in ("grant_id", "parent_run", "root_run")):
        raise ValueError("追加预算证明身份不一致")
    registrations = [item for owner, item in _events(DataStore(settings.data_root), RESERVED)
        if owner == grant["parent_run"] and item == descriptor]
    if len(registrations) != 1:
        raise ValueError("追加预算没有唯一有效的原子预留")
    if lineage_snapshot(settings, grant["parent_run"]) != grant["usage_snapshot"]:
        raise ValueError("追加预算后的原始用量、来源或父运行记录已改变")
    policy, _ = _policy(settings, grant["usage_snapshot"], grant["policy"])
    if (grant["usage_sha256"] != digest_json(grant["usage_snapshot"])
            or grant["allocated_seconds"] + grant["used_seconds"] > policy["max_total_seconds"]
            or grant["allocated_calls"] + grant["used_calls"] > policy["max_total_calls"]):
        raise ValueError("追加预算快照或累计上限无法认证")
    return grant


def load_grant(settings, descriptor):
    """Return an already-reserved immutable authorization, with proof replay."""
    return _load_grant(settings, descriptor)


def preparation_grant(settings, parent_run, descriptor, *, minimum_calls=1):
    if descriptor is None:
        return None
    grant = load_grant(settings, descriptor)
    if (grant["parent_run"] != Path(parent_run).name
            or grant["allocated_calls"] < minimum_calls or grant["allocated_seconds"] < 60):
        raise ValueError("追加授权不属于当前恢复点，或不足以完成所需审查阶段")
    return grant


def prepared_grant(settings, run, plan, parent_run):
    descriptor = plan.get("budget_grant_preparation")
    if descriptor is None:
        return None
    if plan.get("budget_continuation") != descriptor:
        raise ValueError("追加额度准备稿尚未完成真实授权绑定，不能执行")
    grant = verify_grant_plan(settings, run, plan)
    if grant["parent_run"] != Path(parent_run).name:
        raise ValueError("追加预算绑定了不同的科学父运行")
    return grant


def addition_usage(settings, run, plan, new_seconds, new_calls):
    """Keep the original ledger honest and report the new authorization apart."""
    if not plan.get("budget_continuation"):
        return None
    grant = verify_grant_plan(settings, run, plan)
    return {"grant_id": grant["grant_id"], "root_run": grant["root_run"],
        "added_seconds": grant["added_seconds"], "added_calls": grant["added_calls"],
        "allocated_seconds": grant["allocated_seconds"], "allocated_calls": grant["allocated_calls"],
        "previous_used_seconds": grant["used_seconds"], "previous_used_calls": grant["used_calls"],
        "current_used_seconds": new_seconds, "current_used_calls": new_calls,
        "cumulative_used_seconds": grant["used_seconds"] + new_seconds,
        "cumulative_used_calls": grant["used_calls"] + new_calls,
        "remaining_authorized_seconds": max(0, grant["allocated_seconds"] - new_seconds),
        "remaining_authorized_calls": max(0, grant["allocated_calls"] - new_calls)}


def bind_grant(settings, new_run, descriptor):
    run, plan = _run(settings, new_run)
    grant = _load_grant(settings, descriptor)
    if ((run / "execution_started.json").exists() or list((run / "calls").glob("*/request.json"))
            or plan.get("budget_continuation") or plan.get("budget_policy")
            or plan["paper_id"] != grant["paper_id"] or _budget_source_hash(settings, run, plan) != grant["source_bundle_sha256"]
            or plan["max_calls"] > grant["allocated_calls"]
            or plan["model_settings"]["max_calls_per_run"] > grant["allocated_calls"]
            or plan["reading_settings"]["total_timeout_seconds"] > grant["allocated_seconds"]):
        raise ValueError("追加额度只能绑定未执行、来源一致且受限的新续处理计划")
    store = DataStore(settings.data_root)
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if any(event["grant_id"] == grant["grant_id"] for _, event in _events(store, BOUND, db)):
            raise ValueError("此追加额度已绑定后续运行，不能重复消费")
        write_new_json(run / "plan.before-budget-continuation.json", plan)
        plan["budget_continuation"] = descriptor
        plan["input_paths"].append({key: descriptor[key] for key in ("path", "sha256")})
        (run / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        event = {"grant_id": grant["grant_id"], "parent_run": grant["parent_run"], "root_run": grant["root_run"],
            "descriptor_sha256": digest_json(descriptor)}
        db.execute("INSERT INTO run_events(run_id,event,payload,created_at) VALUES(?,?,?,?)", (run.name, BOUND, json.dumps(event), utc_now()))
        db.execute("INSERT INTO run_events(run_id,event,payload,created_at) VALUES(?,?,?,?)",
            (run.name, "prepared", json.dumps({"plan_sha256": hash_file(run / "plan.json")}), utc_now()))
    return grant


def verify_grant_plan(settings, run, plan=None):
    run = Path(run).resolve(strict=True)
    plan = plan or _json(run / "plan.json")
    descriptor = plan.get("budget_continuation")
    if not descriptor:
        return None
    grant = _load_grant(settings, descriptor)
    expected = {"grant_id": grant["grant_id"], "parent_run": grant["parent_run"], "root_run": grant["root_run"],
        "descriptor_sha256": digest_json(descriptor)}
    if (DataStore(settings.data_root).last_event(run.name, BOUND) != expected
            or plan["paper_id"] != grant["paper_id"]
            or _budget_source_hash(settings, run, plan) != grant["source_bundle_sha256"]
            or plan["max_calls"] > grant["allocated_calls"]
            or plan["model_settings"]["max_calls_per_run"] > grant["allocated_calls"]
            or plan["reading_settings"]["total_timeout_seconds"] > grant["allocated_seconds"]
            or plan_model(plan) is None):
        raise ValueError("当前续处理计划未持有真实追加额度，或已改变来源/模型/预算")
    return grant


def verify_lineage_plan(settings, run, plan=None):
    """Enforce cumulative authority even for ordinary/coverage continuations."""
    run = Path(run).resolve(strict=True)
    plan = plan or _json(run / "plan.json")
    store = DataStore(settings.data_root)
    if (store.last_event(run.name, BOUND) and not plan.get("budget_continuation")
            or store.last_event(run.name, "budget_policy_frozen") and not plan.get("budget_policy")):
        raise ValueError("新计划不能移除已冻结的单篇预算控制")
    if plan.get("budget_continuation"):
        grant = verify_grant_plan(settings, run, plan)
        _no_unselected_descendants(settings, grant["usage_snapshot"], current_run=run.name)
        return grant
    if plan.get("budget_policy"):
        return verify_initial_policy(settings, run, plan)
    parent = parent_ref(settings, run, plan)
    if not parent:
        return None
    # Legacy source verifiers keep their exact contracts. Only a chain with an
    # explicit new policy or grant is subject to the new cumulative controller.
    probe, seen, managed = parent, set(), False
    while probe and probe not in seen:
        seen.add(probe)
        path = settings.data_root / "runs" / safe_name(probe)
        if not (path / "plan.json").is_file():
            break
        old = _json(path / "plan.json")
        if (old.get("budget_policy") or old.get("budget_continuation")
                or store.last_event(path.name, ADOPTED) or store.last_event(path.name, "budget_policy_frozen")):
            managed = True
            break
        probe = parent_ref(settings, path, old)
    if not managed:
        return None
    snapshot = lineage_snapshot(settings, parent)
    _no_unselected_descendants(settings, snapshot, current_run=run.name)
    root, root_plan = _run(settings, snapshot["root_run"])
    store = DataStore(settings.data_root)
    if not root_plan.get("budget_policy") and not store.last_event(root.name, ADOPTED):
        return None  # Preserve historical legacy contracts without invented authorization.
    policy, _ = _policy(settings, snapshot)
    chain = {item["run_id"] for item in snapshot["runs"]}
    bindings = {item["grant_id"]: owner for owner, item in _events(store, BOUND)}
    grants = [grant for _, grant in _grant_records(settings, root.name) if bindings.get(grant["grant_id"]) in chain]
    seconds = min(policy["max_total_seconds"], policy["initial_seconds"] + sum(g["added_seconds"] for g in grants))
    calls = min(policy["max_total_calls"], policy["initial_calls"] + sum(g["added_calls"] for g in grants))
    requested_seconds = plan["reading_settings"]["total_timeout_seconds"] if plan["max_calls"] else 0
    if (snapshot["used_seconds"] + requested_seconds > seconds
            or snapshot["used_calls"] + plan["max_calls"] > calls):
        raise ValueError("本续处理会超过单篇已授权的累计预算；请先追加额度，不能用独立补正绕过")
    return {"root_run": root.name, "used_seconds": snapshot["used_seconds"], "used_calls": snapshot["used_calls"],
        "authorized_seconds": seconds, "authorized_calls": calls}


def remaining_authority(settings, parent_run):
    """Read already-authorized unused resources, never granting an addition.

    Legacy chains deliberately return None before the stricter new accounting
    checks. They retain their pre-existing recovery contracts.
    """
    store = DataStore(settings.data_root)
    parent = Path(parent_run)
    probe = parent.name
    seen, managed = set(), False
    while probe and probe not in seen:
        seen.add(probe)
        path = settings.data_root / "runs" / safe_name(probe)
        plan = _json(path / "plan.json")
        if (plan.get("budget_policy") or plan.get("budget_continuation")
                or store.last_event(probe, ADOPTED) or store.last_event(probe, "budget_policy_frozen")):
            managed = True
            break
        probe = parent_ref(settings, path, plan)
    if not managed:
        return None
    snapshot = lineage_snapshot(settings, parent_run)
    policy, _ = _policy(settings, snapshot)
    chain = {item["run_id"] for item in snapshot["runs"]}
    bound = {item["grant_id"]: owner for owner, item in _events(store, BOUND)}
    grants = [grant for _, grant in _grant_records(settings, snapshot["root_run"])
        if bound.get(grant["grant_id"]) in chain]
    seconds = min(policy["max_total_seconds"], policy["initial_seconds"] + sum(g["added_seconds"] for g in grants))
    calls = min(policy["max_total_calls"], policy["initial_calls"] + sum(g["added_calls"] for g in grants))
    return {"root_run": snapshot["root_run"], "authorized_seconds": seconds, "authorized_calls": calls,
        "used_seconds": snapshot["used_seconds"], "used_calls": snapshot["used_calls"],
        "remaining_seconds": max(0, math.floor(seconds - snapshot["used_seconds"])),
        "remaining_calls": max(0, calls - snapshot["used_calls"])}


def retained_stage_budget(settings, run):
    """Use the managed allowance or the original paper's unspent, authenticated legacy limit."""
    balance = remaining_authority(settings, run)
    if balance is None:
        snapshot = lineage_snapshot(settings, run)
        root, plan = _run(settings, snapshot["root_run"])
        seconds, calls = plan["reading_settings"]["total_timeout_seconds"], plan["max_calls"]
        balance = {"root_run": root.name, "authorized_seconds": seconds, "authorized_calls": calls,
            "used_seconds": snapshot["used_seconds"], "used_calls": snapshot["used_calls"],
            "remaining_seconds": max(0, math.floor(seconds - snapshot["used_seconds"])),
            "remaining_calls": max(0, calls - snapshot["used_calls"])}
    return {**balance, "original_calls": balance["authorized_calls"], "original_seconds": balance["authorized_seconds"],
        "policy": "lineage_remaining"}


def authenticate_unfinished_reading(settings, parent_run):
    """Authenticate a genuinely failed first understanding turn, never a read."""
    from .web_initial_recovery import _parent
    from .dossier_extraction_resume import _failed_call_started
    from .workflow import scientific_prompt
    store, run, plan, imported, bundle, _ = _parent(settings, parent_run)
    terminal, failure = _json(run / "result.json"), _json(run / "failure.json")
    if (plan.get("reused_dossier") or plan.get("reused_candidate")
            or terminal.get("status") != "reading_incomplete_failure"
            or terminal.get("last_candidate_version") is not None
            or terminal.get("model_calls") != 1 or failure.get("model_calls") != 1
            or terminal.get("human_validated") is not False or terminal.get("graph_published") is not False
            or list(run.glob("reading_dossier*.json")) or list(run.glob("candidate*.json"))
            or list(run.glob("audit*.json")) or list((run / "reading_tools").rglob("*submitted_record.json"))
            or list((run / "calls").glob("*/answer.json")) or list((run / "calls").glob("*/record.json"))):
        raise ValueError("只有尚未交付理解档案或候选的首次理解失败可继续该阶段")
    requests = list((run / "calls").glob("*/request.json"))
    if len(requests) != 1:
        raise ValueError("首次理解失败的模型调用不唯一，不能丢弃其它成果")
    request_path = requests[0]
    envelope, output = _json(request_path), _json(request_path.with_name("result.json"))
    request = envelope["request"]
    if (envelope.get("fingerprint") != digest_json(request) or request.get("stage") != "comprehension"
            or not request_matches_plan(plan, request)
            or request.get("code_identity") != plan.get("code_identity") or request.get("rule_version") != plan["rule_version"]
            or output.get("status") != "failed" or not output.get("error")
            or failure.get("error") != output["error"] or output.get("submission_id")
            or isinstance(output.get("elapsed_seconds"), bool) or not isinstance(output.get("elapsed_seconds"), (int, float))
            or not math.isfinite(output["elapsed_seconds"]) or output["elapsed_seconds"] < 0):
        raise ValueError("未完成理解的身份、真实用量或失败原因不能认证")
    sources = {str(_safe(settings, value["path"])): value["sha256"] for value in request.get("sources", [])}
    if (any(sources.get(str(_safe(settings, value["path"]))) != value["sha256"] for value in plan["input_paths"])
            or any(hash_file(Path(path)) != checksum for path, checksum in sources.items())
            or hash_file(run / "taxonomy.json") != hash_file(settings.taxonomy_path)):
        raise ValueError("未完成理解的原文、来源交付或分类体系已变化")
    for stem, filename in (("understand", "understand_prompt.txt"), ("extract", "extract_prompt.txt")):
        if (run / filename).read_text(encoding="utf-8").split("\n\nDELIVERY:", 1)[0] != scientific_prompt(settings, stem, reader_model=plan_model(plan)):
            raise ValueError("未完成理解的科学指令已变化")
    _failed_call_started(run, plan, request, output)
    # A retained public final answer is work to assess, even if adapter failure
    # prevented answer.json from being written. It must never be discarded as
    # an unanswered first turn to justify spending another allowance.
    for line in (run / "app_server/events.jsonl").read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        message = event.get("message", {})
        params = message.get("params", {})
        if event.get("direction") != "receive":
            continue
        if (message.get("method") == "item/completed"
                and params.get("item", {}).get("type") == "agentMessage"
                and params.get("item", {}).get("phase") == "final_answer"):
            raise ValueError("首次理解已有公开最终答复，应先评估该交付而非丢弃重试")
        if (message.get("method") == "turn/completed"
                and params.get("turn", {}).get("status") == "completed"):
            raise ValueError("首次理解已有完成的公开回合，不能按未交付理解重新调用")
    return {"route": "unfinished_reading", "parent_run": run.name, "bundle_path": str(imported),
        "cohort_role": plan["cohort_role"], "reading_completed": False, "candidate_reused": False,
        "artifact_hashes": [{"path": str(_safe(settings, path)), "sha256": hash_file(path)}
            for path in sorted(run.rglob("*")) if path.is_file()]}
