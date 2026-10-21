"""Sessions, next-item selection, answer recording and keylog."""
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from . import bank, gitinfo, paths, rubric, schema, selection

QUICK_CONCEPTS = 5
THOROUGH_CONCEPTS = 12
THOROUGH_QUESTIONS = 2
REVISIT_PER_SESSION = 2
MAX_FOLLOWUPS = 3
# Finding statuses whose code change sends the finding to a fix check first.
RECHECKABLE = ("open", "completed", "in_progress", "revisit")
# Finding statuses that mean the developer already worked on it, most advanced first.
WORKED = ("fixed", "completed", "revisit", "in_progress")

DIGEST_REL = ".enguru/digest.json"
KEYLOG_REL = ".enguru/keylog.jsonl"
TOPIC_REL = ".enguru/banks/project/topic-map.json"

_RISKY = re.compile(r"(?i)(auth|login|session|token|password|payment|billing|invoice|charge|"
                    r"stripe|checkout|migrat|schema|permission|admin|crypto|secret)")
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
            if not paths.safe_id(mid):
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
    """Done for module advancement only: solid, retired, or parked in the revisit queue
    (a failed deep question comes back through the warm-up revisits, across modules).
    A finding mirroring another (same_as) in revisit is done too: it is out of the queue
    and comes back through the finding it mirrors."""
    if _concept_status(prog, cid) in ("solid", "retired", "needs_revisit") or cid in prog["revisit"]:
        return True
    f = prog["findings"].get(cid)
    return bool(f) and (f["status"] in ("completed", "fixed")
                        or (bool(f.get("same_as")) and f["status"] == "revisit"))


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
        if cid in stale and st["status"] in RECHECKABLE and not st.get("same_as"):
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
    _link_duplicates(repo, prog)
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
        if _finding_state(prog, f["concept_id"])["status"] in ("open", "in_progress", "recheck")
        and not _is_duplicate(prog, f["concept_id"]))
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


def _counts_since_certificate(repo):
    """A predicate on answer timestamps: newer than the latest certificate. Uses the
    certificate's saved_at when it has one, else the end of its date."""
    d = Path(repo) / paths.STATE_DIRNAME / "certificates"
    dates = [p.stem for p in d.glob("*.json") if _CERT_NAME.match(p.stem)] if d.is_dir() else []
    if not dates:
        return lambda ts: True
    last = max(dates)
    try:
        saved_at = paths.read_json(d / (last + ".json")).get("saved_at")
    except (paths.StateCorrupt, OSError, ValueError, AttributeError):
        saved_at = None
    if isinstance(saved_at, str) and saved_at:
        return lambda ts: ts > saved_at
    return lambda ts: ts[:10] > last


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
    explained = {a["concept_id"] for a in prog["answers"]
                 if a.get("session_id") == sid and a["kind"] == "answer"
                 and a.get("question_kind") == "finding" and a.get("revealed")
                 and a.get("outcome") != "skipped"}
    if session:
        # The session's own scope, plus findings from any bank that this session served
        # or recorded, or that stopped partway in another scope: the findings phase comes
        # last, so a session can end before it resumes them (spec 9.5).
        tm, _, banks = _load_scope(repo, session)
        touched = set((prog["current"] or {}).get("served_concepts", []))
        touched |= {a["concept_id"] for a in prog["answers"] if a.get("session_id") == sid}
        all_tm, all_banks = _all_banks(repo)
        scope_rels = {rel for rel, _ in banks}

        def carried(f):
            cid = f.get("concept_id")
            return cid in touched or _finding_state(prog, cid)["status"] == "in_progress"

        extra = [(rel, dict(b, findings=[f for f in b.get("findings", []) if carried(f)]))
                 for rel, b in all_banks if rel not in scope_rels]
        idx = _index(tm or all_tm, banks + extra)
        seen = set()
        listed = {f["concept_id"] for f in _confirmed(idx)}
        for f in _confirmed(idx):
            cid = f["concept_id"]
            if cid in seen or _finding_state(prog, cid).get("same_as") in listed:
                continue  # a same-issue mirror is disclosed through the finding it mirrors
            seen.add(cid)
            if (f.get("severity") == "critical" and cid not in explained
                    and not _finding_state(prog, cid).get("explained")
                    and _finding_state(prog, cid)["status"] in ("open", "in_progress")):
                c = idx["concepts"].get(cid, {}).get("concept", {})
                undisclosed.append({"concept_id": cid, "title": c.get("title", ""),
                                    "file": f["lines"]["file"]})
    newer = _counts_since_certificate(repo)
    scored = sum(1 for a in prog["answers"]
                 if a["kind"] == "answer" and a.get("outcome") != "skipped"
                 and newer(a["timestamp"]))
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

