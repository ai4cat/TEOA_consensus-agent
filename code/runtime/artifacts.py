import json, os, time, uuid
from pathlib import Path


def fs_path(path):
    path = Path(path)
    if os.name == "nt":
        value = str(path.absolute())
        if not value.startswith("\\\\?\\"):
            value = (
                "\\\\?\\UNC\\" + value[2:]
                if value.startswith("\\\\")
                else "\\\\?\\" + value
            )
        return Path(value)
    return path


def read(path):
    return json.loads(fs_path(path).read_text(encoding="utf-8-sig"))


def save(path, value):
    path = fs_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(temp, path)
            break
        except PermissionError as exc:
            if (
                os.name != "nt"
                or getattr(exc, "winerror", None) not in (5, 32, 33)
                or attempt == 19
            ):
                raise
            time.sleep(min(0.01 * (attempt + 1), 0.1))


def inventory(folder):
    folder = fs_path(folder)
    return {
        p.relative_to(folder).as_posix(): {"bytes": p.stat().st_size}
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def validate(folder, files):
    root = Path(folder).resolve()
    for name in files:
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Missing or invalid artifact path: " + name)


def table(path, rows, fields=None):
    from shared.storage import write_csv

    path = fs_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(path, rows, fields or list(dict.fromkeys((k for r in rows for k in r))))


def process_identity(pid):
    """PID plus process creation time, to avoid treating a reused PID as the owner."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            *[ctypes.POINTER(wintypes.FILETIME)] * 4,
        ]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(4096, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return None
            raise OSError("Cannot verify lock process identity")
        try:
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *[ctypes.byref(v) for v in values]):
                raise OSError("GetProcessTimes failed")
            return str(values[0].dwHighDateTime << 32 | values[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    path = Path("/proc") / str(pid) / "stat"
    try:
        return path.read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


class RunLock:
    def __init__(self, root, stage, kind="run"):
        self.path = Path(root) / "state" / (kind + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.value = {
            "pid": os.getpid(),
            "process_identity": process_identity(os.getpid()),
            "stage": stage,
            "token": uuid.uuid4().hex,
        }

    def acquire(self):
        self.guard = self.path.with_suffix(".guard").open("a+b")
        self.guard.seek(0, 2)
        if self.guard.tell() == 0:
            self.guard.write(b"0")
            self.guard.flush()
        self.guard.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.guard.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.guard.close()
            raise RuntimeError("Another stage owns the operating-system run lock")
        if self.path.exists():
            self.path.rename(
                self.path.with_name("stale_lock_" + uuid.uuid4().hex + ".json")
            )
        save(self.path, self.value)

    def release(self):
        if self.path.exists() and read(self.path).get("token") == self.value["token"]:
            self.path.unlink()
        if getattr(self, "guard", None):
            self.guard.close()
