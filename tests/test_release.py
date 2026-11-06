"""Offline behavioral checks for the public source distribution."""

import ast
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "code"), str(ROOT / "code/pipeline")]
from runtime.artifacts import read
from runtime.library import catalog
from runtime.curated_metadata import source_metadata
from runtime.platform_library import platform_plan
from stages.s13_export import copy_project_files
from preparation.nomination import prepare
from runtime.stage_approval import binding


class ReleaseTests(unittest.TestCase):
    def test_python_sources_parse(self):
        for path in (ROOT / "code").rglob("*.py"):
            with self.subTest(file=str(path.relative_to(ROOT))):
                ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))

    def test_synthetic_catalog_and_source_hash(self):
        rows = catalog(ROOT)
        self.assertEqual(len(rows), 1)
        for row in rows:
            path = ROOT / "inputs/02_documents" / row["relative_path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row["sha256"])
            meta = source_metadata(ROOT, path, read(path))
            self.assertEqual(meta["doi"], "")
            self.assertFalse(meta["curation"]["pdf_identity_revalidated_by_this_change"])

    def test_platform_input_plan(self):
        definition, payloads = platform_plan(ROOT)
        self.assertEqual(definition["documents"], ["SYNTHETIC_PLATFORM_001"])
        self.assertEqual(len(definition["tasks"]), 1)
        self.assertEqual(len(payloads), 1)

    def test_prompt_contract(self):
        with tempfile.TemporaryDirectory(prefix="release prompt ") as tmp:
            result = prepare(Path(tmp))
            self.assertEqual(result["new_model_calls"], 0)
            self.assertEqual(result["state"], "prepared")

    def test_approval_has_no_implicit_provider(self):
        with tempfile.TemporaryDirectory(prefix="release approval ") as tmp:
            run = Path(tmp)
            (run / "state").mkdir()
            (run / "state/agent.json").write_text(
                json.dumps({"settings": {}, "approval": None, "library": None}),
                encoding="utf-8",
            )
            self.assertEqual(binding(ROOT, run, 2)["base_url"], "")

    def test_exported_layout_runs_offline_and_stops_before_models(self):
        with tempfile.TemporaryDirectory(prefix="release package with spaces ") as tmp:
            base = Path(tmp)
            package = base / "portable project"
            package.mkdir()
            copy_project_files(ROOT, package)
            self.assertEqual((package / "LICENSE").read_bytes(), (ROOT / "LICENSE").read_bytes())
            shutil.copytree(ROOT / "inputs", package / "inputs")
            run = base / "offline run"
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
            env.pop("LLM_API_KEY", None)
            shell = shutil.which("pwsh") or shutil.which("powershell")
            if os.name == "nt" and shell:
                launch = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(package / "agent.ps1"), "-Action", "run", "-Stage", "1",
                          "-RunDirectory", str(run), "-PythonPath", sys.executable]
            else:
                launch = [sys.executable, "-B", str(package / "code/agent.py"),
                          "run", "--stage", "1", "--run-directory", str(run)]
            result = subprocess.run(launch, cwd=base, env=env, capture_output=True,
                                    text=True, encoding="utf-8", timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = read(run / "state/agent.json")
            self.assertEqual(state["state"], "waiting_stage_approval")
            self.assertEqual(state["next_stage"], 2)
            summary = read(run / "outputs/01_corpus/summary.json")
            self.assertEqual(summary["main_documents"], 1)
            self.assertGreater(summary["chunks"], 0)
            self.assertEqual(summary["model_calls"], 0)
            config = read(run / "state/run_config.json")
            self.assertEqual((config["initial_concurrency"], config["max_concurrency"]), (4, 16))
            self.assertEqual(config["base_url"], "")
            self.assertFalse((run / "outputs/02_candidates").exists())

            # Starting a model stage without an endpoint must fail before a request.
            missing = subprocess.run(
                [sys.executable, "-B", str(package / "code/workflow.py"), "2",
                 "--run-directory", str(run)], cwd=base, env=env, capture_output=True,
                text=True, encoding="utf-8", timeout=30,
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("--base-url", missing.stdout + missing.stderr)
            self.assertFalse((run / "communications/02_candidates").exists())

    @unittest.skipUnless(os.name == "nt", "PowerShell launcher is Windows-specific")
    def test_powershell_passes_provider_and_paths(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell unavailable")
        with tempfile.TemporaryDirectory(prefix="release command ") as tmp:
            for name, arguments in (("agent.ps1", ["-Action", "run", "-Stage", "1"]),
                                    ("stage.ps1", ["-Stage", "1"])):
                result = subprocess.run(
                    [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     str(ROOT / name), *arguments, "-RunDirectory", tmp,
                     "-PythonPath", sys.executable, "-BaseUrl", "https://example.invalid/v1",
                     "-ShowCommand"], capture_output=True, text=True,
                    encoding="utf-8", timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                command = json.loads(result.stdout)
                self.assertFalse(command["executes"])
                args = command["arguments"]
                self.assertEqual(args[args.index("--base-url") + 1], "https://example.invalid/v1")
                self.assertEqual(args[args.index("--run-directory") + 1], tmp)


if __name__ == "__main__":
    unittest.main()