def _build_action(action, bank_rel, work_name, **extra):
    """The analyst writes to a work file (out_path); the skill promotes it to
    bank_path only after it validates and is sealed."""
    out = {"action": action}
    out.update(extra)
    out.update({"out_path": ".enguru/work/build-%s.json" % work_name, "bank_path": bank_rel,
                "digest_path": DIGEST_REL, "keylog_path": KEYLOG_REL})
    return out


def _rebuild(repo, prog, idx, cid, reason):
    """Return a rebuild action, or None (and skip the concept for the session)
    when this session already asked for one and it did not help."""
    cur = prog["current"]
    entry = idx["concepts"][cid]
    bank_path = entry["bank"]
    if bank_path == TOPIC_REL:  # only in the topic map: merge into its module bank
        module = entry["concept"].get("module")
        bank_path = _module_rel(module) if paths.safe_id(module) else None
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
            "phase": selection.rebuild_phase(prog["concepts"].get(cid, {}),
                                             idx["questions"].get(cid, [])),
            "digest_path": DIGEST_REL, "keylog_path": KEYLOG_REL}


def _serve(repo, prog, idx, cid, phase=None, only_phase=None, revisit=False,
           allow_rebuild=True):
    """Return an ask/rebuild action for a concept, or None to skip it. With `phase`, ask
    only if the concept's next question is in that phase."""
    if cid in prog["current"]["skipped"]:
        return None
    stale, retired = bank.stale_concepts(repo, [idx["concepts"][cid]["concept"]])
    if retired:
        _retire(prog, cid)
        _save(repo, prog)
        return None
    if stale:
        return _rebuild(repo, prog, idx, cid, "stale")
    state, qs = prog["concepts"].get(cid, {}), idx["questions"].get(cid, [])
    plan = selection.pick(state, qs, only_phase)
    if plan is None and only_phase is None and prog["current"]["rebuilds"].get(cid, 0) >= 1:
        plan = selection.below_floor(state, qs)  # the rebuild added nothing deeper
    if plan is None:
        return _rebuild(repo, prog, idx, cid, "revisit") if allow_rebuild else None
    q_phase, q = plan
    if phase is not None and q_phase != phase:
        return None
    cur = prog["current"]
    cur["pending"] = {"concept_id": cid, "question_id": q["id"]}
    if cid not in cur["served_concepts"]:
        cur["served_concepts"].append(cid)
    cur["served_questions"][cid] = cur["served_questions"].get(cid, 0) + 1
    cur["served_phases"].setdefault(cid, []).append(q_phase)
    if revisit:
        cur["revisit_served"].append(cid)
    _concept(prog, cid)["asked_question_ids"].append(q["id"])
    _save(repo, prog)
    return {"action": "ask", "question": q}


def _session_gate(prog, cid, sid):
    """None if the concept has not been served this session; else (passed, phase) of its
    last question here: passed when that answer was correct or partial."""
    phases = prog["current"]["served_phases"].get(cid)
    if not phases:
        return None
    a = selection.last_quiz_answer(prog["answers"], cid, sid)
    if a is None:
        return False, phases[-1]
    return a.get("outcome") in selection.PASSED, selection.phase_of(a.get("style"))


def _finding_answered_in(prog, cid, sid):
    return any(a["kind"] == "answer" and a.get("question_kind") == "finding"
               and a["concept_id"] == cid and a.get("session_id") == sid
               for a in prog["answers"])


def _next_ladder(prog, idx, sid):
    """The next ladder to serve. A finding mirroring another (same_as) is never served,
    nor one whose same-issue mate in this scope was already answered this session."""
    mates = {}
    for group in finding_groups([(idx["finding_bank"][f["concept_id"]], f)
                                 for f in _confirmed(idx)]):
        for cid in group:
            mates[cid] = group
    best = None
    for order, f in enumerate(_confirmed(idx)):
        cid = f["concept_id"]
        st = _finding_state(prog, cid)
        if (_concept_status(prog, cid) == "retired" or st["status"] not in ("open", "in_progress")
                or st.get("same_as")
                or any(_finding_answered_in(prog, m, sid) for m in mates.get(cid, [cid]))):
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


