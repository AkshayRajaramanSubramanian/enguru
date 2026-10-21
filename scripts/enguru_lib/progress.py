"""Sessions, next-item selection, answer recording and keylog."""
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from . import bank, gitinfo, paths, rubric, schema

QUICK_CONCEPTS = 5
THOROUGH_CONCEPTS = 12
THOROUGH_QUESTIONS = 2
REVISIT_PER_SESSION = 2
MAX_FOLLOWUPS = 3
DIFFICULTIES = ["foundational", "intermediate", "advanced"]

DIGEST_REL = ".enguru/digest.json"
KEYLOG_REL = ".enguru/keylog.jsonl"
TOPIC_REL = ".enguru/banks/project/topic-map.json"

_RISKY = re.compile(r"(?i)(auth|login|session|token|password|payment|billing|invoice|charge|"
                    r"stripe|checkout|migrat|schema|permission|admin|crypto|secret)")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_CERT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ProgressError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def recommend_intensity(changed_paths) -> str:
    if len(changed_paths) >= 10 or any(_RISKY.search(p) for p in changed_paths):
        return "thorough"
    return "quick"


# --------------------------------------------------------------------- state

def _empty():
    return {"version": 1, "sessions": [], "answers": [], "concepts": {}, "revisit": [],
            "findings": {}, "current": None}


def _progress_path(repo):
    return Path(repo) / paths.STATE_DIRNAME / "progress.json"


def load(repo) -> dict:
    p = _progress_path(repo)
    if not p.exists():
        return _empty()
    try:
        data = paths.read_json(p)
    except (paths.StateCorrupt, OSError) as e:
        raise ProgressError("state_corrupt", str(e))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ProgressError("state_corrupt", "%s has an unexpected shape" % p)
    for k, v in _empty().items():
        data.setdefault(k, v)
    return data


def _save(repo, prog):
    paths.state_dir(repo)
    paths.atomic_write_json(_progress_path(repo), prog)


def _concept(prog, cid):
    return prog["concepts"].setdefault(
        cid, {"status": "open", "stale": False, "asked_question_ids": []})


def _concept_status(prog, cid):
    return prog["concepts"].get(cid, {}).get("status", "open")


def _finding_state(prog, cid):
    return prog["findings"].get(cid, {"status": "open", "rung": 1})


def _remove_revisit(prog, cid):
    prog["revisit"] = [x for x in prog["revisit"] if x != cid]


def _add_revisit(prog, cid):
    if cid not in prog["revisit"]:
        prog["revisit"].append(cid)


def _retire(prog, cid):
    c = _concept(prog, cid)
    c["status"] = "retired"
    c["stale"] = False
    _remove_revisit(prog, cid)


def _current_session(prog):
    cur = prog.get("current")
    if not cur:
        return None
    for s in prog["sessions"]:
        if s["id"] == cur["session_id"]:
            return s
    return None


def _require_session(repo, prog):
    """Return the current session and refresh its lock heartbeat."""
    s = _current_session(prog)
    if s is None:
        raise ProgressError("state_corrupt", "No active session; run `session start` first.")
    paths.touch_lock(paths.state_dir(repo), s["id"])
    return s


def current_worktree_range_id(repo):
    """The current diff session's worktree range id, or None."""
    try:
        s = _current_session(load(repo))
    except ProgressError:
        return None
    if s and s["scope"] == "diff" and str(s.get("range_id") or "").startswith("worktree-"):
        return s["range_id"]
    return None


# --------------------------------------------------------------------- banks

def _diff_rel(range_id):
    return ".enguru/banks/diff/%s.json" % range_id


def _module_rel(module_id):
    return ".enguru/banks/project/%s.json" % module_id


def _read_bank(repo, rel):
    p = Path(repo) / rel
    if not p.exists():
        return None
    try:
        data = paths.read_json(p)
    except (paths.StateCorrupt, OSError) as e:
        raise ProgressError("state_corrupt", str(e))
    if not isinstance(data, dict):
        raise ProgressError("state_corrupt", "%s is not a JSON object" % rel)
    return data


