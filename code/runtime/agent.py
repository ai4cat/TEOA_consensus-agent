"""Human-in-the-loop orchestration. Decisions are explicit; no scientific edits."""

import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from runtime.artifacts import read, save, RunLock
from runtime.control import Controller
from runtime.registry import folder, MODEL_STAGES, NAMES, STAGE_LAYOUT, EXECUTION_ORDER
from runtime.agent_contract import agent_state, LIBRARY
from runtime.cli_defaults import default_run

ROOT = Path(__file__).resolve().parents[2]


def record(run, kind, **data):
    path = run / "state/agent_events" / (uuid.uuid4().hex + ".json")
    save(
        path, {"kind": kind, "time_utc": datetime.now(timezone.utc).isoformat(), **data}
    )
    return {"path": path.relative_to(run).as_posix(), "event_id": path.stem}


def show(run, state):
    value = {
        "run": str(run),
        "state": state["state"],
        "next_stage": state.get("next_stage"),
    }
    if state["state"] in ("waiting_feedback", "held"):
        value.update(
            candidates=str(run / "outputs/02_candidates/candidates.json"),
            action='agent feedback --decision approve|hold --note "your feedback"',
        )
        path = run / "outputs/02_candidates/candidates.json"
        if path.exists():
            value["candidates_result"] = read(path)
    elif state["state"] == "waiting_stage":
        number = state["next_stage"]
        value.update(
            stage_name=NAMES[number],
            calls_llm=False,
            final_claims=str(run / "outputs/07_themes/04_consensus_claims.csv"),
            action=f"agent run --stage {number}",
        )
    elif state["state"] == "waiting_stage_approval":
        number = state["next_stage"]
        value.update(
            stage_name=NAMES[number],
            calls_llm=True,
            approval_scope="One stage invocation, including concurrent requests and automatic retries",
            action=f"agent approve --stage {number}",
            approval_binding=state.get("pending_stage_binding"),
        )
    print(json.dumps(value, ensure_ascii=False), flush=True)


def update(run, record_value, **changes):
    record_value.update(changes)
    save(run / "state/agent.json", record_value)


def drive(root, run, state, runner=subprocess.run):
    controller = Controller(run)
    controller.install_signal()
    for number in EXECUTION_ORDER:
        if controller.requested():
            update(run, state, state="paused", next_stage=number)
            show(run, state)
            return 75
        if number == 3 and (not state.get("approval")):
            update(run, state, state="waiting_feedback", next_stage=3)
            show(run, state)
            return 0
        if number == 9 and (not state.get("library")):
            update(run, state, state="waiting_stage", next_stage=9)
            show(run, state)
            return 0
        from runtime.stage_approval import available, binding

        completed = run / "state" / folder(number) / "completed.json"
        if (
            number in MODEL_STAGES
            and (not completed.exists())
            and (not available(root, run, number))
        ):
            update(
                run,
                state,
                state="waiting_stage_approval",
                next_stage=number,
                pending_stage_binding=binding(root, run, number),
            )
            show(run, state)
            return 0
        update(run, state, state="running", next_stage=number)
        command = [
            sys.executable,
            "-u",
            "-B",
            str(root / "code/workflow.py"),
            str(number),
            "--run-directory",
            str(run),
            "--batch-run",
            "--agent-run",
        ]
        for key, value in state["settings"].items():
            if value is not None:
                command += ["--" + key.replace("_", "-"), str(value)]
        result = runner(command)
        if result.returncode:
            update(
                run,
                state,
                state="paused" if result.returncode == 75 else "failed",
                next_stage=number,
            )
            record(run, "stage_stopped", stage=number, returncode=result.returncode)
            show(run, state)
            return result.returncode
    update(run, state, state="completed", next_stage=None)
    show(run, state)
    return 0


