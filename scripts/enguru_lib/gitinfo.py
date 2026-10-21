"""Git facts for enguru: history, churn, dependency changes, diff hunks, file map."""
import fnmatch
import os
import re
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .redact import is_env_file

MANIFESTS = {"package.json", "requirements.txt", "pyproject.toml", "Pipfile",
             "go.mod", "Cargo.toml", "Gemfile", "pom.xml", "build.gradle",
             "composer.json"}

GENERATED_LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                       "poetry.lock", "Cargo.lock", "Gemfile.lock", "go.sum",
                       "composer.lock"}
GENERATED_PATTERNS = ("*.min.js", "*.min.css", "*.map")
GENERATED = sorted(GENERATED_LOCKFILES) + list(GENERATED_PATTERNS)

_ENTRY_BASENAMES = {"main.py", "app.py", "manage.py", "index.js", "index.ts",
                    "server.js", "server.ts"}
_ENTRY_GLOBS = ("src/main.*", "cmd/*/main.go", "src/main.rs")
_WALK_SKIP = {"node_modules", "venv", ".venv", "dist", "build"}


def _git(repo, *args, check=True):
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=off", *args],
        cwd=str(repo), capture_output=True, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (
            " ".join(args), proc.stderr.decode("utf-8", "replace").strip()))
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def is_git_repo(repo) -> bool:
    try:
        code, out = _git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    except OSError:
        return False
    return code == 0 and out.strip() == "true"


def resolve_default_base(repo):
    """Merge-base sha of HEAD with origin/HEAD's target, main or master."""
    candidates = []
    code, out = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD",
                     check=False)
    if code == 0 and out.strip():
        candidates.append(out.strip())
    candidates += ["main", "master"]
    for cand in candidates:
        code, out = _git(repo, "merge-base", "HEAD", cand, check=False)
        sha = out.strip()
        if code == 0 and sha:
            return sha
    return None


def range_id(repo, base, head) -> str:
    return "%s..%s" % (str(base)[:7], str(head)[:7])


def worktree_range_id(now) -> str:
    return "worktree-" + now.strftime("%Y%m%d%H%M%S")


@dataclass
class Commit:
    sha: str
    date: str
    subject: str
    files: list = field(default_factory=list)  # (path, added, deleted)
    is_revert: bool = False


_NUMSTAT = re.compile(r"^(\d+|-)\t(\d+|-)\t(.+)$")
_BRACE_RENAME = re.compile(r"\{([^{}]*) => ([^{}]*)\}")


def _numstat_path(p):
    """Resolve rename notations ('a => b', 'd/{a => b}/f') to the new path."""
    if " => " not in p:
        return p
    if "{" in p:
        p = _BRACE_RENAME.sub(lambda m: m.group(2), p)
        return p.replace("//", "/")
    return p.split(" => ", 1)[1]


def commit_log(repo, rev_range=None, limit=500):
    args = ["log", "--numstat", "-n", str(limit),
            "--format=%x1e%H%x1f%aI%x1f%s%x1f%b"]
    if rev_range:
        args.append(rev_range)
    code, out = _git(repo, *args, check=False)
    if code != 0:
        return []
    commits = []
    for rec in out.split("\x1e"):
        if not rec.strip():
            continue
        parts = rec.split("\x1f", 3)
        if len(parts) < 4:
            continue
        sha, date, subject, rest = parts
        files = []
        for line in rest.splitlines():
            m = _NUMSTAT.match(line)
            if m and m.group(1) != "-":
                files.append((_numstat_path(m.group(3)),
                              int(m.group(1)), int(m.group(2))))
        is_revert = subject.startswith('Revert "') or "This reverts commit" in rest
        commits.append(Commit(sha.strip(), date, subject, files, is_revert))
    return commits


def churn_files(commits, top=15):
    totals = {}
    for c in commits:
        for path, added, deleted in c.files:
            totals[path] = totals.get(path, 0) + added + deleted
    return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:top]


def dependency_history(commits):
    out = []
    for c in commits:
        for path, _a, _d in c.files:
            if PurePosixPath(path).name in MANIFESTS:
                out.append({"sha": c.sha, "date": c.date, "file": path})
    return out


@dataclass
class Hunk:
    id: str
    path: str
    old_start: int
    new_start: int
    lines: list = field(default_factory=list)


def _is_generated(path):
    name = PurePosixPath(path).name
    return name in GENERATED_LOCKFILES or any(
        fnmatch.fnmatch(name, p) for p in GENERATED_PATTERNS)


_HUNK_HDR = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # SHA-1 fallback


def empty_tree(repo) -> str:
    """The empty tree's id in the repo's hash format (SHA-256 repos differ)."""
    try:
        code, out = _git(repo, "hash-object", "-t", "tree", "/dev/null", check=False)
    except OSError:
        return EMPTY_TREE
    out = out.strip()
    return out if code == 0 and out else EMPTY_TREE
UNTRACKED_MAX_LINES = 500
UNTRACKED_MAX_BYTES = 1024 * 1024