def _load_scope(repo, session):
    """Return (topic_map|None, {module_id: bank}, [(rel, bank)]) for the session's scope."""
    if session["scope"] == "project":
        tm = _read_bank(repo, TOPIC_REL)
        module_banks, banks = {}, []
        for m in (tm or {}).get("modules", []):
            mid = m.get("id")
            if not isinstance(mid, str) or not _SAFE_NAME.match(mid):
                continue
            b = _read_bank(repo, _module_rel(mid))
            if b is not None:
                module_banks[mid] = b
                banks.append((_module_rel(mid), b))
        return tm, module_banks, banks
    rel = _diff_rel(session["range_id"])
    b = _read_bank(repo, rel)
    return None, {}, ([(rel, b)] if b is not None else [])


def _index(tm, banks):
    concepts, questions, findings, finding_bank = {}, {}, [], {}
    for m in (tm or {}).get("modules", []):
        for c in m.get("concepts", []):
            concepts[c["id"]] = {"concept": c, "bank": TOPIC_REL}
    for rel, b in banks:
        for c in b.get("concepts", []):
            concepts[c["id"]] = {"concept": c, "bank": rel}
        for q in b.get("questions", []):
            questions.setdefault(q["concept_id"], []).append(q)
        for f in b.get("findings", []):
            findings.append(f)
            finding_bank[f["concept_id"]] = rel
    return {"concepts": concepts, "questions": questions, "findings": findings,
            "finding_bank": finding_bank}


def _confirmed(idx):
    return [f for f in idx["findings"] if f.get("verdict") == "confirmed"]


def _concept_done(prog, cid):
    if _concept_status(prog, cid) in ("solid", "retired"):
        return True
    f = prog["findings"].get(cid)
    return bool(f) and f["status"] in ("completed", "fixed")


def _module_concept_ids(tm, module_banks, mid):
    """Concept ids of a module: topic-map order, then any extras from its bank."""
    tm_ids = [c["id"] for m in tm["modules"] if m.get("id") == mid for c in m.get("concepts", [])]
    b = module_banks.get(mid)
    if b is None:
        return tm_ids
    bank_ids = [c["id"] for c in b.get("concepts", [])]
    return [c for c in tm_ids if c in bank_ids] + [c for c in bank_ids if c not in tm_ids]


def _pick_module(prog, tm, module_banks):
    if not tm:
        return None
    for m in tm.get("modules", []):
        if any(not _concept_done(prog, cid)
               for cid in _module_concept_ids(tm, module_banks, m.get("id"))):
            return m.get("id")
    return None


def _mark_staleness(repo, prog, idx):
    stale, retired = bank.stale_concepts(repo, [e["concept"] for e in idx["concepts"].values()])
    for cid, st in prog["concepts"].items():
        if cid in idx["concepts"]:
            st["stale"] = cid in stale
    for cid in stale:
        _concept(prog, cid)["stale"] = True
    for cid in retired:
        _retire(prog, cid)
    for f in _confirmed(idx):
        cid = f["concept_id"]
        st = dict(_finding_state(prog, cid))
        if cid in stale and st["status"] in ("open", "completed"):
            st["prev_status"] = st["status"]
            st["status"] = "recheck"
            prog["findings"][cid] = st


# ------------------------------------------------------------------ sessions

def start_session(repo, scope, intensity, now=None, takeover=False) -> dict:
    repo = Path(repo)
    paths.ensure_gitignore(repo)
    sd = paths.state_dir(repo)
    sid = "s%d-%s" % (len(load(repo)["sessions"]) + 1, secrets.token_hex(3))
    paths.acquire_lock(sd, session_id=sid, force=takeover)
    try:
        return _start(repo, scope, intensity, now or _now(), sid)
    except BaseException:
        paths.release_lock(sd, sid)
        raise