def _all_banks(repo, tolerant=False):
    """[(rel, bank)] for every bank on disk: project module banks and diff banks.
    tolerant skips unreadable banks instead of raising."""
    tm = _read_bank(repo, TOPIC_REL)
    rels = [_module_rel(m.get("id")) for m in (tm or {}).get("modules", [])
            if paths.safe_id(m.get("id"))]
    diff_dir = Path(repo) / ".enguru" / "banks" / "diff"
    if diff_dir.is_dir():
        rels += [".enguru/banks/diff/%s" % p.name for p in sorted(diff_dir.glob("*.json"))]
    out = []
    for rel in rels:
        try:
            b = _read_bank(repo, rel)
        except ProgressError:
            if not tolerant:
                raise
            continue
        if b is not None:
            out.append((rel, b))
    return tm, out


# ------------------------------------------------- same issue in two banks (8.5)

def _span(f):
    ln = f.get("lines")
    if not isinstance(ln, dict) or not isinstance(ln.get("file"), str):
        return None
    s, e = ln.get("start"), ln.get("end")
    if not isinstance(s, int) or not isinstance(e, int):
        return None
    return ln["file"], min(s, e), max(s, e)


def _same_issue(a, b):
    (rel_a, fa, sa), (rel_b, fb, sb) = a, b
    return (rel_a != rel_b and sa is not None and sb is not None
            and fa.get("severity") == fb.get("severity") and sa[0] == sb[0]
            and sa[1] <= sb[2] and sb[1] <= sa[2])


def finding_groups(bank_findings):
    """Group confirmed findings from [(bank_rel, finding)] that are one issue: same file,
    overlapping lines, same severity, from different banks (spec 8.5). Returns lists of
    concept ids in input order; singletons included."""
    items, seen = [], set()
    for rel, f in bank_findings:
        cid = f.get("concept_id")
        if f.get("verdict") == "confirmed" and cid and cid not in seen:
            seen.add(cid)
            items.append((rel, f, _span(f)))
    group_of = list(range(len(items)))
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if group_of[i] != group_of[j] and _same_issue(items[i], items[j]):
                old = group_of[j]
                group_of = [group_of[i] if g == old else g for g in group_of]
    groups = {}
    for g, (_, f, _) in zip(group_of, items):
        groups.setdefault(g, []).append(f["concept_id"])
    return list(groups.values())


def _worked_status(st):
    s = st.get("status", "open")
    return (st.get("prev_status") or "open") if s == "recheck" else s


def group_winner(group, states):
    """The finding already worked on that stands for its group, or None when none is.
    Findings mirroring another member never win; among the rest, the most advanced
    status wins (WORKED order), then the earliest."""
    own = [c for c in group if states.get(c, {}).get("same_as") not in group]
    worked = [c for c in own if _worked_status(states.get(c, {})) in WORKED]
    if not worked:
        return None
    return min(worked, key=lambda c: (WORKED.index(_worked_status(states[c])), group.index(c)))


def _link_duplicates(repo, prog):
    """Mirror each worked-on finding's state onto the other findings for the same issue,
    marked same_as, so they are never laddered on their own. A finding no longer in a
    group gets its own state back. Returns whether prog changed."""
    before = json.dumps(prog["findings"], sort_keys=True), list(prog["revisit"])
    _, banks = _all_banks(repo, tolerant=True)
    states = prog["findings"]
    linked = set()
    for group in finding_groups([(rel, f) for rel, b in banks for f in b.get("findings", [])]):
        win = group_winner(group, states) if len(group) > 1 else None
        if win is None:
            continue
        mirror = {k: v for k, v in states[win].items() if k not in ("same_as", "own_state")}
        for cid in group:
            if cid == win:
                continue
            old = states.get(cid)
            own = old.get("own_state") if old and old.get("same_as") else old
            states[cid] = dict(mirror, same_as=win, own_state=own)
            _remove_revisit(prog, cid)
            linked.add(cid)
    for cid, st in list(states.items()):
        if st.get("same_as") and cid not in linked:
            if st.get("own_state"):
                states[cid] = st["own_state"]
            else:
                del states[cid]
    return (json.dumps(prog["findings"], sort_keys=True), list(prog["revisit"])) != before


