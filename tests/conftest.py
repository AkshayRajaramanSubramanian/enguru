import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

CLI = ROOT / "scripts" / "enguru.py"


def run_cli_raw(args, cwd, stdin=None):
    """Run the CLI; return (exit_code, parsed_stdout_json)."""
    proc = subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=str(cwd),
        input=stdin,
        capture_output=True,
        text=True,
    )
    return proc.returncode, json.loads(proc.stdout)


def run_cli(args, cwd, stdin=None):
    """Run the CLI, assert exit 0, return parsed JSON."""
    code, out = run_cli_raw(args, cwd, stdin)
    assert code == 0, out
    return out


import pytest  # noqa: E402

_UUID_COUNTER = [0]


def _next_uuid():
    _UUID_COUNTER[0] += 1
    return "uuid-%04d" % _UUID_COUNTER[0]


@pytest.fixture
def repo(tmp_path):
    """A plain directory the tests treat as the repo."""
    r = tmp_path / "repo"
    r.mkdir()
    return r


def _entry(kind, content, cwd, ts, uuid, session_id, sidechain):
    return {
        "type": kind,
        "uuid": uuid or _next_uuid(),
        "parentUuid": None,
        "sessionId": session_id,
        "timestamp": ts,
        "cwd": str(cwd),
        "isSidechain": sidechain,
        "message": {"role": kind, "content": content},
    }


def user(text_or_blocks, cwd="/repo", ts="2026-10-01T12:00:00.000Z",
         uuid=None, session_id="s1", sidechain=False):
    return _entry("user", text_or_blocks, cwd, ts, uuid, session_id, sidechain)


def assistant(blocks, cwd="/repo", ts="2026-10-01T12:00:05.000Z",
              uuid=None, session_id="s1", sidechain=False):
    return _entry("assistant", blocks, cwd, ts, uuid, session_id, sidechain)


def tool_result(tool_use_id, content, is_error=False, cwd="/repo",
                ts="2026-10-01T12:00:10.000Z", uuid=None, session_id="s1",
                sidechain=False):
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return _entry("user", [block], cwd, ts, uuid, session_id, sidechain)


def write_transcript(root, project_dirname, session_id, entries, extra_bytes=b""):
    """Write entries as JSONL to <root>/<project_dirname>/<session_id>.jsonl."""
    d = Path(root) / project_dirname
    d.mkdir(parents=True, exist_ok=True)
    p = d / (session_id + ".jsonl")
    data = b"".join(json.dumps(e).encode("utf-8") + b"\n" for e in entries)
    p.write_bytes(data + extra_bytes)
    return p


def _git_for(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True)


def make_repo(tmp_path, commits, messages):
    """Real git repo (branch main). commits: list of {path: str|bytes|None}
    (None deletes the file); messages: one commit message per commit."""
    r = Path(tmp_path) / "gitrepo"
    r.mkdir()
    _git_for(r, "init", "-b", "main")
    _git_for(r, "config", "user.name", "Test User")
    _git_for(r, "config", "user.email", "test@example.com")
    _git_for(r, "config", "commit.gpgsign", "false")
    for files, msg in zip(commits, messages):
        for rel, content in files.items():
            p = r / rel
            if content is None:
                if p.exists():
                    p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                p.write_bytes(content)
            else:
                p.write_text(content, encoding="utf-8")
        _git_for(r, "add", "-A")
        _git_for(r, "commit", "-q", "--allow-empty", "-m", msg)
    return r