def _start(repo, scope, intensity, now, sid):
    recommended = None
    if scope.kind == "diff":
        try:
            hunks, _ = gitinfo.diff_hunks(repo, scope.base, scope.head, scope.uncommitted)
        except RuntimeError as e:
            raise ProgressError("no_base", str(e))
        if not hunks:
            raise ProgressError("empty_diff", "No changes to interview on in this range.")
        if intensity is None:
            recommended = recommend_intensity(sorted({h.path for h in hunks}))
    else:
        intensity = None
    prog = load(repo)
    session = {"id": sid, "scope": scope.kind,
               "range_id": scope.range_id if scope.kind == "diff" else None,
               "intensity": intensity, "module_id": None, "started": now, "ended": None}
    tm, module_banks, banks = _load_scope(repo, session)
    idx = _index(tm, banks)
    _mark_staleness(repo, prog, idx)
    session["module_id"] = _pick_module(prog, tm, module_banks)
    prog["sessions"].append(session)
    prog["current"] = {"session_id": session["id"], "served_concepts": [],
                       "served_questions": {}, "revisit_served": [], "pending": None,
                       "rebuilds": {}, "skipped": {}, "revisit_at_start": list(prog["revisit"])}
    _save(repo, prog)
    out = {"session_id": session["id"], "scope": scope.kind, "range_id": session["range_id"],
           "intensity": intensity}
    if recommended is not None:
        out["recommend_intensity"] = recommended
    out["open_findings"] = sum(
        1 for f in _confirmed(idx)
        if _finding_state(prog, f["concept_id"])["status"] in ("open", "in_progress", "recheck"))
    out["revisit_count"] = len(prog["revisit"])
    out["module_id"] = session["module_id"]
    return out


def set_intensity(repo, intensity) -> dict:
    if intensity not in ("quick", "thorough"):
        raise ProgressError("state_corrupt", "intensity must be quick or thorough")
    prog = load(repo)
    session = _require_session(repo, prog)
    if session["scope"] != "diff":
        raise ProgressError("state_corrupt", "intensity applies to diff sessions only")
    session["intensity"] = intensity
    _save(repo, prog)
    return {"ok": True, "intensity": intensity}


def _latest_certificate_date(repo):
    d = Path(repo) / paths.STATE_DIRNAME / "certificates"
    dates = [p.stem for p in d.glob("*.json") if _CERT_NAME.match(p.stem)] if d.is_dir() else []
    return max(dates) if dates else None


def end_session(repo, now=None) -> dict:
    repo = Path(repo)
    prog = load(repo)
    session = _current_session(prog) or (prog["sessions"][-1] if prog["sessions"] else None)
    sid = session["id"] if session else None
    solid = []
    for a in prog["answers"]:
        cid = a["concept_id"]
        if (a.get("session_id") == sid and a["kind"] == "answer"
                and _concept_status(prog, cid) == "solid" and cid not in solid):
            solid.append(cid)
    undisclosed = []
    if session:
        tm, _, banks = _load_scope(repo, session)
        idx = _index(tm, banks)
        for f in _confirmed(idx):
            cid = f["concept_id"]
            if (f.get("severity") == "critical"
                    and _finding_state(prog, cid)["status"] in ("open", "in_progress")):
                c = idx["concepts"].get(cid, {}).get("concept", {})
                undisclosed.append({"concept_id": cid, "title": c.get("title", ""),
                                    "file": f["lines"]["file"]})
    since = _latest_certificate_date(repo)
    scored = sum(1 for a in prog["answers"]
                 if a["kind"] == "answer" and a.get("outcome") != "skipped"
                 and (since is None or a["timestamp"][:10] > since))
    current = prog["current"]
    if current and session:
        session["ended"] = now or _now()
    prog["current"] = None
    _save(repo, prog)
    if current:
        paths.release_lock(paths.state_dir(repo), current["session_id"])
    return {"solid": solid, "revisit": list(prog["revisit"]),
            "undisclosed_critical": undisclosed, "scored_since_last_certificate": scored,
            "suggest_certificate": scored >= rubric.load_config()["suggest_certificate_min"]}


# ----------------------------------------------------------------- next item

def _build_action(action, out_rel, **extra):
    out = {"action": action}
    out.update(extra)
    out.update({"out_path": out_rel, "digest_path": DIGEST_REL, "keylog_path": KEYLOG_REL})
    return out


