"""Digest prep: transcript chunks and rule-based structural events."""
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import blast, gitinfo, paths, transcripts
from .redact import redact

CHUNK_TURNS = 40
TEXT_CAP = 1500
TRUNC_MARK = " …[truncated]"
PARAPHRASE_CAP = 200
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

_TEST_RUNNER = re.compile(
    r"\b(pytest|npm (?:run )?test|jest|vitest|go test|cargo test|rspec|mvn test|gradle test)\b")
_DEP_CMD = re.compile(
    r"\b(npm (?:i|install)|yarn add|pnpm add|pip install|poetry add|cargo add|go get|bundle add)\b")


class DigestError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Scope:
    kind: str
    base: Optional[str] = None
    head: Optional[str] = None
    uncommitted: bool = False
    range_id: Optional[str] = None


def _rev_parse(repo, ref):
    try:
        _, out = gitinfo._git(repo, "rev-parse", "--verify", ref + "^{commit}")
    except RuntimeError as e:
        raise DigestError("no_base", str(e))
    return out.strip()


def resolve_scope(repo, project, rng, uncommitted, now=None):
    """Build a Scope from CLI choices; raises DigestError."""
    if project:
        return Scope("project")
    if not gitinfo.is_git_repo(repo):
        raise DigestError("not_git", "Diff mode needs a git repository.")
    if uncommitted:
        now = now or datetime.now()
        return Scope("diff", None, None, True, gitinfo.worktree_range_id(now))
    if rng:
        if ".." not in rng:
            raise DigestError("no_base", "Range must look like A..B.")
        a, b = rng.split("..", 1)
        base, head = _rev_parse(repo, a), _rev_parse(repo, b or "HEAD")
    else:
        base = gitinfo.resolve_default_base(repo)
        if base is None:
            raise DigestError("no_base", "No base branch found; use --range A..B or --uncommitted.")
        head = _rev_parse(repo, "HEAD")
    return Scope("diff", base, head, False, gitinfo.range_id(repo, base, head))


def _cap(text):
    text = redact(text)
    return text[:TEXT_CAP] + TRUNC_MARK if len(text) > TEXT_CAP else text


def _para(text):
    return text[:PARAPHRASE_CAP]


def _is_edit(turn):
    return any(tu.name in EDIT_TOOLS for tu in turn.tool_uses)


def _candidate(turns, i):
    t = turns[i]
    if t.role == "user":
        return bool(t.text.strip())
    if t.role == "assistant":
        return bool(t.text.strip()) and any(_is_edit(x) for x in turns[i:i + 3])
    if t.role == "tool":
        return bool(t.tool_errors)
    return False


def _parse_ts(s):
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _event(label, paraphrase, files, session_id, ts, source):
    return {"label": label, "paraphrase": _para(paraphrase), "files": sorted(files),
            "session_id": session_id, "timestamp": ts, "source": source,
            "origin": "rules"}


def _session_events(session, repo):
    turns = session.turns
    events = []
    # Churn: file edited 3+ times
    by_file = {}
    for t in turns:
        for f in transcripts.edited_files(t, repo):
            by_file.setdefault(f, []).append(t)
    for f, ts in sorted(by_file.items()):
        if len(ts) >= 3:
            events.append(_event("churn", "%s edited %d times" % (f, len(ts)), [f],
                                 session.id, ts[-1].timestamp,
                                 {"turn_ids": [t.id for t in ts]}))
    for i, t in enumerate(turns):
        # Failing test run followed by an edit
        if t.role == "tool" and t.tool_errors:
            runner = None
            for prev in reversed(turns[:i]):
                if prev.role == "assistant":
                    for tu in prev.tool_uses:
                        m = tu.name == "Bash" and _TEST_RUNNER.search(str(tu.input.get("command", "")))
                        if m:
                            runner = m.group(1)
                    break
            if runner:
                for nxt in turns[i + 1:i + 4]:
                    if _is_edit(nxt):
                        events.append(_event(
                            "churn", "fix after failing %s" % runner,
                            transcripts.edited_files(nxt, repo), session.id,
                            nxt.timestamp, {"turn_ids": [t.id, nxt.id]}))
                        break
        # Dependency installs
        if t.role == "assistant":
            for tu in t.tool_uses:
                cmd = str(tu.input.get("command", "")) if tu.name == "Bash" else ""
                if _DEP_CMD.search(cmd):
                    events.append(_event("dependency", "dependency command: " + redact(cmd.strip()),
                                         [], session.id, t.timestamp, {"turn_ids": [t.id]}))
    return events


