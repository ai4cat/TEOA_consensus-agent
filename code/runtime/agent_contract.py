"""Human decisions and late source inputs bound to the stage receipt chain."""

from pathlib import Path
from runtime.artifacts import read, validate

LIBRARY = "inputs/agent_library"


def agent_state(run):
    path = Path(run) / "state/agent.json"
    return read(path) if path.exists() else None


def event(run, reference):
    path = (Path(run) / reference["path"]).resolve()
    if not path.is_relative_to((Path(run) / "state/agent_events").resolve()):
        raise ValueError("Invalid agent event location")
    return read(path)


def stage_contract(run, number):
    state = agent_state(run)
    if state is None or number <= 2:
        return {}
    approval = state.get("approval")
    if not approval or event(run, approval).get("decision") != "approve":
        raise ValueError("Agent is waiting for explicit stage2 feedback")
    result = {"approval_event": approval["path"]}
    if number in {9, 10, 11, 13}:
        binding = state.get("library")
        if not binding:
            raise ValueError("Agent is waiting to start the online reference collection after stage8")
        event(run, binding)
        value = read(Path(run) / LIBRARY / "manifest.json")
        validate(Path(run) / LIBRARY / "02_documents", value["files"])
        result["library_event"] = binding["path"]
    return result


def document_folder(root, run, number):
    if number in {9, 10, 11, 13} and agent_state(run) is not None:
        stage_contract(run, number)
        return Path(run) / LIBRARY / "02_documents"
    return Path(root) / "inputs/02_documents"


def resolve_source(ctx, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Invalid source provenance path")
    if path.as_posix().startswith(LIBRARY + "/"):
        if agent_state(ctx.run) is None:
            raise ValueError("Second-library source has no agent input binding")
        return ctx.run / path
    return ctx.root / path