def _is_duplicate(prog, cid):
    return bool(prog["findings"].get(cid, {}).get("same_as"))


def _resumable(repo, prog, idx, f):
    """Whether an in-progress finding from any bank may be resumed now. A finding whose
    code is gone is retired (or skipped this session if only its own file is gone); one
    whose code changed is marked recheck, so its own scope's verify_fix handles it."""
    cid = f["concept_id"]
    entry = idx["concepts"].get(cid)
    if entry is None or cid in prog["current"]["skipped"]:
        return False
    stale, retired = bank.stale_concepts(repo, [entry["concept"]])
    if retired:
        _retire(prog, cid)
        return False
    if not (Path(repo) / f["lines"]["file"]).is_file():
        prog["current"]["skipped"][cid] = "finding_file_missing"
        return False
    if stale:
        st = dict(_finding_state(prog, cid))
        st["prev_status"], st["status"] = st["status"], "recheck"
        prog["findings"][cid] = st
        return False
    return True


def _resume_ladder(repo, prog, sid):
    """A ladder stopped partway (in_progress) in any scope resumes at its saved rung, first
    in the findings phase."""
    if not any(f.get("status") == "in_progress" for f in prog["findings"].values()):
        return None
    tm, banks = _all_banks(repo)
    idx = _index(tm, banks)
    before = json.dumps(prog, sort_keys=True)
    idx["findings"] = [f for f in idx["findings"]
                       if _finding_state(prog, f["concept_id"])["status"] == "in_progress"
                       and f.get("verdict") == "confirmed"
                       and not _is_duplicate(prog, f["concept_id"])
                       and _resumable(repo, prog, idx, f)]
    if json.dumps(prog, sort_keys=True) != before:
        _save(repo, prog)
    return _next_ladder(prog, idx, sid)


def _revisit_ladder(repo, prog, idx, cid):
    """A finding explained by last resort comes back through the revisit step: its ladder
    from rung 1, without the announcement."""
    if _finding_state(prog, cid)["status"] != "revisit" or _is_duplicate(prog, cid):
        return None
    f = next((f for f in _confirmed(idx) if f["concept_id"] == cid), None)
    if f is None:
        return None
    cur = prog["current"]
    if cid not in cur["revisit_served"]:
        cur["served_concepts"].append(cid)
        cur["revisit_served"].append(cid)
        _save(repo, prog)
    return {"action": "ladder", "concept_id": cid, "rung": 1, "step": f["ladder"][0],
            "ladder_length": len(f["ladder"]), "severity": f["severity"], "announce": False,
            "revisit": True, "finding": f}


def _open_finding_concepts(prog, idx):
    return {f["concept_id"] for f in _confirmed(idx)
            if _finding_state(prog, f["concept_id"])["status"] in ("open", "in_progress", "recheck")}


def next_item(repo) -> dict:
    """Order of work: housekeeping (builds, verification, fix checks), then up to 2 quiz
    revisits as warm-up, then the quiz phase by phase (explain it -> what if -> spot the
    flaw / review a diff), then real findings (ladders), then done."""
    repo = Path(repo)
    prog = load(repo)
    session = _require_session(repo, prog)
    cur = prog["current"]
    for k in ("rebuilds", "skipped", "served_phases"):
        cur.setdefault(k, {})
    cur.setdefault("revisit_ladders", [])
    if _link_duplicates(repo, prog):
        _save(repo, prog)
    if session["scope"] == "project":
        if _read_bank(repo, TOPIC_REL) is None:
            return _build_action("build_topic_map", TOPIC_REL, "topic-map")
    elif _read_bank(repo, _diff_rel(session["range_id"])) is None:
        return _build_action("build_diff_bank", _diff_rel(session["range_id"]),
                             "diff-" + session["range_id"], range_id=session["range_id"])
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
        if (_finding_state(prog, cid)["status"] == "recheck" and not _is_duplicate(prog, cid)
                and _concept_status(prog, cid) != "retired"):
            return {"action": "verify_fix", "concept_id": cid, "finding": f,
                    "bank_path": idx["finding_bank"][cid],
                    "out_path": ".enguru/work/verdicts/fixcheck-%s.json" % cid}

    item = _warm_up(repo, prog, idx)
    if item:
        return item

    uncovered = []
    if session["scope"] == "project":
        item = _next_project(repo, prog, session, idx, tm, module_banks)
    else:
        item, uncovered = _next_diff(repo, prog, session, idx, banks[0][1])
    if item:
        return item
    return _findings_phase(repo, prog, session, idx) or {"action": "done", "uncovered": uncovered}


