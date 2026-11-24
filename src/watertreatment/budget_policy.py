"""Explicit per-paper execution budgets, frozen before a new extraction starts.

Execution limits do not change model reasoning effort or scientific acceptance.
Policies apply to a single extraction lineage; old plans are never rewritten.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .model_policy import ACTIVE_MODEL, require_active_model
from .storage import DataStore, hash_file, write_new_json


class BudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    initial_seconds: int = Field(ge=60, le=21600)
    initial_calls: int = Field(ge=3, le=20)
    addition_seconds: int = Field(ge=60, le=3600)
    addition_calls: int = Field(ge=1, le=10)
    max_auto_additions: int = Field(ge=0, le=5)
    max_total_seconds: int = Field(ge=60, le=21600)
    max_total_calls: int = Field(ge=3, le=20)

    @model_validator(mode="after")
    def cumulative_contains_initial(self):
        if self.max_total_seconds < self.initial_seconds or self.max_total_calls < self.initial_calls:
            raise ValueError("单篇累计上限不能小于单篇初始额度")
        return self


def default_policy(settings):
    seconds = max(60, min(21600, settings.reading.total_timeout_seconds))
    calls = max(3, min(20, settings.codex.max_calls_per_run))
    return BudgetPolicy(initial_seconds=seconds, initial_calls=calls,
        addition_seconds=1800, addition_calls=2,
        max_auto_additions=int(seconds < 21600 and calls < 20),
        max_total_seconds=min(21600, seconds + 1800),
        max_total_calls=min(20, calls + 2)).model_dump(mode="json")


def apply_initial_policy(settings, policy):
    policy = BudgetPolicy.model_validate(policy)
    require_active_model(settings.codex)
    result = settings.model_copy(deep=True)
    result.codex.max_calls_per_run = policy.initial_calls
    result.codex.timeout_seconds = min(result.codex.timeout_seconds, policy.initial_seconds)
    result.reading.total_timeout_seconds = policy.initial_seconds
    return result


def freeze_initial_policy(settings, run, policy, batch_id, job_id=None):
    policy = BudgetPolicy.model_validate(policy)
    run = Path(run).resolve(strict=True)
    if run.parent != (settings.data_root / "runs").resolve():
        raise ValueError("初始预算只能冻结在配置数据目录内的新运行")
    path = run / "plan.json"
    plan = json.loads(path.read_text(encoding="utf-8"))
    if (plan.get("run_id") != run.name or plan.get("workflow") != "active_reading"
            or (run / "execution_started.json").exists() or list((run / "calls").glob("*/request.json"))
            or plan.get("budget_policy") or plan.get("budget_continuation")
            or plan.get("reused_candidate") or plan.get("reused_dossier")
            or plan.get("max_calls", 0) > policy.initial_calls
            or plan.get("model_settings", {}).get("max_calls_per_run", 0) > policy.initial_calls
            or plan.get("reading_settings", {}).get("total_timeout_seconds") != policy.initial_seconds
            or plan.get("model_settings", {}).get("model") != ACTIVE_MODEL
            or plan.get("model_settings", {}).get("reasoning_effort") != "max"):
        raise ValueError("仅全新且模型/初始额度一致的抽取计划可以冻结批次预算")
    store = DataStore(settings.data_root)
    rows = [row for row in store.runs() if row["run_id"] == run.name]
    if (len(rows) != 1 or rows[0]["kind"] != "extraction"
            or Path(rows[0]["manifest_path"]).resolve() != run / "manifest.json"
            or (store.last_event(run.name, "prepared") or {}).get("plan_sha256") != hash_file(path)):
        raise ValueError("初始抽取计划的登记或完整性证明不匹配")
    artifact = run / "budget_policy.json"
    payload = {"version": 1, "policy": policy.model_dump(mode="json"),
        "root_run": run.name, "paper_id": plan["paper_id"], "batch_id": batch_id,
        "job_id": job_id, "source_bundle_sha256": hash_file(run / "sources.json")}
    write_new_json(artifact, payload)
    descriptor = {"path": str(artifact), "sha256": hash_file(artifact), "root_run": run.name}
    write_new_json(run / "plan.before-budget-policy.json", plan)
    plan["budget_policy"] = descriptor
    plan["input_paths"].append({key: descriptor[key] for key in ("path", "sha256")})
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    store.event(run.name, "budget_policy_frozen", payload)
    store.event(run.name, "prepared", {"plan_sha256": hash_file(path)})
    return payload


def verify_initial_policy(settings, run, plan=None):
    run = Path(run).resolve(strict=True)
    plan = plan or json.loads((run / "plan.json").read_text(encoding="utf-8"))
    descriptor = plan.get("budget_policy")
    if not descriptor:
        return None
    path = Path(descriptor["path"])
    if path != run / "budget_policy.json" or path.resolve(strict=True) != path or hash_file(path) != descriptor["sha256"]:
        raise ValueError("初始预算快照的位置或哈希已改变")
    payload = json.loads(path.read_text(encoding="utf-8"))
    policy = BudgetPolicy.model_validate(payload["policy"])
    if (descriptor.get("root_run") != run.name or payload.get("root_run") != run.name
            or payload.get("paper_id") != plan["paper_id"]
            or payload.get("source_bundle_sha256") != hash_file(run / "sources.json")
            or plan.get("budget_continuation")
            or plan.get("max_calls", 0) > policy.initial_calls
            or plan.get("model_settings", {}).get("max_calls_per_run", 0) > policy.initial_calls
            or plan["reading_settings"]["total_timeout_seconds"] > policy.initial_seconds
            or DataStore(settings.data_root).last_event(run.name, "budget_policy_frozen") != payload):
        raise ValueError("初始预算计划、来源或额度登记不匹配")
    return payload