def _git_events(commits):
    events = []
    for c in commits:
        if c.is_revert:
            events.append(_event("revert", "revert: " + redact(c.subject),
                                 [p for p, _a, _d in c.files], None, c.date,
                                 {"commit": c.sha}))
    for d in gitinfo.dependency_history(commits):
        events.append(_event("dependency", "dependency manifest %s changed" % d["file"],
                             [d["file"]], None, d["date"], {"commit": d["sha"]}))
    return events


def _reset_dir(p):
    shutil.rmtree(p, ignore_errors=True)
    p.mkdir(parents=True, exist_ok=True)


def prep(repo, scope, root):
    repo = Path(repo)
    is_git = gitinfo.is_git_repo(repo)
    diff = None
    changed_paths = None
    commits = []
    window = None
    if scope.kind == "diff":
        if not is_git:
            raise DigestError("not_git", "Diff mode needs a git repository.")
        try:
            hunks, excluded = gitinfo.diff_hunks(repo, scope.base, scope.head, scope.uncommitted)
            if not hunks:
                raise DigestError("empty_diff", "No changes to digest in this range.")
            changed_paths = {h.path for h in hunks}
            if not scope.uncommitted:
                commits = gitinfo.commit_log(repo, "%s..%s" % (scope.base, scope.head))
            candidates = blast.blast_radius(repo, blast.changed_symbols(hunks), changed_paths)
        except RuntimeError as e:
            raise DigestError("no_base", str(e))
        diff = {"hunks": [asdict(h) for h in hunks], "excluded": excluded,
                "blast_radius_candidates": candidates}
        dates = [d for d in (_parse_ts(c.date) for c in commits) if d]
        if dates:
            window = (min(dates) - timedelta(hours=1), max(dates) + timedelta(hours=1))
    elif is_git:
        commits = gitinfo.commit_log(repo)

    sd = paths.state_dir(repo)
    paths.ensure_gitignore(repo)
    work = sd / "work"
    chunk_dir, out_dir = work / "chunks", work / "extracted"
    _reset_dir(chunk_dir)
    _reset_dir(out_dir)
    cache_dir = sd / "cache" / "extractor"

    sessions, skipped = transcripts.load_sessions(repo, root)
    pending, cached, chunk_index, events = [], [], [], []
    used = 0
    for s in sessions:
        if changed_paths is not None:
            overlap = any(transcripts.edited_files(t, repo) & changed_paths for t in s.turns)
            in_window = window is not None and any(
                (d := _parse_ts(t.timestamp)) and window[0] <= d <= window[1]
                for t in s.turns)
            if not (overlap or in_window):
                continue
        used += 1
        events.extend(_session_events(s, repo))
        cand = [t for i, t in enumerate(s.turns) if _candidate(s.turns, i)]
        for k in range(0, (len(cand) + CHUNK_TURNS - 1) // CHUNK_TURNS):
            part = cand[k * CHUNK_TURNS:(k + 1) * CHUNK_TURNS]
            cid = "%s__%d" % (s.id, k)
            is_cached = (cache_dir / ("%s__%d.json" % (s.content_hash, k))).exists()
            chunk_index.append({"chunk_id": cid, "content_hash": s.content_hash, "k": k,
                                "session_id": s.id, "turn_ids": [t.id for t in part],
                                "cached": is_cached})
            if is_cached:
                cached.append(cid)
                continue
            chunk = {"chunk_id": cid, "session_id": s.id, "turns": [
                {"id": t.id, "role": t.role, "text": _cap(t.text),
                 "files": sorted(transcripts.edited_files(t, repo)),
                 "errors": [redact(e) for e in t.tool_errors]} for t in part]}
            cpath = chunk_dir / (cid + ".json")
            paths.atomic_write_json(cpath, chunk)
            pending.append({"chunk_id": cid, "chunk_path": str(cpath),
                            "out_path": str(out_dir / (cid + ".json"))})

    events.extend(_git_events(commits))
    git = {}
    if is_git:
        git = {"commits": len(commits), "churn_files": [list(x) for x in gitinfo.churn_files(commits)],
               "dependency_history": gitinfo.dependency_history(commits),
               "reverts": [c.sha for c in commits if c.is_revert]}
    paths.atomic_write_json(work / "rules.json", {
        "scope": asdict(scope), "events": events, "git": git,
        "file_map": gitinfo.file_map(repo), "diff": diff,
        "sessions_found": len(sessions), "sessions_used": used,
        "skipped_lines": skipped})
    paths.atomic_write_json(work / "prep.json", {"chunks": chunk_index})
    return {"pending": pending, "cached": cached, "skipped_lines": skipped}
