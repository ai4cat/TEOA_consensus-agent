"""Stage12 extracts platforms from the entire independent Lib2 collection."""

from platforms.extraction import execute_systems
from runtime.artifacts import read, save
from runtime.platform_library import prepare_platform_inputs, PAYLOAD_ROOT, REQUEST_DEFINITION
import shutil


def publish_results(ctx):
    manifest = read(ctx.work / "systems/manifest.json")
    manifest["payload_root"] = PAYLOAD_ROOT
    manifest["payload_scope"] = "run"
    manifest["response_root"] = "outputs/12_systems/extractions"
    for task in manifest["tasks"]:
        response = ctx.work / "systems" / task["response_file"]
        ctx.publish(response, "extractions/" + task["response_file"])
    ctx.communications.joinpath("input_definition").mkdir(parents=True, exist_ok=True)
    save(
        ctx.communications / "input_definition/prompt_schema.json",
        {key: manifest.pop(key) for key in ("prompt", "schema")},
    )
    manifest["request_definition"] = REQUEST_DEFINITION
    ctx.emit("extractions/manifest.json", manifest)
    shutil.copytree(ctx.work / 'platform_tasks', ctx.pending / 'input_tasks')
    for p in sorted((ctx.work / "systems_statistics").glob("*")):
        if p.is_file():
            ctx.publish(p, "statistics/" + p.name)


def run(ctx):
    source = ctx.work / 'platform_tasks'
    prepare_platform_inputs(ctx.root, source)
    transport, _ = ctx.setup_models()
    execute_systems(
        ctx.root,
        ctx.work,
        transport,
        ctx.status,
        source=source,
    )
    publish_results(ctx)