def _rebuild(repo, prog, idx, cid, reason):
    """Return a rebuild action, or None (and skip the concept for the session)
    when this session already asked for one and it did not help."""
    cur = prog["current"]
    entry = idx["concepts"][cid]
    bank_path = entry["bank"]
    if bank_path == TOPIC_REL:  # only in the topic map: merge into its module bank
        module = entry["concept"].get("module")
        bank_path = _module_rel(module) if isinstance(module, str) and _SAFE_NAME.match(module) else None
    if bank_path is None or cur["rebuilds"].get(cid, 0) >= 1:
        cur["skipped"][cid] = "rebuild_failed:" + reason if bank_path else "no_module_bank"
        _save(repo, prog)
        return None
    cur["rebuilds"][cid] = cur["rebuilds"].get(cid, 0) + 1
    _save(repo, prog)
    return {"action": "rebuild_concept", "concept_id": cid, "reason": reason,
            "bank_path": bank_path,
            "out_path": ".enguru/work/revisit-%s.json" % cid,
            "asked_question_ids": list(prog["concepts"].get(cid, {}).get("asked_question_ids", [])),
            "digest_path": DIGEST_REL, "keylog_path": KEYLOG_REL}


def _pick_question(prog, idx, cid):
    asked = set(prog["concepts"].get(cid, {}).get("asked_question_ids", []))
    qs = [q for q in idx["questions"].get(cid, []) if q["id"] not in asked]
    if not qs:
        return None
    failed = any(a["kind"] == "answer" and a["concept_id"] == cid
                 and a.get("outcome") in ("incorrect", "dont_know") for a in prog["answers"])
    start = 0 if failed else 1

    def key(item):
        i, q = item
        d = DIFFICULTIES.index(q["difficulty"]) if q.get("difficulty") in DIFFICULTIES else 1
        return (abs(d - start), d, i)

    return min(enumerate(qs), key=key)[1]


def _serve(repo, prog, idx, cid, revisit=False, allow_rebuild=True):
    """Return an ask/rebuild action for a concept, or None to skip it."""
    if cid in prog["current"]["skipped"]:
        return None
    stale, retired = bank.stale_concepts(repo, [idx["concepts"][cid]["concept"]])
    if retired:
        _retire(prog, cid)
        _save(repo, prog)
        return None
    if stale:
        return _rebuild(repo, prog, idx, cid, "stale")
    q = _pick_question(prog, idx, cid)
    if q is None:
        return _rebuild(repo, prog, idx, cid, "revisit") if allow_rebuild else None
    cur = prog["current"]
    cur["pending"] = {"concept_id": cid, "question_id": q["id"]}
    if cid not in cur["served_concepts"]:
        cur["served_concepts"].append(cid)
    cur["served_questions"][cid] = cur["served_questions"].get(cid, 0) + 1
    if revisit:
        cur["revisit_served"].append(cid)
    _concept(prog, cid)["asked_question_ids"].append(q["id"])
    _save(repo, prog)
    return {"action": "ask", "question": q}


def _finding_answered_in(prog, cid, sid):
    return any(a["kind"] == "answer" and a.get("question_kind") == "finding"
               and a["concept_id"] == cid and a.get("session_id") == sid
               for a in prog["answers"])


def _next_ladder(prog, idx, sid):
    best = None
    for order, f in enumerate(_confirmed(idx)):
        cid = f["concept_id"]
        st = _finding_state(prog, cid)
        if (_concept_status(prog, cid) == "retired" or st["status"] not in ("open", "in_progress")
                or _finding_answered_in(prog, cid, sid)):
            continue
        key = (0 if st["status"] == "in_progress" else 1,
               0 if f.get("severity") == "critical" else 1, order)
        if best is None or key < best[0]:
            best = (key, f, st)
    if best is None:
        return None
    _, f, st = best
    n = len(f["ladder"])
    rung = min(max(int(st.get("rung") or 1), 1), n)
    return {"action": "ladder", "concept_id": f["concept_id"], "rung": rung,
            "step": f["ladder"][rung - 1], "ladder_length": n, "severity": f["severity"],
            "announce": rung == 1 and st["status"] == "open", "finding": f}


def _open_finding_concepts(prog, idx):
    return {f["concept_id"] for f in _confirmed(idx)
            if _finding_state(prog, f["concept_id"])["status"] in ("open", "in_progress", "recheck")}


