"""Select the model for new work and authenticate each retained run under its own model."""
from __future__ import annotations


ACTIVE_MODEL = "gpt-6.2-sol"
LEGACY_MODEL = "gpt-6.1-sol"
REASONING_EFFORT = "max"


def require_active_model(config):
    if config.model != ACTIVE_MODEL or config.reasoning_effort != REASONING_EFFORT:
        raise ValueError(f"本项目的新模型调用固定使用 {ACTIVE_MODEL} / {REASONING_EFFORT}，请检查模型配置")


def plan_model(plan):
    """Extraction plans nest their settings; web question/review plans store them directly."""
    if "model_settings" in plan:
        profile = plan["model_settings"]
        if not isinstance(profile, dict):
            return None
        model, effort = profile.get("model"), profile.get("reasoning_effort")
    else:
        model, effort = plan.get("model"), plan.get("effort")
    if model in (LEGACY_MODEL, ACTIVE_MODEL) and effort == REASONING_EFFORT:
        return model
    return None


def request_matches_plan(plan, request):
    model = plan_model(plan)
    return (model is not None and request.get("model") == model
            and request.get("reasoning_effort") == REASONING_EFFORT)


def manifest_matches_plan(plan, manifest):
    model = plan_model(manifest)
    if model is None:
        return False
    if not any(key in plan for key in ("model", "model_settings", "effort")):
        return model == LEGACY_MODEL
    return plan_model(plan) == model


def receipt_matches_plan(plan, request, result, *, legacy_sparse=False):
    """A server selection is evidence only for its own plan and request.

    Some early 6.1 audit formats recorded the selection but omitted the model
    fields from the plan or request. Preserve their original admission boundary
    only at readers that previously supported them. Partial or conflicting
    declarations, and new-model receipts missing either binding, always fail.
    """
    selected = result.get("server_selected_model")
    if (selected not in (LEGACY_MODEL, ACTIVE_MODEL)
            or result.get("server_selected_effort") != REASONING_EFFORT
            or ("requested_model" in result and result["requested_model"] != selected)):
        return False
    allow_missing = legacy_sparse and selected == LEGACY_MODEL
    missing_plan = not any(key in plan for key in ("model_settings", "model", "effort"))
    missing_request = "model" not in request and "reasoning_effort" not in request
    if plan_model(plan) != selected and not (allow_missing and missing_plan):
        return False
    return ((request.get("model") == selected and request.get("reasoning_effort") == REASONING_EFFORT)
            or (allow_missing and missing_request))
