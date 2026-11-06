"""Run-local model selections for clustering, summaries, scoring and systems."""

from pathlib import Path
from runtime.artifacts import read

OVERRIDE_STAGES = {5, 8, 10, 12}

def model_override(run, number):
    if number not in OVERRIDE_STAGES:
        return None
    path = Path(run) / "state/stage_parameters.json"
    if not path.exists():
        return None
    value = read(path)
    if not isinstance(value, dict) or value.get("schema") != "stage-parameters.v1":
        raise ValueError("Invalid stage_parameters.json schema")
    stages = value.get("stages")
    if not isinstance(stages, dict) or set(stages) - {str(n) for n in OVERRIDE_STAGES}:
        raise ValueError("Model overrides are supported for stages 5, 8, 10 and 12")
    parameters = stages.get(str(number), {})
    if not isinstance(parameters, dict) or set(parameters) - {"model"}:
        raise ValueError("Only the stage model parameter is supported")
    model = parameters.get("model")
    if model is not None and (
        not isinstance(model, str) or not model.strip() or model != model.strip()
    ):
        raise ValueError("Stage model must be a nonempty model name")
    return model


def resolve_model(run, number, config, default):
    if number not in OVERRIDE_STAGES:
        return default
    # Approval pins the selection for the entire invocation, including retries.
    if "_approved_stage_model" in config:
        return config["_approved_stage_model"] or default
    return model_override(run, number) or default