def _revisit_slots_used(cur):
    reserved = [c for c in cur["revisit_ladders"] if c not in cur["revisit_served"]]
    return len(cur["revisit_served"]) + len(reserved)


def _warm_up(repo, prog, idx):
    """Up to 2 revisit items per session, in queue order. Quiz revisits are served now, as
    warm-up; a finding explained by last resort is held for the findings phase (its slot is
    reserved here)."""
    cur = prog["current"]
    busy = _open_finding_concepts(prog, idx)
    for cid in list(prog["revisit"]):
        if _revisit_slots_used(cur) >= REVISIT_PER_SESSION:
            return None
        if (cid in cur["served_concepts"] or cid in cur["revisit_ladders"]
                or cid not in idx["concepts"] or cid in busy
                or _concept_status(prog, cid) == "retired"):
            continue
        if (_finding_state(prog, cid)["status"] == "revisit"
                and any(f["concept_id"] == cid for f in _confirmed(idx))):
            cur["revisit_ladders"].append(cid)
            _save(repo, prog)
            continue
        item = _serve(repo, prog, idx, cid, revisit=True)
        if item:
            return item
    return None


def _findings_phase(repo, prog, session, idx):
    """Real findings, after the quiz: a ladder stopped partway (any scope) resumes at its
    saved rung first, then this scope's open findings (critical first), then the findings
    held for revisit."""
    cur = prog["current"]
    ladder = _resume_ladder(repo, prog, session["id"]) or _next_ladder(prog, idx, session["id"])
    if ladder:
        if ladder["concept_id"] not in cur["served_concepts"]:
            cur["served_concepts"].append(ladder["concept_id"])
            _save(repo, prog)
        return ladder
    for cid in cur["revisit_ladders"]:
        if (_concept_status(prog, cid) != "retired"
                and not _finding_answered_in(prog, cid, session["id"])):
            item = _revisit_ladder(repo, prog, idx, cid)
            if item:
                return item
    return None


def _held_for_revisit(prog, cid):
    """A concept queued for revisit when the session started is only served through
    the capped revisit step (concepts sent to revisit during the session keep their slot)."""
    cur = prog["current"]
    return cid in cur.get("revisit_at_start", []) and cid not in cur["revisit_served"]


def _next_project(repo, prog, session, idx, tm, module_banks):
    """The session's module, phase by phase: every concept's explain-it question, then
    what-if, then spot-the-flaw / review-diff. A concept moves to the next phase this
    session only if it passed (correct or partial) the one before; None when the quiz part
    of the module is over."""
    cur = prog["current"]
    if session["module_id"] is None:
        session["module_id"] = _pick_module(prog, tm, module_banks)
        if session["module_id"] is None:
            return None
        _save(repo, prog)
    mid = session["module_id"]
    if mid not in module_banks:
        return _build_action("build_module", _module_rel(mid), "module-" + mid, module_id=mid)
    cids = [cid for cid in _module_concept_ids(tm, module_banks, mid)
            if (idx["concepts"].get(cid) or {}).get("concept", {}).get("kind") == "quiz"
            and not _held_for_revisit(prog, cid)]
    for phase in selection.QUIZ_PHASES:
        for cid in cids:
            if _concept_status(prog, cid) in ("solid", "retired"):
                continue
            served = cur["served_phases"].get(cid, [])
            gate = _session_gate(prog, cid, session["id"])
            if phase in served or (gate is not None and not gate[0]):
                continue
            item = _serve(repo, prog, idx, cid, phase=phase, allow_rebuild=not served)
            if item:
                return item
    return None


def _next_diff(repo, prog, session, idx, diff_bank):
    """The risk-ordered window of concepts, phase by phase. Returns (item or None,
    uncovered concept ids)."""
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
    for phase in selection.QUIZ_PHASES:
        for cid in window:
            served = cur["served_questions"].get(cid, 0)
            if served >= per_concept:
                continue
            if served == 0:
                item = _serve(repo, prog, idx, cid, phase=phase)
            elif _concept_status(prog, cid) == "solid":
                continue
            else:
                # a 2nd question: the next phase if the 1st passed, else the same phase
                passed, last_phase = _session_gate(prog, cid, session["id"]) or (True, None)
                item = _serve(repo, prog, idx, cid, phase=phase,
                              only_phase=None if passed else last_phase, allow_rebuild=False)
            if item:
                return item, uncovered
    return None, uncovered