def next_item(repo) -> dict:
    repo = Path(repo)
    prog = load(repo)
    session = _require_session(repo, prog)
    cur = prog["current"]
    for k in ("rebuilds", "skipped"):
        cur.setdefault(k, {})
    if session["scope"] == "project":
        if _read_bank(repo, TOPIC_REL) is None:
            return _build_action("build_topic_map", TOPIC_REL)
    elif _read_bank(repo, _diff_rel(session["range_id"])) is None:
        return _build_action("build_diff_bank", _diff_rel(session["range_id"]),
                             range_id=session["range_id"])
    tm, module_banks, banks = _load_scope(repo, session)
    for rel, b in banks:
        pending = [f["concept_id"] for f in b.get("findings", []) if f.get("verdict") == "pending"]
        if pending:
            return {"action": "verify_findings", "bank_path": rel, "concept_ids": pending}
    idx = _index(tm, banks)

    pend = cur.get("pending")
    if pend:
        for q in idx["questions"].get(pend["concept_id"], []):
            if q["id"] == pend["question_id"]:
                return {"action": "ask", "question": q}
        cur["pending"] = None

    for f in _confirmed(idx):
        cid = f["concept_id"]
        if _finding_state(prog, cid)["status"] == "recheck" and _concept_status(prog, cid) != "retired":
            return {"action": "verify_fix", "concept_id": cid, "finding": f,
                    "bank_path": idx["finding_bank"][cid],
                    "out_path": ".enguru/work/verdicts/fixcheck-%s.json" % cid}

    ladder = _next_ladder(prog, idx, session["id"])
    if ladder:
        if ladder["concept_id"] not in cur["served_concepts"]:
            cur["served_concepts"].append(ladder["concept_id"])
            _save(repo, prog)
        return ladder

    if len(cur["revisit_served"]) < REVISIT_PER_SESSION:
        busy = _open_finding_concepts(prog, idx)
        for cid in list(prog["revisit"]):
            if (cid in cur["served_concepts"] or cid not in idx["concepts"] or cid in busy
                    or _concept_status(prog, cid) == "retired"):
                continue
            item = _serve(repo, prog, idx, cid, revisit=True)
            if item:
                return item

    if session["scope"] == "project":
        return _next_project(repo, prog, session, idx, tm, module_banks)
    return _next_diff(repo, prog, session, idx, banks[0][1])


def _held_for_revisit(prog, cid):
    """A concept queued for revisit when the session started is only served through
    the capped revisit step (concepts sent to revisit during the session keep their slot)."""
    cur = prog["current"]
    return cid in cur.get("revisit_at_start", []) and cid not in cur["revisit_served"]


def _next_project(repo, prog, session, idx, tm, module_banks):
    cur = prog["current"]
    if session["module_id"] is None:
        session["module_id"] = _pick_module(prog, tm, module_banks)
        if session["module_id"] is None:
            return {"action": "done", "uncovered": []}
        _save(repo, prog)
    mid = session["module_id"]
    if mid not in module_banks:
        return _build_action("build_module", _module_rel(mid), module_id=mid)
    for cid in _module_concept_ids(tm, module_banks, mid):
        entry = idx["concepts"].get(cid)
        if (entry is None or entry["concept"].get("kind") != "quiz"
                or _concept_status(prog, cid) in ("solid", "retired")
                or cid in cur["served_concepts"] or _held_for_revisit(prog, cid)):
            continue
        item = _serve(repo, prog, idx, cid)
        if item:
            return item
    return {"action": "done", "uncovered": []}


def _next_diff(repo, prog, session, idx, diff_bank):
    cur = prog["current"]
    thorough = session.get("intensity") == "thorough"
    limit = THOROUGH_CONCEPTS if thorough else QUICK_CONCEPTS
    per_concept = THOROUGH_QUESTIONS if thorough else 1
    ordered = sorted(enumerate(diff_bank.get("concepts", [])),
                     key=lambda ic: (ic[1].get("risk_tier", 3), ic[0]))
    candidates = []
    for _, c in ordered:
        cid, status = c["id"], _concept_status(prog, c["id"])
        if (c.get("kind") != "quiz" or status == "retired" or cid in cur["revisit_served"]
                or _held_for_revisit(prog, cid)
                or (status == "solid" and cid not in cur["served_concepts"])):
            continue
        candidates.append(cid)
    window, uncovered = candidates[:limit], candidates[limit:]
    for cid in window:
        served = cur["served_questions"].get(cid, 0)
        if served == 0:
            item = _serve(repo, prog, idx, cid)
        elif served < per_concept and _concept_status(prog, cid) != "solid":
            item = _serve(repo, prog, idx, cid, allow_rebuild=False)
        else:
            continue
        if item:
            return item
    return {"action": "done", "uncovered": uncovered}


