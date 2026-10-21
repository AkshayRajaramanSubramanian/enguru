import json
import os
import time

import pytest

from enguru_lib.paths import (
    LOCK_STALE_SECONDS,
    LockHeld,
    StateCorrupt,
    acquire_lock,
    atomic_write_json,
    ensure_gitignore,
    read_json,
    release_lock,
    state_dir,
)


def test_atomic_write_keeps_backup_and_restores(tmp_path):
    p = tmp_path / "progress.json"
    atomic_write_json(p, {"v": 1})
    atomic_write_json(p, {"v": 2})
    p.write_text("{corrupt")
    assert read_json(p) == {"v": 1}


def test_read_json_raises_when_both_corrupt(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{")
    (tmp_path / "x.json.bak").write_text("{")
    with pytest.raises(StateCorrupt):
        read_json(p)


def test_lock_held_by_live_pid(tmp_path):
    acquire_lock(tmp_path)
    with pytest.raises(LockHeld):
        acquire_lock(tmp_path)


def test_stale_lock_reclaimed_dead_pid(tmp_path):
    (tmp_path / "lock").write_text(json.dumps({"pid": 999999, "started": time.time()}))
    acquire_lock(tmp_path)


def test_stale_lock_reclaimed_by_age(tmp_path):
    (tmp_path / "lock").write_text(json.dumps({"pid": os.getpid(), "started": 0}))
    acquire_lock(tmp_path, now=LOCK_STALE_SECONDS + 1)


def test_release_lock(tmp_path):
    acquire_lock(tmp_path)
    release_lock(tmp_path)
    assert not (tmp_path / "lock").exists()
    release_lock(tmp_path)  # idempotent
    acquire_lock(tmp_path)


def test_lock_content_and_held_details(tmp_path):
    acquire_lock(tmp_path, now=100.0)
    data = json.loads((tmp_path / "lock").read_text())
    assert data == {"pid": os.getpid(), "started": 100.0}
    with pytest.raises(LockHeld) as e:
        acquire_lock(tmp_path, now=101.0)
    assert e.value.pid == os.getpid() and e.value.started == 100.0


def test_read_json_missing_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_json(tmp_path / "nope.json")


def test_state_dir_created(tmp_path):
    sd = state_dir(tmp_path)
    assert sd == tmp_path / ".enguru" and sd.is_dir()


def test_gitignore_added_once(tmp_path):
    assert ensure_gitignore(tmp_path) is True
    assert ensure_gitignore(tmp_path) is False
    assert (tmp_path / ".gitignore").read_text().count(".enguru/") == 1


def test_gitignore_appends_to_existing_without_newline(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules")
    assert ensure_gitignore(tmp_path) is True
    assert (tmp_path / ".gitignore").read_text() == "node_modules\n.enguru/\n"