def main(argv=None, *, root=None, runner=subprocess.run):
    root = Path(root or ROOT).resolve()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "run",
            "status",
            "approve",
            "feedback",
            "resume",
            "pause",
        ],
        nargs="?",
        default="status",
    )
    parser.add_argument("--stage", type=int, choices=sorted(NAMES))
    parser.add_argument("--run-directory", type=Path)
    parser.add_argument("--decision", choices=["approve", "hold"])
    parser.add_argument("--note", default="")
    parser.add_argument("--corpus-run", type=Path)
    parser.add_argument("--credential-pool")
    parser.add_argument("--candidate-cache-run", type=Path)
    parser.add_argument("--candidate-model")
    parser.add_argument("--base-url")
    parser.add_argument("--initial-concurrency", type=int)
    parser.add_argument("--max-concurrency", type=int)
    args = parser.parse_args(argv)
    if args.action in {"run", "approve"} and args.stage is None:
        parser.error("Specify --stage for run or approve")
    if args.action == "run" and args.stage in MODEL_STAGES:
        parser.error(f"Stage {args.stage} calls a model; use agent approve --stage {args.stage}")
    if args.action == "approve" and args.stage not in MODEL_STAGES:
        parser.error(f"Stage {args.stage} does not call a model; use agent run --stage {args.stage}")
    run = (args.run_directory or default_run(root)).resolve()
    if run != root and (run.is_relative_to(root) or root.is_relative_to(run)):
        parser.error("Agent runtime must be the repository root or a separate directory")
    if args.action == "status":
        state = agent_state(run)
        if state:
            show(run, state)
        else:
            print("No agent run. Use agent run --stage 1 with a fresh run directory.")
        return 0
    if args.action == "pause":
        if agent_state(run) is None:
            parser.error("No agent run")
        Controller(run).request()
        print("Pause requested; wait for the active stage to stop safely.")
        return 0
    lock = RunLock(run, args.action, kind="agent")
    lock.acquire()
    try:
        for kind in ("batch", "run"):
            probe = RunLock(run, "agent_preflight", kind=kind)
            probe.acquire()
            probe.release()
        state = agent_state(run)
        if args.corpus_run is not None:
            args.corpus_run = str(Path(args.corpus_run).resolve())
        if args.candidate_cache_run is not None:
            args.candidate_cache_run = str(args.candidate_cache_run.resolve())
        if state is None:
            if args.action != "run" or args.stage != 1:
                parser.error("Use agent run --stage 1 first")
            if (run / "state/run_config.json").exists() or (run / "outputs").exists():
                from runtime.engine import validate_stage

                config = read(run / "state/run_config.json")
                if config.get("stage_layout") != STAGE_LAYOUT:
                    raise ValueError(
                        "Use a new run; existing scientific runs are not migrated silently"
                    )
                outputs = run / "outputs"
                present = {p.name for p in outputs.iterdir()} if outputs.is_dir() else set()
                prefix = next(
                    (list(range(1, end + 1)) for end in range(1, 9)
                     if present == {folder(n) for n in range(1, end + 1)}),
                    None,
                )
                if prefix is None:
                    raise ValueError(
                        "Only a consecutive completed Stage1-through-Stage8 prefix may be adopted"
                    )
                for number in prefix:
                    validate_stage(run, number, config["identity"])
                for key in (
                    "candidate_model",
                    "candidate_cache_run",
                    "base_url",
                    "corpus_run",
                    "initial_concurrency",
                    "max_concurrency",
                ):
                    requested = getattr(args, key)
                    existing = config.get(key)
                    if requested is not None and str(requested) != str(existing):
                        raise ValueError("Existing run settings differ")
                    if requested is None:
                        setattr(args, key, existing)
            state = {
                "schema": "interactive-agent.v2",
                "stage_layout": STAGE_LAYOUT,
                "state": "ready",
                "next_stage": 1,
                "settings": {
                    k: getattr(args, k)
                    for k in (
                        "credential_pool",
                        "candidate_cache_run",
                        "candidate_model",
                        "base_url",
                        "initial_concurrency",
                        "max_concurrency",
                        "corpus_run",
                    )
                },
            }
            update(run, state)
            record(run, "created")
        if state.get("stage_layout") != STAGE_LAYOUT:
            raise ValueError("Stage numbering changed; use a new linear-stages run")
        if args.action == "run" and (
            args.stage != state.get("next_stage")
            or state["state"] not in {"ready", "failed", "paused", "waiting_stage"}
        ):
            raise ValueError("Run requires the current local stage; complete the displayed checkpoint first")
        control = Controller(run)
        control.flag.unlink(missing_ok=True)
        control.install_signal()
        if args.action == "approve":
            if (
                state["state"] != "waiting_stage_approval"
                or args.stage != state["next_stage"]
            ):
                raise ValueError(
                    "Approval requires the exact stage currently waiting for approval"
                )
            from runtime.stage_approval import binding

            expected = binding(root, run, args.stage)
            if expected != state.get("pending_stage_binding"):
                raise ValueError(
                    "Stage inputs or settings changed since the approval preview"
                )
            decision = record(run, "stage_approval", binding=expected, note=args.note)
            approvals = dict(state.get("stage_approvals", {}))
            approvals[str(args.stage)] = decision
            update(
                run,
                state,
                state="ready",
                stage_approvals=approvals,
                pending_stage_binding=None,
            )
        elif args.action == "feedback":
            if state["state"] not in ("waiting_feedback", "held") or not args.decision:
                raise ValueError(
                    "Feedback requires waiting_feedback/held and an explicit approve or hold decision"
                )
            from runtime.engine import validate_stage

            validate_stage(run, 2, read(run / "state/run_config.json")["identity"])
            decision = record(
                run,
                "feedback",
                decision=args.decision,
                note=args.note,
                candidate_receipt="state/02_candidates/completed.json",
            )
            if args.decision == "hold":
                update(run, state, state="held", next_stage=3)
                show(run, state)
                return 0
            update(run, state, approval=decision)
        elif args.action == "run":
            if args.stage == 9 and not state.get("library"):
                from runtime.engine import validate_stage
                from runtime.agent_inputs import prepare_reference_inputs

                identity = read(run / "state/run_config.json")["identity"]
                for number in range(1, 9):
                    validate_stage(run, number, identity)
                prepare_reference_inputs(root, run)
                reference_binding = record(
                    run,
                    "reference_inputs",
                    note=args.note,
                    manifest_path=LIBRARY + "/manifest.json",
                )
                update(run, state, library=reference_binding)
            update(run, state, state="ready")
        elif state["state"] in (
            "waiting_feedback",
            "held",
            "waiting_stage",
            "waiting_stage_approval",
            "completed",
        ):
            show(run, state)
            return 0
        return drive(root, run, state, runner)
    finally:
        lock.release()
