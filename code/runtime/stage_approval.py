"""One explicit authorization per model-stage invocation in an agent run."""

from datetime import datetime, timezone
from pathlib import Path
from runtime.artifacts import save
from runtime.registry import DEPENDENCIES, MODEL_STAGES, folder
from runtime.agent_contract import agent_state, event
from runtime.stage_parameters import model_override


def binding(root, run, number):
    state = agent_state(run)
    settings = state["settings"]
    result = {
        "stage": number,
        "candidate_model": settings.get("candidate_model") or "gpt-5.4",
        "candidate_cache_run": settings.get("candidate_cache_run"),
        "base_url": settings.get("base_url") or "",
        "dependencies": {
            str(n): (Path("state") / folder(n) / "completed.json").as_posix()
            for n in DEPENDENCIES[number]
        },
        "feedback": state.get("approval"),
        "library": None if number == 12 else state.get("library"),
    }
    override = model_override(run, number)
    if override is not None:
        result["stage_model"] = override
    return result


def available(root, run, number):
    state = agent_state(run)
    reference = state.get("stage_approvals", {}).get(str(number))
    if not reference:
        return False
    consumed = (
        Path(run)
        / "state/stage_approval_consumed"
        / (Path(reference["path"]).stem + ".json")
    )
    # A spent approval cannot authorize another invocation. Keep its historical
    # binding intact and let the agent preview a fresh approval after a change.
    if consumed.exists():
        return False
    value = event(run, reference)
    if value.get("kind") != "stage_approval" or value.get("binding") != binding(
        root, run, number
    ):
        raise ValueError(
            "LLM-stage approval does not match the current stage, inputs or settings"
        )
    return True


def consume(root, run, number, config):
    """Called under the stage lock, after completed-result reuse checks."""
    state = agent_state(run)
    if state is None or number not in MODEL_STAGES:
        return
    if not available(root, run, number):
        raise ValueError(
            f"LLM stage {number} requires explicit approval through agent approve --stage {number}"
        )
    expected = binding(root, run, number)
    if any((config[k] != expected[k] for k in ("candidate_model", "base_url"))):
        raise ValueError("Execution settings differ from the approved LLM stage")
    if number in {5, 8, 10, 12}:
        config["_approved_stage_model"] = expected.get("stage_model")
    reference = state["stage_approvals"][str(number)]
    save(
        Path(run)
        / "state/stage_approval_consumed"
        / (Path(reference["path"]).stem + ".json"),
        {
            "stage": number,
            "approval": reference,
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "scope": "One stage invocation, including its concurrent requests and automatic retries",
        },
    )