# -------------------------------------------------------------------- record

def _kind_rules(rec):
    errs = []
    kind = rec["kind"]
    if kind == "answer":
        for k in ("question_kind", "outcome"):
            if k not in rec:
                errs.append("$: answer record needs '%s'" % k)
    elif kind == "ladder_progress":
        if not isinstance(rec.get("ladder_rung_reached"), int):
            errs.append("$: ladder_progress record needs integer 'ladder_rung_reached'")
    elif kind == "fix_check" and "fix_verdict" not in rec:
        errs.append("$: fix_check record needs 'fix_verdict'")
    return errs


def record(repo, rec) -> dict:
    if not isinstance(rec, dict):
        raise ProgressError("state_corrupt", "invalid record: $: expected an object")
    errs = schema.validate(rec, schema.load_schema("record"))
    if not errs:
        errs = _kind_rules(rec)
    if errs:
        raise ProgressError("state_corrupt", "invalid record: " + "; ".join(errs))
    prog = load(repo)
    session = _require_session(repo, prog)
    if rec["session_id"] != session["id"]:
        raise ProgressError("state_corrupt", "record is for session %s but the current session is %s"
                            % (rec["session_id"], session["id"]))
    cid, kind = rec["concept_id"], rec["kind"]
    if kind == "fix_check" and _finding_state(prog, cid)["status"] != "recheck":
        raise ProgressError("state_corrupt", "fix_check for %s, which is not awaiting a recheck" % cid)
    prog["answers"].append(rec)
    if kind == "ladder_progress":
        st = prog["findings"].setdefault(cid, {"status": "open", "rung": 1})
        st["status"] = "in_progress"
        st["rung"] = rec["ladder_rung_reached"]
        result = st["status"]
    elif kind == "fix_check":
        st = prog["findings"].setdefault(cid, {"status": "open", "rung": 1})
        prev = st.pop("prev_status", None) or "open"
        st["status"] = "fixed" if rec["fix_verdict"] == "fixed" else prev
        result = st["status"]
    elif rec["question_kind"] == "finding" and rec["outcome"] == "skipped":
        # A skipped ladder keeps its status and rung; answered-this-session keeps it
        # from being served again until a later session.
        _add_revisit(prog, cid)
        result = _finding_state(prog, cid)["status"]
    elif rec["question_kind"] == "finding":
        st = prog["findings"].setdefault(cid, {"status": "open", "rung": 1})
        if rec.get("revealed"):
            st.update(status="open", rung=1)
            _add_revisit(prog, cid)
        else:
            st["status"] = "completed"
            _remove_revisit(prog, cid)
        result = st["status"]
    else:
        c = _concept(prog, cid)
        if rec["outcome"] == "correct":
            c["status"] = "solid"
            _remove_revisit(prog, cid)
        elif rec["outcome"] == "skipped" or rec.get("followups_used", 0) >= MAX_FOLLOWUPS:
            c["status"] = "needs_revisit"
            _add_revisit(prog, cid)
        result = c["status"]
    cur = prog.get("current")
    if (cur and cur.get("pending") and kind == "answer"
            and cid == cur["pending"]["concept_id"]):
        cur["pending"] = None
    _save(repo, prog)
    return {"ok": True, "concept_status": result}


# -------------------------------------------------------------- status/keylog

def status(repo) -> dict:
    prog = load(repo)
    counts = {}
    for st in prog["concepts"].values():
        counts[st["status"]] = counts.get(st["status"], 0) + 1
    cur = prog.get("current")
    return {"sessions": len(prog["sessions"]), "answers": len(prog["answers"]),
            "concepts": counts, "revisit": list(prog["revisit"]),
            "findings": {cid: {"status": f["status"], "rung": f.get("rung", 1)}
                         for cid, f in prog["findings"].items()},
            "current_session": cur["session_id"] if cur else None}


def keylog_add(repo, entry) -> None:
    keys = ("question_id", "issue", "evidence")
    if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in keys):
        raise ProgressError("state_corrupt", "keylog entry needs string question_id, issue, evidence")
    line = {k: entry[k] for k in keys}
    line["timestamp"] = _now()
    with open(paths.state_dir(repo) / "keylog.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
