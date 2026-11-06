import argparse, importlib, json, os, shutil, sys, traceback, uuid
from datetime import datetime, timezone
from pathlib import Path
from runtime.artifacts import read, save, inventory, validate, RunLock
from runtime.registry import (
    NAMES,
    DEPENDENCIES,
    MODEL_STAGES,
    NETWORK_STAGES,
    folder,
    artifact_directory,
    STAGE_LAYOUT,
    EXECUTION_ORDER,
)
from runtime.control import Controller, PauseRequested, disable_network
from runtime.cli_defaults import default_run

ROOT = Path(__file__).resolve().parents[2]
SOURCE_GROUPS = {n: [] for n in NAMES}
SOURCE_GROUPS.update({1: ["02_documents"], 2: ["01_prompts"], 9: ["02_documents"], 12: ["03_platform_documents"]})


def verify_sources(number, all_inputs=False):
    required = ["code/workflow.py", "code/pipeline/claims/entity_prompts.py"]
    groups = ("01_prompts", "02_documents", "03_platform_documents") if all_inputs else SOURCE_GROUPS[number]
    for group in groups:
        if not (ROOT / "inputs" / group).is_dir():
            raise ValueError("Missing input directory: " + group)
    for name in required:
        if not (ROOT / name).is_file():
            raise ValueError("Missing source file: " + name)


def receipt_path(run, number):
    return Path(run) / "state" / folder(number) / "completed.json"


def validate_stage(run, number, identity, seen=None):
    seen = set() if seen is None else seen
    path = receipt_path(run, number)
    if not path.is_file():
        raise ValueError("Missing completed prerequisite: " + folder(number))
    receipt = read(path)
    if receipt.get("state") != "completed" or receipt.get("stage") != number:
        raise ValueError("Invalid stage completion record")
    if number in seen:
        return receipt
    seen.add(number)
    validate(artifact_directory(Path(run), number), receipt["files"])
    for dep in DEPENDENCIES[number]:
        validate_stage(run, dep, identity, seen)
    if number == 8:
        from runtime.scoring_scope import validate_summary_scope
        validate_summary_scope(Path(run))
    return receipt


def commit(run, number, contract, pending):
    state = run / "state" / folder(number)
    destination = artifact_directory(run, number)
    record = {
        "state": "completed",
        "stage": number,
        "contract": contract,
        "artifact_directory": destination.relative_to(run).as_posix(),
        "files": inventory(pending),
        "completed_utc": datetime.now(timezone.utc).isoformat(),
    }
    if not record["files"]:
        raise ValueError("Stage produced no artifacts")
    save(state / "prepared.json", record)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("Uncommitted output exists; inspect prepared receipt")
    os.replace(pending, destination)
    save(state / "completed.json", record)
    return record


