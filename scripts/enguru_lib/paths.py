"""State directory, gitignore, atomic JSON and lock primitives."""
import json
import os
import shutil
import time
from pathlib import Path

LOCK_STALE_SECONDS = 14400
STATE_DIRNAME = ".enguru"


class StateCorrupt(Exception):
    pass


class LockHeld(Exception):
    def __init__(self, pid, started):
        super().__init__("lock held by pid %s since %s" % (pid, started))
        self.pid = pid
        self.started = started


def state_dir(repo: Path) -> Path:
    sd = Path(repo) / STATE_DIRNAME
    sd.mkdir(parents=True, exist_ok=True)
    return sd


def ensure_gitignore(repo: Path) -> bool:
    gi = Path(repo) / ".gitignore"
    text = gi.read_text() if gi.exists() else ""
    for line in text.splitlines():
        if line.strip() in (".enguru/", ".enguru", "/.enguru/", "/.enguru"):
            return False
    if text and not text.endswith("\n"):
        text += "\n"
    gi.write_text(text + ".enguru/\n")
    return True


def atomic_write_json(path: Path, data) -> None:
    path = Path(path)
    bak = path.with_name(path.name + ".bak")
    tmp = path.with_name(path.name + ".tmp")
    if path.exists():
        shutil.copyfile(path, bak)
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: Path):
    path = Path(path)
    bak = path.with_name(path.name + ".bak")
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError:
        pass
    try:
        with open(bak) as f:
            data = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        raise StateCorrupt("%s and its backup are unreadable" % path)
    shutil.copyfile(bak, path)
    return data


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def acquire_lock(sd: Path, now=None) -> None:
    sd = Path(sd)
    lock = sd / "lock"
    now = time.time() if now is None else now
    payload = json.dumps({"pid": os.getpid(), "started": now})
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                held = json.loads(lock.read_text())
                pid, started = held["pid"], held["started"]
            except (ValueError, KeyError, TypeError, OSError):
                pid, started = None, None  # unreadable lock counts as stale
            stale = (
                pid is None
                or not _pid_alive(pid)
                or now - started > LOCK_STALE_SECONDS
            )
            if not stale:
                raise LockHeld(pid, started)
            try:
                lock.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        return
    raise LockHeld(None, None)


def release_lock(sd: Path) -> None:
    try:
        (Path(sd) / "lock").unlink()
    except FileNotFoundError:
        pass