# -------------------------------------------------------------------- record

def _kind_rules(rec):
    errs = []
    kind = rec["kind"]
    if kind == "answer":
        for k in ("question_kind", "outcome"):
            if k not in rec:
                errs.append("$: answer record needs '%s'" % k)
        if rec.get("question_kind") == "quiz":
            for k in ("style", "skill", "difficulty"):
                if k not in rec:
                    errs.append("$: quiz answer record needs '%s'" % k)
            if not isinstance(rec.get("question_id"), str) or not rec["question_id"]:
                errs.append("$: quiz answer record needs a string 'question_id'")
    elif kind == "ladder_progress":
        if not isinstance(rec.get("ladder_rung_reached"), int):
            errs.append("$: ladder_progress record needs integer 'ladder_rung_reached'")
    elif kind == "fix_check" and "fix_verdict" not in rec:
        errs.append("$: fix_check record needs 'fix_verdict'")
    return errs


def _deepest_phase(repo, session, cid):
    tm, _, banks = _load_scope(repo, session)
    return selection.deepest_phase(_index(tm, banks)["questions"].get(cid, []))


def record(repo, rec) -> dict:
    if not isinstance(rec, dict):
        raise ProgressError("state_corrupt", "invalid record: $: expected an object")
    errs = schema.validate(rec, schema.load_schema("record"))
    if not errs:
        errs = _kind_rules(rec)
    if errs:
        raise ProgressError("state_corrupt", "invalid record: " + "; ".join(errs))
    rec = dict(rec, timestamp=_now())  # the CLI's clock, never the caller's
    prog = load(repo)
    session = _require_session(repo, prog)
    if rec["session_id"] != session["id"]:
        raise ProgressError("state_corrupt", "record is for session %s but the current session is %s"
                            % (rec["session_id"], session["id"]))
    cid, kind = rec["concept_id"], rec["kind"]
    if kind == "fix_check" and _finding_state(prog, cid)["status"] != "recheck":
        raise ProgressError("state_corrupt", "fix_check for %s, which is not awaiting a recheck" % cid)
    if kind == "ladder_progress":
        saved = int(_finding_state(prog, cid).get("rung") or 1)
        if rec["ladder_rung_reached"] != saved + 1:
            raise ProgressError("state_corrupt", "ladder_progress for %s must move one rung down, "
                                "from rung %d to %d (got %d)"
                                % (cid, saved, saved + 1, rec["ladder_rung_reached"]))
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
            # Explained by last resort: back to the revisit queue (spec 9.5), not to the
            # findings phase; no longer an undisclosed issue.
            st.update(status="revisit", rung=1, explained=True)
            _add_revisit(prog, cid)
        else:
            st["status"] = "completed"
            _remove_revisit(prog, cid)
        result = st["status"]
    else:
        # Solid needs a correct answer on the deepest phase the concept has questions for;
        # phase_passed is the deepest phase answered correct or partial (the phase gate).
        c = _concept(prog, cid)
        phase = selection.phase_of(rec.get("style"))
        if rec["outcome"] in selection.PASSED:
            c["phase_passed"] = max(int(c.get("phase_passed") or 0), phase)
        deepest = phase >= (_deepest_phase(repo, session, cid) or phase)
        if rec["outcome"] == "correct" and deepest:
            c["status"] = "solid"
            _remove_revisit(prog, cid)
        elif rec["outcome"] == "correct":
            pass  # below the deepest phase: a revisit concept stays queued until it gets there
        elif (rec["outcome"] == "skipped" or rec.get("followups_used", 0) >= MAX_FOLLOWUPS
              or (deepest and rec["outcome"] in ("incorrect", "dont_know"))):
            # A failed deepest question parks the concept in revisit so the module can
            # still advance (_concept_done); it comes back as a warm-up revisit.
            c["status"] = "needs_revisit"
            _add_revisit(prog, cid)
        result = c["status"]
    cur = prog.get("current")
    if (cur and cur.get("pending") and kind == "answer"
            and cid == cur["pending"]["concept_id"]):
        cur["pending"] = None
    if kind != "answer" or rec["question_kind"] == "finding":
        _link_duplicates(repo, prog)  # carry the new state to the same issue's other findings
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
