"""Per-package CLI preferences stored in the user's configuration directory."""

import json
from pathlib import Path


def default_run(root):
    root = Path(root).resolve()
    if (root / 'state/run_config.json').is_file():
        return root
    config = Path.home() / '.config/photocatalysis/cli.json'
    projects = json.loads(config.read_text(encoding='utf-8-sig')).get('projects', {}) if config.exists() else {}
    entry = next((value for key, value in projects.items()
                  if str(Path(key).resolve()).casefold() == str(root).casefold()), {})
    value = entry.get('run_directory')
    if value:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError('Configured run_directory must be absolute')
        return path
    runs = root.with_name(root.name + '.runs')
    selector = runs / 'active_run.txt'
    if selector.is_file():
        name = selector.read_text(encoding='utf-8-sig').strip()
        if not name or Path(name).name != name or name in {'.', '..'}:
            raise ValueError('active_run.txt must contain one run directory name')
        return runs / name
    candidates = sorted(p for p in runs.iterdir() if p.is_dir() and
                        ((p/'state/agent.json').is_file() or (p/'state/run_config.json').is_file())) if runs.is_dir() else []
    if len(candidates) > 1:
        raise ValueError('Multiple runs found; select one with active_run.txt or --run-directory')
    return candidates[0] if candidates else runs / 'preprocess_first_001'
