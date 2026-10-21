"""Shared helpers for the model-component evals (run_analyst/verifier/grader/holdfirm).

- build_seeded_repo(dest): materialise evals/seeded-repo/app as a real git repo with the
  history in evals/seeded-repo/COMMITS.json. PLANTED.json, COMMITS.json and history/ are
  never copied into it, so the agents under test cannot read the labels.
- claude(...): one `claude -p` call with a timeout and one retry when `accept` rejects it.
  `--plugin-dir` points at a temp copy of the plugin without evals/, e2e/, docs/, tests/
  and VCS/state folders, so the models under test cannot read datasets or labels.
- pmap(...): run work items concurrently.
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEEDED = ROOT / "evals" / "seeded-repo"
RESULTS = ROOT / "evals" / "results"
ENGURU = ROOT / "scripts" / "enguru.py"
MODEL = "sonnet"
WORKERS = 6

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Dana Lee", "GIT_AUTHOR_EMAIL": "dana@example.com",
    "GIT_COMMITTER_NAME": "Dana Lee", "GIT_COMMITTER_EMAIL": "dana@example.com",
}


PLUGIN_EXCLUDE = {"evals", "e2e", "docs", "tests", ".git", ".superpowers", ".enguru",
                  "__pycache__", ".pytest_cache"}
_plugin = {"dir": None}
_plugin_lock = threading.Lock()


def plugin_dir():
    """A temp copy of the plugin (created once per process) without PLUGIN_EXCLUDE."""
    with _plugin_lock:
        if _plugin["dir"] is None:
            tmp = tempfile.mkdtemp(prefix="enguru-plugin-")
            atexit.register(shutil.rmtree, tmp, True)
            dest = Path(tmp) / "enguru"
            shutil.copytree(ROOT, dest, ignore=lambda d, names: [
                n for n in names if n in PLUGIN_EXCLUDE or n.endswith(".pyc")])
            _plugin["dir"] = dest
        return _plugin["dir"]


def git(repo, *args, env=None):
    e = dict(os.environ)
    e.update(_GIT_ENV)
    e.update(env or {})
    return subprocess.run(["git", *args], cwd=str(repo), env=e, check=True,
                          capture_output=True, text=True).stdout


def build_seeded_repo(dest):
    """Create the seeded repo at dest (must not exist or be empty) and return its Path."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    git(dest, "init", "-q", "-b", "main")
    commits = json.loads((SEEDED / "COMMITS.json").read_text(encoding="utf-8"))
    for i, c in enumerate(commits):
        for f in c["files"]:
            path, src = (f, "app/" + f) if isinstance(f, str) else (f["path"], f["from"])
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SEEDED / src, target)
            git(dest, "add", path)
        date = "2026-09-%02dT10:00:00Z" % (1 + 2 * i)
        git(dest, "commit", "-q", "-m", c["message"],
            env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    # The final tree must equal app/ exactly, so PLANTED.json line numbers hold.
    for p in (SEEDED / "app").rglob("*"):
        if p.is_file():
            rel = p.relative_to(SEEDED / "app")
            assert (dest / rel).read_bytes() == p.read_bytes(), "seeded tree mismatch: %s" % rel
    return dest


def load_planted():
    planted = json.loads((SEEDED / "PLANTED.json").read_text(encoding="utf-8"))
    for p in planted:  # self-check: each planted range really holds its anchor line
        lines = (SEEDED / "app" / p["file"]).read_text(encoding="utf-8").splitlines()
        a, b = p["lines"]
        assert p["anchor"] in "\n".join(lines[a - 1:b]), "PLANTED anchor missing: %s" % p["id"]
    return planted


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def claude(prompt, cwd, tools, timeout, accept=None, extra=(), model=MODEL):
    """Run `claude -p` once, retrying once on timeout, non-zero exit or accept() -> False.

    accept(stdout) returns a truthy value to keep the run. Returns (stdout, ok)."""
    cmd = ["claude", "-p", "--plugin-dir", str(plugin_dir()), "--allowedTools", tools,
           "--model", model, *extra, prompt]
    out = ""
    for _attempt in range(2):
        try:
            proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                                  timeout=timeout)
            out = proc.stdout
            if proc.returncode != 0:
                out = out + proc.stderr[-300:]
                continue
        except subprocess.TimeoutExpired:
            out = "timeout"
            continue
        if accept is None or accept(out):
            return out, True
    return out, False


def enguru(repo, *args, stdin=None):
    proc = subprocess.run([sys.executable, str(ENGURU), *args], cwd=str(repo), input=stdin,
                          capture_output=True, text=True)
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {"error": "bad_output", "message": (proc.stdout + proc.stderr)[-300:]}


def pmap(fn, items, workers=WORKERS):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


def emit(name, result, ok):
    """Print the metrics object, save it to evals/results/<name>.json, return the exit code."""
    text = json.dumps(result)
    print(text)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / ("%s.json" % name)).write_text(text + "\n", encoding="utf-8")
    if not ok:
        print("targets missed", file=sys.stderr)
    return 0 if ok else 1


def last_json_line(text):
    """The last line of text that parses as a JSON object, or None."""
    for line in reversed(text.strip().splitlines()):
        line = line.strip().strip("`").strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None