def recover_commit(run, number, contract):
    prepared = run / "state" / folder(number) / "prepared.json"
    destination = artifact_directory(run, number)
    if not destination.exists():
        return False
    if not prepared.is_file():
        raise ValueError("Output exists without a prepared commit")
    record = read(prepared)
    if record["contract"] != contract:
        raise ValueError("Interrupted commit configuration differs")
    validate(destination, record["files"])
    save(receipt_path(run, number), record)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Independent research stages; this source-only package defaults to online execution."
    )
    parser.add_argument(
        "stage",
        nargs="?",
        default="stages",
        help="1..13, preview, stages, extract, all, pause, resume, status, check",
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help="Show inputs/outputs only; do not start work or create a run",
    )
    parser.add_argument("--run-directory", type=Path)
    parser.add_argument(
        "--corpus-run",
        type=Path,
        help="Explicitly copy a completed stage1; content and source-version validation are disabled",
    )
    parser.add_argument("--mode", choices=["online"])
    parser.add_argument("--candidate-model")
    parser.add_argument("--credential-pool", type=Path)
    parser.add_argument("--candidate-cache-run", type=Path, help="Reuse a retained successful candidate response without a nomination model call")
    parser.add_argument("--base-url", default=None)
    parser.add_argument(
        "--initial-concurrency",
        type=int,
        default=None,
        help="Initial global concurrency; new runs default to 4",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=None,
        help="Global concurrency ceiling; new runs default to 16",
    )
    parser.add_argument("--batch-run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--agent-run", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    run = (args.run_directory or default_run(ROOT)).resolve()
    if run != ROOT and (run.is_relative_to(ROOT) or ROOT.is_relative_to(run)):
        parser.error("Runtime must be the repository root or a separate directory")
    command = args.stage.lower().removeprefix("stage")
    if args.stage.lower() == "stages":
        command = "stages"
    if command == "preview":
        verify_sources(2)
        if args.describe:
            print(
                json.dumps(
                    {
                        "command": "preview",
                        "calls_llm": False,
                        "output": str(run / "inputs/02_candidates"),
                    }
                )
            )
            return 0
        from preparation.nomination import prepare

        value = prepare(run / "work/prompt_preview")
        destination = run / "inputs/02_candidates"
        destination.mkdir(parents=True, exist_ok=True)
        for path in value["outputs"]:
            shutil.copy2(path, destination / Path(path).name)
        print(
            json.dumps(
                {"command": "preview", "calls_llm": False, "output": str(destination)}
            )
        )
        return 0
    if command == "stages" or args.describe:
        selected = [1, 2, 3, 4] if command == "extract" else EXECUTION_ORDER if command in ("stages", "all") else [int(command)]
        if any((n not in NAMES for n in selected)):
            parser.error("Unknown stage")
        for n in selected:
            print(
                json.dumps(
                    {
                        "stage": n,
                        "name": NAMES[n],
                        "source_inputs": [
                            str(ROOT / "inputs" / g) for g in SOURCE_GROUPS[n]
                        ],
                        "upstream_outputs": [
                            str(artifact_directory(run, d)) for d in DEPENDENCIES[n]
                        ],
                        "publishes_to": str(artifact_directory(run, n)),
                        "calls_llm": n in MODEL_STAGES,
                        "uses_network": n in NETWORK_STAGES,
                        "executes": False,
                    },
                    ensure_ascii=False,
                )
            )
        return 0
    if command == "check":
        verify_sources(1, all_inputs=True)
        print("Required source paths available; content validation disabled")
        return 0
    if command == "status":
        for n in NAMES:
            p = run / "state" / folder(n) / "status.json"
            if p.exists():
                print(json.dumps(read(p), ensure_ascii=False))
        if (run / "state/run.lock").exists():
            print("Active lock:", json.dumps(read(run / "state/run.lock")))
        if (run / "state/batch.json").exists():
            print("Batch:", json.dumps(read(run / "state/batch.json")))
        return 0
    if command == "pause":
        Controller(run).request()
        print(
            "Pause requested. Wait for state=paused or completed; do not close the running window."
        )
        return 0
    if command == "resume":
        batch = (
            read(run / "state/batch.json")
            if (run / "state/batch.json").exists()
            else {}
        )
        if batch.get("state") in ("paused", "failed", "running"):
            command = batch.get("command", "all")
        else:
            last = read(run / "state/last.json")
            if last["state"] not in ("paused", "failed", "running"):
                raise ValueError("No unfinished stage to resume")
            command = str(last["stage"])
    if command in ("all", "extract"):
        import subprocess

        run.mkdir(parents=True, exist_ok=True)
        batch_lock = RunLock(run, command, kind="batch")
        batch_lock.acquire()
        batch_control = Controller(run)
        try:
            probe = RunLock(run, "batch_start")
            probe.acquire()
            probe.release()
            batch_control.flag.unlink(missing_ok=True)
            batch_control.install_signal()
            stages = [1, 2, 3, 4] if command == "extract" else EXECUTION_ORDER
            for number in stages:
                if batch_control.requested():
                    save(
                        run / "state/batch.json",
                        {"state": "paused", "next_stage": number, "command": command},
                    )
                    return 75
                save(
                    run / "state/batch.json",
                    {"state": "running", "next_stage": number, "pid": os.getpid(), "command": command},
                )
                child = [
                    sys.executable,
                    "-u",
                    "-B",
                    str(ROOT / "code/workflow.py"),
                    str(number),
                    "--run-directory",
                    str(run),
                    "--batch-run",
                ]
                if args.initial_concurrency is not None:
                    child += ["--initial-concurrency", str(args.initial_concurrency)]
                if args.max_concurrency is not None:
                    child += ["--max-concurrency", str(args.max_concurrency)]
                if args.corpus_run:
                    child += ["--corpus-run", str(args.corpus_run.resolve())]
                if args.mode:
                    child += ["--mode", args.mode]
                if args.candidate_model:
                    child += ["--candidate-model", args.candidate_model]
                if args.credential_pool:
                    child += ["--credential-pool", str(args.credential_pool)]
                if args.candidate_cache_run:
                    child += ["--candidate-cache-run", str(args.candidate_cache_run.resolve())]
                if args.base_url:
                    child += ["--base-url", args.base_url]
                result = subprocess.run(child)
                if result.returncode:
                    save(
                        run / "state/batch.json",
                        {
                            "state": "paused" if result.returncode == 75 else "failed",
                            "next_stage": number,
                            "command": command,
                        },
                    )
                    return result.returncode
            save(run / "state/batch.json", {"state": "completed", "next_stage": None, "command": command})
            return 0
        finally:
            batch_lock.release()
    number = int(command)
    if number not in NAMES:
        parser.error("Stage must be 1 through 13")
    from runtime.agent_contract import agent_state, stage_contract

    if agent_state(run) is not None and (not args.agent_run):
        probe = RunLock(run, "manual_stage", kind="agent")
        probe.acquire()
        probe.release()
    run.mkdir(parents=True, exist_ok=True)
    lock = RunLock(run, number)
    lock.acquire()
    ctx = None
    controller = Controller(run)
    if not args.batch_run:
        controller.flag.unlink(missing_ok=True)
    controller.install_signal()
    state = run / "state" / folder(number)
    state.mkdir(parents=True, exist_ok=True)

    def status(value, **extra):
        record = {
            "stage": number,
            "name": NAMES[number],
            "state": value,
            "time_utc": datetime.now(timezone.utc).isoformat(),
            **extra,
        }
        save(state / "status.json", record)
        save(run / "state/last.json", record)
        print(json.dumps(record, ensure_ascii=False), flush=True)

    try:
        config_path = run / "state/run_config.json"
        if config_path.exists():
            config = read(config_path)
            if config.get("stage_layout") != STAGE_LAYOUT:
                raise ValueError(
                    "Stage numbering changed; use a new linear-stages run"
                )
            if args.corpus_run and config.get("corpus_run") != str(
                args.corpus_run.resolve()
            ):
                raise ValueError("Corpus source cannot change inside a run")
            if args.mode and config["mode"] != args.mode:
                raise ValueError("Run mode cannot change")
            if args.candidate_cache_run and config.get("candidate_cache_run") != str(args.candidate_cache_run.resolve()):
                raise ValueError("Candidate cache source cannot change inside a run")
            if (
                args.candidate_model
                and config["candidate_model"] != args.candidate_model
            ):
                raise ValueError("Candidate model cannot change")
            if args.base_url and config["base_url"] != args.base_url:
                raise ValueError("Endpoint cannot change")
        else:
            mode = args.mode or "online"
            config = {
                "mode": mode,
                "stage_layout": STAGE_LAYOUT,
                "corpus_run": str(args.corpus_run.resolve())
                if args.corpus_run
                else None,
                "candidate_model": args.candidate_model or "gpt-5.4",
                "candidate_cache_run": str(args.candidate_cache_run.resolve()) if args.candidate_cache_run else None,
                "base_url": args.base_url or "",
                "initial_concurrency": 4
                if args.initial_concurrency is None
                else args.initial_concurrency,
                "max_concurrency": 16
                if args.max_concurrency is None
                else args.max_concurrency,
            }
            if not 1 <= config["initial_concurrency"] <= config["max_concurrency"]:
                raise ValueError("Invalid concurrency")
            config["identity"] = uuid.uuid4().hex
            save(config_path, config)
        previous = {k: config[k] for k in ("initial_concurrency", "max_concurrency")}
        if args.initial_concurrency is not None:
            config["initial_concurrency"] = args.initial_concurrency
        if args.max_concurrency is not None:
            config["max_concurrency"] = args.max_concurrency
        if not 1 <= config["initial_concurrency"] <= config["max_concurrency"]:
            raise ValueError("Invalid concurrency")
        current = {k: config[k] for k in previous}
        if current != previous:
            save(
                run / "state/performance_changes" / (uuid.uuid4().hex + ".json"),
                {
                    "before": previous,
                    "after": current,
                    "time_utc": datetime.now(timezone.utc).isoformat(),
                },
            )
            save(config_path, config)
        if number not in NETWORK_STAGES:
            disable_network()
        if args.credential_pool:
            config["credential_pool"] = str(args.credential_pool.resolve())
        else:
            config["credential_pool"] = None
        verify_sources(number)
        seen = set()
        for dep in DEPENDENCIES[number]:
            validate_stage(run, dep, config["identity"], seen)
        contract = {
            "identity": config["identity"],
            "stage": number,
            "dependencies": {
                str(dep): (Path('state') / folder(dep) / 'completed.json').as_posix() for dep in DEPENDENCIES[number]
            },
        }
        agent_binding = stage_contract(run, number)
        if agent_binding:
            contract["agent"] = agent_binding
        if receipt_path(run, number).exists():
            validate_stage(run, number, config["identity"])
            status("completed", reused=True)
            return 0
        if recover_commit(run, number, contract):
            status("completed", recovered_commit=True)
            return 0
        from runtime.provider_credentials import credentials_available
        if (number in MODEL_STAGES
                and not (number == 2 and config.get('candidate_cache_run'))
                and not config.get('base_url')):
            raise ValueError('Start a new run with --base-url set to your model provider endpoint')
        if (config['mode'] == 'online' and number in MODEL_STAGES
                and not (number == 2 and config.get('candidate_cache_run'))
                and not credentials_available(config)):
            raise ValueError('Set LLM_API_KEY or configure a local credential pool before model approval')
        controller.check()
        from runtime.stage_approval import consume

        consume(ROOT, run, number, config)
        from runtime.context import Context

        ctx = Context(ROOT, run, number, config, controller)
        status(
            "running",
            mode=config["mode"],
            pid=os.getpid(),
            calls_llm=number in MODEL_STAGES,
            initial_concurrency=config["initial_concurrency"],
            max_concurrency=config["max_concurrency"],
        )
        module = importlib.import_module(f"stages.s{number:02d}_{NAMES[number]}")
        module.run(ctx)
        if controller.seen:
            raise PauseRequested(
                "A model task was deferred; partial outputs cannot be published"
            )
        ctx.close()
        ctx = None
        verify_sources(number)
        for dep in DEPENDENCIES[number]:
            validate_stage(run, dep, config["identity"])
        if stage_contract(run, number) != agent_binding:
            raise ValueError("Agent input binding changed during stage execution")
        record = commit(
            run, number, contract, run / "work" / folder(number) / "publish"
        )
        status("completed", files=len(record["files"]))
        return 0
    except BaseException as exc:
        if ctx is not None:
            ctx.close()
        paused = Controller(run).requested()
        status(
            "paused" if paused else "failed",
            error_type=type(exc).__name__,
            message=str(exc)[:1200],
        )
        save(
            state / "last_exception.json",
            {
                "type": type(exc).__name__,
                "traceback": traceback.format_exc(),
                "pause_requested": paused,
            },
        )
        if paused:
            return 75
        raise
    finally:
        if ctx is not None:
            ctx.close()
        lock.release()