def has_commits(repo) -> bool:
    code, _ = _git(repo, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
    return code == 0


def _diff_range_args(repo, base, head, uncommitted):
    if uncommitted:
        # Worktree against HEAD; with no commits yet, against the empty tree.
        return ["HEAD" if has_commits(repo) else empty_tree(repo)]
    return ["%s..%s" % (base, head or "HEAD")]


def _numstat_entries(repo, rng):
    """[(path, is_binary)] using -z; renames yield the new path."""
    _, out = _git(repo, "diff", "--numstat", "-z", "-M", *rng)
    toks = out.split("\0")
    entries, i = [], 0
    while i < len(toks):
        t = toks[i]
        i += 1
        if not t:
            continue
        added, deleted, path = t.split("\t", 2)
        if path == "":  # rename: old and new follow as separate tokens
            path = toks[i + 1]
            i += 2
        entries.append((path, added == "-" and deleted == "-"))
    return entries


def _strip_prefix(p):
    p = p.split("\t")[0]
    return p[2:] if p.startswith(("a/", "b/")) else p


def _excluded_path(path):
    return _is_generated(path) or is_env_file(path)


def _untracked_hunks(repo, start, excluded):
    """Non-ignored untracked files as all-added hunks (binary, generated, env and
    oversized files are excluded; long files are cut to UNTRACKED_MAX_LINES)."""
    _, out = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    hunks = []
    for path in sorted(p for p in out.split("\0") if p):
        if path.startswith(".enguru/"):
            continue
        full = os.path.join(str(repo), path)
        try:
            if not stat.S_ISREG(os.lstat(full).st_mode):
                continue  # symlinks, sockets, fifos, directories
            size = os.path.getsize(full)
            with open(full, "rb") as fh:
                data = b"" if size > UNTRACKED_MAX_BYTES else fh.read()
        except OSError:
            continue
        if _excluded_path(path) or size > UNTRACKED_MAX_BYTES or b"\0" in data[:8000]:
            if path not in excluded:
                excluded.append(path)
            continue
        lines = data.decode("utf-8", "replace").splitlines()
        if not lines:
            continue
        body = ["+" + line for line in lines[:UNTRACKED_MAX_LINES]]
        if len(lines) > UNTRACKED_MAX_LINES:
            body.append("… [%d more lines truncated]" % (len(lines) - UNTRACKED_MAX_LINES))
        hunks.append(Hunk("h-%d" % (start + len(hunks) + 1), path, 0, 1, body))
    return hunks


def diff_hunks(repo, base, head, uncommitted):
    rng = _diff_range_args(repo, base, head, uncommitted)
    excluded = []
    for path, binary in _numstat_entries(repo, rng):
        if binary or _excluded_path(path):
            if path not in excluded:
                excluded.append(path)
    _, out = _git(repo, "diff", "-U3", "--no-color", "--no-ext-diff", "-M",
                    "--src-prefix=a/", "--dst-prefix=b/", *rng)
    hunks = []
    old_path = new_path = None
    cur = None
    in_hunk = False  # only "diff --git" ends a hunk body; "--- x" may be content
    for line in out.split("\n"):
        if line.startswith("diff --git "):
            cur = None
            in_hunk = False
            old_path = new_path = None
        elif in_hunk and not line.startswith("@@"):
            if cur is not None and line[:1] in ("+", "-", " ", "\\"):
                cur.lines.append(line)
        elif line.startswith("--- "):
            old_path = None if line[4:].startswith("/dev/null") else _strip_prefix(line[4:])
        elif line.startswith("+++ "):
            new_path = None if line[4:].startswith("/dev/null") else _strip_prefix(line[4:])
        elif line.startswith("@@"):
            in_hunk = True
            m = _HUNK_HDR.match(line)
            path = new_path or old_path
            if not m or path is None:
                cur = None
                continue
            if path in excluded:
                cur = None
                continue
            cur = Hunk("h-%d" % (len(hunks) + 1), path,
                       int(m.group(1)), int(m.group(2)), [])
            hunks.append(cur)
    if uncommitted:
        hunks += _untracked_hunks(repo, len(hunks), excluded)
    return hunks, excluded


def _list_files(repo):
    if is_git_repo(repo):
        code, out = _git(repo, "ls-files", "-z", check=False)
        if code == 0:
            return [p for p in out.split("\0") if p]
    paths = []
    root = str(repo)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d not in _WALK_SKIP]
        for f in filenames:
            rel = os.path.relpath(os.path.join(dirpath, f), root)
            paths.append(rel.replace(os.sep, "/"))
    return paths


def file_map(repo):
    files, stack, entry = [], set(), []
    for rel in sorted(_list_files(repo)):
        if is_env_file(rel):
            continue
        try:
            size = os.path.getsize(os.path.join(str(repo), rel))
        except OSError:
            continue  # tracked but missing from the worktree
        files.append({"path": rel, "size": size})
        name = PurePosixPath(rel).name
        if name in MANIFESTS:
            stack.add(name)
        if name in _ENTRY_BASENAMES or any(
                fnmatch.fnmatchcase(rel, g) for g in _ENTRY_GLOBS):
            entry.append(rel)
    return {"files": files, "stack": sorted(stack), "entry_points": entry}
