"""Digest prep: transcript chunks and rule-based structural events."""
import json
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import blast, gitinfo, paths, schema, transcripts
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


LIMITS_PATH = Path(__file__).resolve().parents[2] / "config" / "limits.json"


def load_limits():
    """Extractor fan-out limits: {max_sessions, max_chunks}."""
    with open(LIMITS_PATH, encoding="utf-8") as f:
        return json.load(f)["digest"]


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


_RANGE_HINT = "Give a range like A..B, for example main..HEAD, or use --uncommitted."


def _rev_parse(repo, ref):
    if not ref or ref.startswith("-"):
        raise DigestError("no_base", "The range needs a start and an end. " + _RANGE_HINT)
    try:
        _, out = gitinfo._git(repo, "rev-parse", "--verify", "-q", ref + "^{commit}")
    except RuntimeError:
        raise DigestError("no_base", "'%s' is not a commit in this repository. %s"
                          % (ref, _RANGE_HINT))
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
            raise DigestError("no_base", "The range must look like A..B. " + _RANGE_HINT)
        a, b = rng.split("..", 1)
        base, head = _rev_parse(repo, a), _rev_parse(repo, b or "HEAD")
    else:
        base = gitinfo.resolve_default_base(repo)
        if base is None:
            raise DigestError("no_base", "No base branch (main or master) found. " + _RANGE_HINT)
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
        for h in hunks:
            h.lines = [redact(line) for line in h.lines]
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
    relevant = []
    for s in sessions:
        if transcripts.is_enguru_session(s):
            continue
        if changed_paths is not None:
            overlap = any(transcripts.edited_files(t, repo) & changed_paths for t in s.turns)
            in_window = window is not None and any(
                (d := _parse_ts(t.timestamp)) and window[0] <= d <= window[1]
                for t in s.turns)
            if not (overlap or in_window):
                continue
        relevant.append(s)
    # Most recent sessions first; cap sessions and the chunks sent to the extractor.
    limits = load_limits()
    relevant.sort(key=lambda s: max((_ts_key(t.timestamp) for t in s.turns), default=_MIN_TS),
                  reverse=True)
    truncation = []
    if len(relevant) > limits["max_sessions"]:
        truncation.append("sessions:capped:%d/%d" % (limits["max_sessions"], len(relevant)))
        relevant = relevant[:limits["max_sessions"]]
    pending, cached, chunk_index, events = [], [], [], []
    used, to_extract, over_cap = 0, 0, 0
    for s in relevant:
        used += 1
        events.extend(_session_events(s, repo))
        cand = [t for i, t in enumerate(s.turns) if _candidate(s.turns, i)]
        for k in range(0, (len(cand) + CHUNK_TURNS - 1) // CHUNK_TURNS):
            part = cand[k * CHUNK_TURNS:(k + 1) * CHUNK_TURNS]
            cid = "%s__%d" % (s.id, k)
            is_cached = (cache_dir / ("%s__%d.json" % (s.content_hash, k))).exists()
            if not is_cached:
                if to_extract >= limits["max_chunks"]:
                    over_cap += 1
                    continue
                to_extract += 1
            chunk_index.append({"chunk_id": cid, "content_hash": s.content_hash, "k": k,
                                "session_id": s.id, "turn_ids": [t.id for t in part],
                                "turn_timestamps": {t.id: t.timestamp for t in part},
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
    if over_cap:
        truncation.append("chunks:capped:%d/%d" % (to_extract, to_extract + over_cap))

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
        "skipped_lines": skipped, "truncation": truncation})
    paths.atomic_write_json(work / "prep.json", {"chunks": chunk_index})
    return {"pending": pending, "cached": cached, "skipped_lines": skipped}


# ---------------------------------------------------------------- merge half

_PUSHBACK = re.compile(
    r"(?i)\b(no|don'?t|instead|revert|undo|wrong|broke|broken|that'?s not|stop)\b")
_EXCERPT_STAGES = ((300, "excerpts:300"), (150, "excerpts:150"))
_MIN_TS = datetime.min.replace(tzinfo=timezone.utc)


def _ts_key(ts):
    return (_parse_ts(ts) if ts else None) or _MIN_TS


def keyword_events(chunk, timestamps=None):
    """Fallback events from keywords: first user turn, then pushback turns."""
    timestamps = timestamps or {}
    turns = chunk["turns"]
    sid = chunk.get("session_id")
    events = []
    seen_request = False
    for i, t in enumerate(turns):
        if t["role"] != "user" or not t["text"].strip():
            continue
        ts = timestamps.get(t["id"], "")
        if not seen_request:
            seen_request = True
            events.append({"label": "request", "paraphrase": _para(t["text"]), "files": [],
                           "session_id": sid, "timestamp": ts,
                           "source": {"turn_ids": [t["id"]]}, "origin": "keywords"})
            continue
        files = None
        if i > 0 and turns[i - 1]["role"] != "user":
            prev = turns[i - 1]
            if prev["files"]:
                files = prev["files"]
            elif prev["role"] == "tool" and i > 1 and turns[i - 2]["files"]:
                files = turns[i - 2]["files"]
        if files and _PUSHBACK.search(t["text"]):
            events.append({"label": "pushback", "paraphrase": _para(t["text"]),
                           "files": sorted(files), "session_id": sid, "timestamp": ts,
                           "source": {"turn_ids": [t["id"]]}, "origin": "keywords"})
    return events


def _size(digest):
    return len(json.dumps(digest)) / 4


# Structural parts (file map, diff) are capped first, to these shares of the budget,
# so the events still have room on medium and large repos.
FILE_MAP_SHARE = 0.25
DIFF_SHARE = 0.35
HUNK_LINE_STAGES = (60, 20, 8)
BLAST_CANDIDATES_CAP = 100


def _json_tokens(obj):
    return len(json.dumps(obj)) / 4


def _file_counts(files):
    by_dir, by_ext = {}, {}
    for f in files:
        p = f["path"]
        d = p.split("/", 1)[0] + "/" if "/" in p else "./"
        name = p.rsplit("/", 1)[-1]
        ext = "." + name.rsplit(".", 1)[1] if "." in name.lstrip(".") else "(none)"
        by_dir[d] = by_dir.get(d, 0) + 1
        by_ext[ext] = by_ext.get(ext, 0) + 1
    return {"by_dir": by_dir, "by_ext": by_ext}


def _cap_file_map(digest, budget, trunc):
    """Keep entry points, then the most churned, then the largest files; summarise the
    rest as counts by top-level directory and extension."""
    fm = digest.get("file_map")
    if not isinstance(fm, dict) or not isinstance(fm.get("files"), list):
        return
    if _json_tokens(fm) <= budget:
        return
    files = fm["files"]
    entry = set(fm.get("entry_points") or [])
    churn = {}
    for i, item in enumerate((digest.get("git") or {}).get("churn_files") or []):
        if isinstance(item, (list, tuple)) and item:
            churn.setdefault(item[0], i)

    def rank(f):
        p = f.get("path")
        return (0 if p in entry else 1 if p in churn else 2,
                churn.get(p, 0), -(f.get("size") or 0), str(p))

    ordered = sorted(files, key=rank)
    # Size of the kept list grows monotonically: one pass to find how many fit.
    base = dict(fm, files=[], file_counts=_file_counts(files))
    room = (budget - _json_tokens(base)) * 4
    n = 0
    for f in ordered:
        room -= len(json.dumps(f)) + 2
        if room < 0:
            break
        n += 1
    kept, rest = ordered[:n], ordered[n:]
    kept.sort(key=lambda f: str(f.get("path")))
    fm["files"] = kept
    fm["file_counts"] = _file_counts(rest)
    trunc.append("file_map:capped:%d/%d" % (len(kept), len(files)))


def _cap_diff(digest, budget, trunc):
    """Cap blast-radius candidates, then shorten long hunks, then drop trailing hunks."""
    diff = digest.get("diff")
    if not isinstance(diff, dict) or _json_tokens(diff) <= budget:
        return
    cands = diff.get("blast_radius_candidates") or []
    if len(cands) > BLAST_CANDIDATES_CAP:
        diff["blast_radius_candidates"] = cands[:BLAST_CANDIDATES_CAP]
        trunc.append("blast:capped:%d/%d" % (BLAST_CANDIDATES_CAP, len(cands)))
        if _json_tokens(diff) <= budget:
            return
    hunks = diff.get("hunks") or []
    for limit in HUNK_LINE_STAGES:
        cut = 0
        for h in hunks:
            lines = h.get("lines") or []
            if len(lines) > limit + 1:
                extra = len(lines) - limit
                if lines[-1].startswith("…"):  # already truncated at a looser stage
                    extra += int(re.search(r"\d+", lines[-1]).group()) - 1
                h["lines"] = lines[:limit] + ["… [%d more lines truncated]" % extra]
                cut += 1
        if cut:
            trunc.append("hunks:lines:%d" % limit)
        if _json_tokens(diff) <= budget:
            return
    total = len(hunks)
    room = (budget - _json_tokens(dict(diff, hunks=[]))) * 4
    n = 0
    for h in hunks:
        room -= len(json.dumps(h)) + 2
        if room < 0:
            break
        n += 1
    diff["hunks"] = hunks[:n]
    trunc.append("hunks:dropped:%d/%d" % (total - n, total))


def apply_budget(digest, cap_tokens=20000):
    """Trim the digest until it fits, recording each stage used in meta.truncation."""
    meta = digest.setdefault("meta", {})
    trunc = meta.setdefault("truncation", [])

    def finish():
        if "events_kept" in meta:
            meta["events_kept"] = len(digest["events"])
        return digest

    if _size(digest) <= cap_tokens:
        return finish()
    _cap_file_map(digest, cap_tokens * FILE_MAP_SHARE, trunc)
    _cap_diff(digest, cap_tokens * DIFF_SHARE, trunc)
    if _size(digest) <= cap_tokens:
        return finish()
    for limit, name in _EXCERPT_STAGES:
        trunc.append(name)
        for e in digest["events"]:
            e["paraphrase"] = e["paraphrase"][:limit]
        if _size(digest) <= cap_tokens:
            return finish()
    trunc.append("drop:request")
    digest["events"] = [e for e in digest["events"] if e["label"] != "request"]
    if _size(digest) <= cap_tokens:
        return finish()
    # Stage 4 removes at most half of what is left; beyond that, summarise (stage 5).
    # One pass: drop oldest events until the size estimate fits (at most half).
    events = digest["events"]
    order = sorted(range(len(events)), key=lambda i: _ts_key(events[i]["timestamp"]))
    excess = (_size(digest) - cap_tokens) * 4
    dropped = set()
    for i in order[:len(events) // 2]:
        if excess <= 0:
            break
        dropped.add(i)
        excess -= len(json.dumps(events[i])) + 2
    digest["events"] = [e for j, e in enumerate(events) if j not in dropped]
    if dropped:
        trunc.append("drop:oldest:%d" % len(dropped))
    if _size(digest) <= cap_tokens:
        return finish()
    counts = {}
    for e in digest["events"]:
        counts[e["label"]] = counts.get(e["label"], 0) + 1
    meta["event_counts"] = counts
    digest["events"] = []
    trunc.append("events:counts")
    return finish()


def _event_schema():
    ev = dict(schema.load_schema("digest")["properties"]["events"]["items"])
    return ev


def _valid_cache(data, turn_ids):
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        return None
    es = _event_schema()
    for e in data["events"]:
        if not isinstance(e, dict) or schema.validate(dict(e, id="x"), es):
            return None
        if not set(e["source"].get("turn_ids", [])) <= set(turn_ids):
            return None
    return data["events"]


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _convert(ev, chunk_meta):
    stamps = [chunk_meta.get("turn_timestamps", {}).get(t, "") for t in ev["source_turn_ids"]]
    stamps = [x for x in stamps if x]
    ts = min(stamps, key=_ts_key) if stamps else ""
    return {"label": ev["label"], "paraphrase": _para(ev["paraphrase"]), "files": ev["files"],
            "session_id": chunk_meta["session_id"], "timestamp": ts,
            "source": {"turn_ids": list(ev["source_turn_ids"])}, "origin": "haiku"}


def merge(repo):
    repo = Path(repo)
    sd = paths.state_dir(repo)
    work = sd / "work"
    try:
        prep_data = paths.read_json(work / "prep.json")
        rules = paths.read_json(work / "rules.json")
    except FileNotFoundError:
        raise DigestError("state_corrupt", "No digest prep found; run digest prep first.")
    except paths.StateCorrupt as e:
        raise DigestError("state_corrupt", "Prep state unreadable (%s); run digest prep again." % e)
    cache_dir = sd / "cache" / "extractor"
    ext_schema = schema.load_schema("extracted")
    chunk_schema = schema.load_schema("chunk")

    events, fallback, dropped = list(rules["events"]), [], 0
    for cm in prep_data["chunks"]:
        cid = cm["chunk_id"]
        turn_ids = set(cm["turn_ids"])
        cache_path = cache_dir / ("%s__%d.json" % (cm["content_hash"], cm["k"]))
        if cm["cached"]:
            cached = _valid_cache(_read_json(cache_path), cm["turn_ids"])
            if cached is None:
                fallback.append(cid)
                try:
                    cache_path.unlink()
                except OSError:
                    pass
            else:
                events.extend(cached)
            continue
        raw = _read_json(work / "extracted" / (cid + ".json"))
        if isinstance(raw, dict) and not schema.validate(raw, ext_schema):
            good = []
            for ev in raw["events"]:
                if set(ev["source_turn_ids"]) <= turn_ids:
                    good.append(_convert(ev, cm))
                else:
                    dropped += 1
            cache_dir.mkdir(parents=True, exist_ok=True)
            paths.atomic_write_json(cache_path, {"chunk_id": cid, "events": good})
            events.extend(good)
            continue
        fallback.append(cid)
        chunk = _read_json(work / "chunks" / (cid + ".json"))
        if isinstance(chunk, dict) and not schema.validate(chunk, chunk_schema):
            events.extend(keyword_events(chunk, cm.get("turn_timestamps")))

    events.sort(key=lambda e: _ts_key(e["timestamp"]))
    for i, e in enumerate(events, 1):
        e["id"] = "ev-%d" % i
        e["source"] = dict(e["source"])
    scope = rules["scope"]
    diff = rules.get("diff")
    result = {
        "version": 1,
        "meta": {"scope": scope["kind"], "range_id": scope.get("range_id"),
                 "sessions_found": rules["sessions_found"], "sessions_used": rules["sessions_used"],
                 "events_kept": len(events), "events_dropped": dropped,
                 "fallback_chunks": fallback, "skipped_lines": rules["skipped_lines"],
                 "truncation": list(rules.get("truncation", []))},
        "file_map": rules["file_map"], "git": rules["git"], "events": events, "diff": diff}
    apply_budget(result)
    errors = schema.validate(result, schema.load_schema("digest"))
    if errors:
        raise DigestError("state_corrupt", "Merged digest failed validation: %s" % errors[0])
    paths.atomic_write_json(sd / "digest.json", result)
    m = result["meta"]
    return {"ok": True, "events_kept": m["events_kept"], "events_dropped": dropped,
            "fallback_chunks": fallback, "truncation": m["truncation"]}
