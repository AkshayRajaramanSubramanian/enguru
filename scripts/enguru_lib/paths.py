"""State directory, gitignore, atomic JSON and lock primitives."""
import json
import os
import re
import shutil
import time
from pathlib import Path

LOCK_STALE_SECONDS = 14400
STATE_DIRNAME = ".enguru"


_SAFE_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def safe_id(value) -> bool:
    """True for ids that are safe to use in a file name and a shell argument."""
    return isinstance(value, str) and bool(_SAFE_ID.match(value)) and ".." not in value


class StateCorrupt(Exception):
    pass


def _fmt_time(t):
    if not _is_num(t):
        return "unknown"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class LockHeld(Exception):
    def __init__(self, pid, started, heartbeat=None, session_id=None):
        if session_id is not None:
            msg = "lock held by session %s since %s (last heartbeat %s)" % (
                session_id, _fmt_time(started), _fmt_time(heartbeat))
        else:
            msg = "lock held by pid %s since %s" % (pid, _fmt_time(started))
        super().__init__(msg)
        self.pid = pid
        self.started = started
        self.heartbeat = heartbeat
        self.session_id = session_id


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


def _read_lock(lock):
    try:
        held = json.loads(Path(lock).read_text())
    except (ValueError, OSError):
        return None
    return held if isinstance(held, dict) else None


def _lock_stale(held, now) -> bool:
    """Session locks (pid null) go stale by heartbeat age; PID locks by a dead PID or age."""
    if held is None:
        return True  # unreadable lock counts as stale
    if held.get("pid") is None and "session_id" in held:
        hb = held.get("heartbeat")
        return not _is_num(hb) or now - hb > LOCK_STALE_SECONDS
    pid, started = held.get("pid"), held.get("started")
    return pid is None or not _is_num(started) or not _pid_alive(pid) or now - started > LOCK_STALE_SECONDS


def _write_lock_file(lock, payload):
    tmp = lock.with_name(lock.name + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, lock)


def acquire_lock(sd: Path, now=None, session_id=None, force=False) -> None:
    """Take the lock. With session_id it is a session lock (pid null, heartbeat);
    otherwise it is tied to this process's PID. force reclaims a held lock."""
    sd = Path(sd)
    lock = sd / "lock"
    now = time.time() if now is None else now
    if session_id is not None:
        payload = {"pid": None, "session_id": session_id, "started": now, "heartbeat": now}
    else:
        payload = {"pid": os.getpid(), "started": now}
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            held = _read_lock(lock)
            if not force and not _lock_stale(held, now):
                raise LockHeld(held.get("pid"), held.get("started"), held.get("heartbeat"),
                               held.get("session_id"))
            try:
                lock.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(payload))
        return
    raise LockHeld(None, None)


def touch_lock(sd: Path, session_id, now=None) -> None:
    """Refresh a session lock's heartbeat; re-create it if missing.
    Raises LockHeld if another session holds it."""
    lock = Path(sd) / "lock"
    now = time.time() if now is None else now
    held = _read_lock(lock)
    if held is None or not lock.exists():
        acquire_lock(sd, now=now, session_id=session_id)
        return
    if held.get("session_id") != session_id:
        if not _lock_stale(held, now):
            raise LockHeld(held.get("pid"), held.get("started"), held.get("heartbeat"),
                           held.get("session_id"))
        acquire_lock(sd, now=now, session_id=session_id, force=True)
        return
    held["heartbeat"] = now
    _write_lock_file(lock, held)


def release_lock(sd: Path, session_id=None) -> None:
    """Remove the lock; with session_id, only if that session holds it."""
    lock = Path(sd) / "lock"
    if session_id is not None:
        held = _read_lock(lock)
        if held is None or held.get("session_id") != session_id:
            return
    try:
        lock.unlink()
    except FileNotFoundError:
        pass
