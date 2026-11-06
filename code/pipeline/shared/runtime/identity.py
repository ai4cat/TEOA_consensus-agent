"""Descriptive package identity; no content digest or source locking."""

from shared.runtime.paths import project_root


def package_identity():
    code = project_root().parent
    files = [
        p.relative_to(code).as_posix()
        for p in sorted(code.rglob("*"))
        if p.is_file()
        and p.suffix in (".py", ".json", ".ps1", ".txt")
        and ("__pycache__" not in p.parts)
    ]
    return {"distribution": "research-pipeline", "version": None, "files": files}
