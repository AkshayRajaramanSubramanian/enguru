import json
from pathlib import Path

import pytest

from conftest import make_repo, run_cli, run_cli_raw
from enguru_lib import bank, digest, paths, progress
from enguru_lib.progress import recommend_intensity

LINES = "".join("line %d\n" % i for i in range(1, 21))
TS = "2026-10-21T10:00:00Z"


# ------------------------------------------------------------------ factories

def concept(cid, kind="quiz", risk=2, module="m1", files=("app.py",)):
    return {"id": cid, "module": module, "title": "Title " + cid, "why_it_matters": "w",
            "risk_tier": risk, "kind": kind,
            "evidence_refs": [{"file": f, "lines": [1, 2]} for f in files],
            "evidence_hashes": {}}


def question(qid, cid, difficulty="intermediate", style="explain_it", file="app.py"):
    return {"id": qid, "concept_id": cid, "style": style, "skill": "comprehension",
            "difficulty": difficulty, "prompt": "prompt " + qid, "context": "",
            "answer_key": {"must_have": [{"point": "p", "explanation": "e",
                                          "evidence_ref": {"file": file, "lines": [1, 2]}}],
                           "nice_to_have": [], "misconceptions": []}}


def finding(cid, severity="major", rungs=3, file="app.py", verdict="confirmed"):
    return {"concept_id": cid, "severity": severity,
            "lines": {"file": file, "start": 1, "end": 2},
            "consequence": "c", "fix": "f",
            "ladder": [{"question": "%s rung %d" % (cid, i + 1),
                        "expected_realization": "r%d" % (i + 1)} for i in range(rungs)],
            "verdict": verdict}


def make_bank(quiz=(), findings=(), per_concept=1, scope_key="diff"):
    """quiz: concept ids or (cid, risk) tuples; findings: finding dicts."""
    concepts, questions = [], []
    for item in quiz:
        cid, risk = (item, 2) if isinstance(item, str) else item
        concepts.append(concept(cid, risk=risk))
        diffs = ["intermediate", "foundational", "advanced"]
        for i in range(per_concept):
            questions.append(question("q-%s-%d" % (cid, i + 1), cid, diffs[i % 3]))
    for f in findings:
        concepts.append(concept(f["concept_id"], kind="finding", risk=1, files=(f["lines"]["file"],)))
        questions.append(question("q-%s-fb" % f["concept_id"], f["concept_id"]))
    return {"scope_key": scope_key, "concepts": concepts, "questions": questions,
            "findings": list(findings)}


def diff_repo(tmp_path, extra=None):
    files = {"app.py": LINES}
    files.update(extra or {})
    return make_repo(tmp_path, [files, {"app.py": LINES + "more\n"}], ["init", "change"])


def diff_scope(repo):
    return digest.resolve_scope(repo, False, "HEAD~1..HEAD", False)


def diff_bank_path(repo, scope):
    return Path(repo) / ".enguru" / "banks" / "diff" / (scope.range_id + ".json")


def write_bank(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    paths.atomic_write_json(path, data)


def start_diff(tmp_path, b, intensity="quick", repo=None, seal=False):
    repo = repo or diff_repo(tmp_path)
    scope = diff_scope(repo)
    bp = diff_bank_path(repo, scope)
    write_bank(bp, b)
    if seal:
        bank.seal(repo, bp)
    s = progress.start_session(repo, scope, intensity)
    return repo, scope, s


def restart(repo, scope, intensity="quick"):
    progress.end_session(repo)
    return progress.start_session(repo, scope, intensity)


def rec(sid, cid, qid=None, outcome="correct", kind="answer", qkind="quiz", **kw):
    r = {"kind": kind, "session_id": sid, "concept_id": cid, "question_id": qid,
         "question_kind": qkind, "style": "explain_it", "skill": "comprehension",
         "difficulty": "intermediate", "outcome": outcome, "must_haves_hit": [],
         "misconception_stated": False, "hints_used": 0, "followups_used": 0,
         "ladder_rung_reached": None, "ladder_length": None, "fix_valid": None,
         "revealed": False, "scope": "diff", "range_id": None, "intensity": None,
         "timestamp": TS}
    if qkind == "finding":
        r.update(style="review_diff", difficulty="advanced")
    r.update(kw)
    return r


def answer_ask(repo, sid, item, outcome="correct", **kw):
    assert item["action"] == "ask", item
    q = item["question"]
    return progress.record(repo, rec(sid, q["concept_id"], q["id"], outcome, **kw))


def finish_ladder(repo, sid, cid, revealed=False):
    return progress.record(repo, rec(sid, cid, None, "correct", qkind="finding",
                                     ladder_rung_reached=1, ladder_length=3, revealed=revealed))


def load_progress(repo):
    return paths.read_json(Path(repo) / ".enguru" / "progress.json")


# ---------------------------------------------------------------- named tests

def test_findings_first_critical_before_major_and_announce_on_rung1(tmp_path):
    b = make_bank(["c1", "c2"], [finding("f-major", "major"), finding("f-crit", "critical")])
    repo, scope, s = start_diff(tmp_path, b)
    sid = s["session_id"]
    item = progress.next_item(repo)
    assert item["action"] == "ladder" and item["concept_id"] == "f-crit"
    assert item["severity"] == "critical" and item["rung"] == 1 and item["announce"] is True
    assert item["step"] == b["findings"][1]["ladder"][0] and item["ladder_length"] == 3
    progress.record(repo, rec(sid, "f-crit", None, "incorrect", kind="ladder_progress",
                              qkind="finding", ladder_rung_reached=2, ladder_length=3))
    item = progress.next_item(repo)
    assert item["concept_id"] == "f-crit" and item["rung"] == 2 and item["announce"] is False
    assert item["step"]["question"] == "f-crit rung 2"
    assert finish_ladder(repo, sid, "f-crit")["concept_status"] == "completed"
    item = progress.next_item(repo)
    assert item["action"] == "ladder" and item["concept_id"] == "f-major" and item["announce"] is True


def test_in_progress_finding_served_before_open_critical(tmp_path):
    b = make_bank(["c1"], [finding("f-crit", "critical"), finding("f-major", "major")])
    repo, scope, s = start_diff(tmp_path, b)
    progress.record(repo, rec(s["session_id"], "f-major", None, "incorrect", kind="ladder_progress",
                              qkind="finding", ladder_rung_reached=2, ladder_length=3))
    item = progress.next_item(repo)
    assert item["concept_id"] == "f-major" and item["rung"] == 2


def test_revisit_capped_at_two_per_session(tmp_path):
    b = make_bank([("n1", 1), ("r1", 3), ("r2", 3), ("r3", 3)])
    repo, scope, s = start_diff(tmp_path, b)
    for cid in ("r1", "r2", "r3"):
        progress.record(repo, rec(s["session_id"], cid, "q-%s-1" % cid, "skipped"))
    assert load_progress(repo)["revisit"] == ["r1", "r2", "r3"]
    s2 = restart(repo, scope)
    assert s2["revisit_count"] == 3
    served = []
    while True:
        item = progress.next_item(repo)
        if item["action"] == "done":
            break
        served.append(item["question"]["concept_id"])
        answer_ask(repo, s2["session_id"], item, "partial")
    assert served == ["r1", "r2", "n1"]  # r3 waits for a later session: cap of 2
    assert item["uncovered"] == []


def test_revisit_with_all_questions_asked_requests_rebuild(tmp_path):
    b = make_bank([("r1", 1)])
    repo, scope, s = start_diff(tmp_path, b)
    item = progress.next_item(repo)
    assert item["question"]["id"] == "q-r1-1"
    answer_ask(repo, s["session_id"], item, "skipped")
    restart(repo, scope)
    item = progress.next_item(repo)
    assert item == {
        "action": "rebuild_concept", "concept_id": "r1", "reason": "revisit",
        "bank_path": ".enguru/banks/diff/%s.json" % scope.range_id,
        "out_path": ".enguru/work/revisit-r1.json",
        "asked_question_ids": ["q-r1-1"],
        "digest_path": ".enguru/digest.json", "keylog_path": ".enguru/keylog.jsonl",
    }
    partial = Path(repo) / ".enguru" / "work" / "revisit-r1.json"
    partial.parent.mkdir(parents=True, exist_ok=True)
    paths.atomic_write_json(partial, {"scope_key": "diff", "concepts": [],
                                      "questions": [question("q-r1-new", "r1")], "findings": []})
    bank.merge_partial(diff_bank_path(repo, scope), partial)
    item = progress.next_item(repo)
    assert item["action"] == "ask" and item["question"]["id"] == "q-r1-new"


def test_quick_serves_five_concepts_plus_findings(tmp_path):
    quiz = [("c%d" % i, 3 if i < 3 else 2) for i in range(7)]  # c0..c2 risk 3, c3..c6 risk 2
    b = make_bank(quiz, [finding("f1")])
    repo, scope, s = start_diff(tmp_path, b, "quick")
    sid = s["session_id"]
    item = progress.next_item(repo)
    assert item["action"] == "ladder"
    finish_ladder(repo, sid, "f1")
    asked = []
    while True:
        item = progress.next_item(repo)
        if item["action"] == "done":
            break
        asked.append(item["question"]["concept_id"])
        answer_ask(repo, sid, item, "partial")
    assert asked == ["c3", "c4", "c5", "c6", "c0"]
    assert item["uncovered"] == ["c1", "c2"]


def test_thorough_caps_12_concepts_2_questions(tmp_path):
    b = make_bank(["c%02d" % i for i in range(13)], per_concept=3)
    repo, scope, s = start_diff(tmp_path, b, "thorough")
    sid = s["session_id"]
    asked = []
    while True:
        item = progress.next_item(repo)
        if item["action"] == "done":
            break
        asked.append(item["question"]["concept_id"])
        answer_ask(repo, sid, item, "partial")
    assert len(asked) == 24
    assert len(set(asked)) == 12
    assert all(asked.count(c) == 2 for c in set(asked))
    assert asked[0] == asked[1]  # 2nd question follows the 1st immediately
    assert item["uncovered"] == ["c12"]


def test_thorough_second_question_skipped_when_solid(tmp_path):
    b = make_bank(["a", "b"], per_concept=3)
    repo, scope, s = start_diff(tmp_path, b, "thorough")
    item = progress.next_item(repo)
    answer_ask(repo, s["session_id"], item, "correct")
    assert progress.next_item(repo)["question"]["concept_id"] == "b"


def test_starting_difficulty_foundational_after_failure(tmp_path):
    b = make_bank(["c1"])
    b["questions"] = [question("qa", "c1", "advanced"), question("qf", "c1", "foundational"),
                      question("qi", "c1", "intermediate")]
    b["concepts"].append(concept("c2"))
    b["questions"].append(question("qa2", "c2", "advanced"))
    repo, scope, s = start_diff(tmp_path, b)
    item = progress.next_item(repo)
    assert item["question"]["id"] == "qi"
    answer_ask(repo, s["session_id"], item, "incorrect")
    item = progress.next_item(repo)
    assert item["question"]["id"] == "qa2"  # only advanced available: nearest difficulty
    answer_ask(repo, s["session_id"], item, "correct")
    restart(repo, scope)
    item = progress.next_item(repo)
    assert item["question"]["id"] == "qf"


def test_skip_and_followup_cap_send_to_revisit(tmp_path):
    repo, scope, s = start_diff(tmp_path, make_bank(["c1", "c2", "c3"]))
    sid = s["session_id"]
    assert progress.record(repo, rec(sid, "c1", "q-c1-1", "skipped")) == \
        {"ok": True, "concept_status": "needs_revisit"}
    r = progress.record(repo, rec(sid, "c2", "q-c2-1", "partial", followups_used=3))
    assert r["concept_status"] == "needs_revisit"
    r = progress.record(repo, rec(sid, "c3", "q-c3-1", "partial", followups_used=2))
    assert r["concept_status"] == "open"
    progress.record(repo, rec(sid, "c2", "q-c2-1", "skipped"))
    assert load_progress(repo)["revisit"] == ["c1", "c2"]
    assert progress.record(repo, rec(sid, "c1", "q-c1-1", "correct"))["concept_status"] == "solid"
    assert load_progress(repo)["revisit"] == ["c2"]


def test_revealed_finding_returns_to_revisit_open(tmp_path):
    b = make_bank(["c1"], [finding("f1")])
    repo, scope, s = start_diff(tmp_path, b)
    sid = s["session_id"]
    progress.record(repo, rec(sid, "f1", None, "incorrect", kind="ladder_progress",
                              qkind="finding", ladder_rung_reached=3, ladder_length=3))
    r = finish_ladder(repo, sid, "f1", revealed=True)
    assert r == {"ok": True, "concept_status": "open"}
    p = load_progress(repo)
    assert p["findings"]["f1"]["status"] == "open" and p["findings"]["f1"]["rung"] == 1
    assert "f1" in p["revisit"]
    item = progress.next_item(repo)
    assert item["action"] == "ask" and item["question"]["concept_id"] == "c1"
    restart(repo, scope)
    item = progress.next_item(repo)
    assert item["action"] == "ladder" and item["concept_id"] == "f1"
    assert item["rung"] == 1 and item["announce"] is True


def test_ladder_progress_resumes_at_saved_rung(tmp_path):
    f = finding("f1", rungs=4)
    repo, scope, s = start_diff(tmp_path, make_bank(["c1"], [f]))
    assert progress.next_item(repo)["rung"] == 1
    r = progress.record(repo, rec(s["session_id"], "f1", None, "dont_know", kind="ladder_progress",
                                  qkind="finding", ladder_rung_reached=2, ladder_length=4))
    assert r["concept_status"] == "in_progress"
    restart(repo, scope)
    item = progress.next_item(repo)
    assert item["action"] == "ladder" and item["rung"] == 2
    assert item["step"] == f["ladder"][1] and item["announce"] is False


def test_end_session_lists_undisclosed_critical(tmp_path):
    b = make_bank(["c1"], [finding("f-crit", "critical", file="auth.py"), finding("f-maj", "major")])
    repo = diff_repo(tmp_path, {"auth.py": LINES})
    repo, scope, s = start_diff(tmp_path, b, repo=repo)
    sid = s["session_id"]
    progress.record(repo, rec(sid, "f-crit", None, "incorrect", kind="ladder_progress",
                              qkind="finding", ladder_rung_reached=2, ladder_length=3))
    answer_ask(repo, sid, {"action": "ask", "question": b["questions"][0]}, "correct")
    out = progress.end_session(repo)
    assert out["undisclosed_critical"] == [
        {"concept_id": "f-crit", "title": "Title f-crit", "file": "auth.py"}]
    assert out["solid"] == ["c1"]
    assert out["revisit"] == []
    assert out["scored_since_last_certificate"] == 1
    assert out["suggest_certificate"] is False
    assert not (Path(repo) / ".enguru" / "lock").exists()
    p = load_progress(repo)
    assert p["current"] is None and p["sessions"][-1]["ended"]


def test_stale_finding_marked_recheck_and_served_verify_fix(tmp_path):
    b = make_bank(["c1"], [finding("f1"), finding("f2")])
    repo, scope, s = start_diff(tmp_path, b, seal=True)
    finish_ladder(repo, s["session_id"], "f1")
    progress.end_session(repo)
    (Path(repo) / "app.py").write_text(LINES + "edited\n")
    s2 = progress.start_session(repo, scope, "quick")
    p = load_progress(repo)
    assert p["findings"]["f1"]["status"] == "recheck"
    assert p["findings"]["f1"]["prev_status"] == "completed"
    assert p["findings"]["f2"]["status"] == "recheck"
    item = progress.next_item(repo)
    assert item == {"action": "verify_fix", "concept_id": "f1", "finding": b["findings"][0],
                    "out_path": ".enguru/work/verdicts/fixcheck-f1.json"}
    r = progress.record(repo, rec(s2["session_id"], "f1", kind="fix_check", qkind="finding",
                                  fix_verdict="fixed"))
    assert r["concept_status"] == "fixed"
    assert progress.next_item(repo)["concept_id"] == "f2"
    r = progress.record(repo, rec(s2["session_id"], "f2", kind="fix_check", qkind="finding",
                                  fix_verdict="still_present"))
    assert r["concept_status"] == "open"
    assert "prev_status" not in load_progress(repo)["findings"]["f2"]
    bank.seal(repo, diff_bank_path(repo, scope))
    item = progress.next_item(repo)
    assert item["action"] == "ladder" and item["concept_id"] == "f2"


def test_retired_concept_leaves_revisit(tmp_path):
    b = make_bank(["c1"])
    b["concepts"].append(concept("r1", files=("gone.py",)))
    b["questions"].append(question("q-r1", "r1", file="gone.py"))
    repo = diff_repo(tmp_path, {"gone.py": LINES})
    repo, scope, s = start_diff(tmp_path, b, repo=repo, seal=True)
    progress.record(repo, rec(s["session_id"], "r1", "q-r1", "skipped"))
    assert load_progress(repo)["revisit"] == ["r1"]
    progress.end_session(repo)
    (Path(repo) / "gone.py").unlink()
    s2 = progress.start_session(repo, scope, "quick")
    assert s2["revisit_count"] == 0
    p = load_progress(repo)
    assert p["concepts"]["r1"]["status"] == "retired" and p["revisit"] == []
    item = progress.next_item(repo)
    assert item["question"]["concept_id"] == "c1"
    answer_ask(repo, s2["session_id"], item)
    assert progress.next_item(repo) == {"action": "done", "uncovered": []}


def test_recommend_intensity():
    assert recommend_intensity(["src/auth/x.py"]) == "thorough"
    assert recommend_intensity(["db/Migrations/001.sql"]) == "thorough"
    assert recommend_intensity(["a.py", "b.py"]) == "quick"
    assert recommend_intensity(["f%d.py" % i for i in range(10)]) == "thorough"
    assert recommend_intensity(["f%d.py" % i for i in range(9)]) == "quick"


def test_lock_held_cli_error(tmp_path):
    repo = diff_repo(tmp_path)
    first = run_cli(["session", "start", "--project"], repo)
    code, out = run_cli_raw(["session", "start", "--project"], repo)
    assert code == 2 and out["error"] == "lock_held"
    assert first["session_id"] in out["message"] and "heartbeat" in out["message"]
    second = run_cli(["session", "start", "--project", "--takeover"], repo)
    assert second["session_id"] != first["session_id"]
    lock = json.loads((Path(repo) / ".enguru" / "lock").read_text())
    assert lock["session_id"] == second["session_id"] and lock["pid"] is None
    run_cli(["session", "end"], repo)
    assert not (Path(repo) / ".enguru" / "lock").exists()


def test_heartbeat_refreshed_by_next_record_and_set_intensity(tmp_path):
    repo, scope, s = start_diff(tmp_path, make_bank(["c1"]), intensity=None)
    lockp = Path(repo) / ".enguru" / "lock"

    def age_lock():
        data = json.loads(lockp.read_text())
        data["heartbeat"] = 0
        lockp.write_text(json.dumps(data))

    for call in (lambda: progress.set_intensity(repo, "quick"),
                 lambda: progress.next_item(repo),
                 lambda: progress.record(repo, rec(s["session_id"], "c1", "q-c1-1"))):
        age_lock()
        call()
        assert json.loads(lockp.read_text())["heartbeat"] > 0
    age_lock()  # an idle session whose heartbeat went stale can be reclaimed
    s2 = progress.start_session(repo, scope, "quick")
    assert s2["session_id"] != s["session_id"]


def test_done_lists_uncovered_concepts_in_thorough(tmp_path):
    quiz = [("c%02d" % i, 1 if i >= 12 else 2) for i in range(14)]  # c12,c13 risk 1
    repo, scope, s = start_diff(tmp_path, make_bank(quiz), "thorough")
    asked = []
    while True:
        item = progress.next_item(repo)
        if item["action"] == "done":
            break
        asked.append(item["question"]["concept_id"])
        answer_ask(repo, s["session_id"], item, "correct")
    assert asked[:2] == ["c12", "c13"] and len(asked) == 12
    assert item["uncovered"] == ["c10", "c11"]


# ------------------------------------------------------------- further rules

def test_start_session_diff_recommends_and_set_intensity(tmp_path):
    repo = diff_repo(tmp_path)
    s = progress.start_session(repo, diff_scope(repo), None)
    assert s["recommend_intensity"] == "quick" and s["intensity"] is None
    assert s["scope"] == "diff" and s["range_id"] == diff_scope(repo).range_id
    assert ".enguru/" in (Path(repo) / ".gitignore").read_text()
    assert progress.next_item(repo)["action"] == "build_diff_bank"
    progress.set_intensity(repo, "thorough")
    assert load_progress(repo)["sessions"][-1]["intensity"] == "thorough"
    progress.end_session(repo)
    s = progress.start_session(repo, diff_scope(repo), "quick")
    assert "recommend_intensity" not in s and s["intensity"] == "quick"


def test_missing_diff_bank_requests_build(tmp_path):
    repo = diff_repo(tmp_path)
    scope = diff_scope(repo)
    progress.start_session(repo, scope, "quick")
    assert progress.next_item(repo) == {
        "action": "build_diff_bank", "range_id": scope.range_id,
        "out_path": ".enguru/banks/diff/%s.json" % scope.range_id,
        "digest_path": ".enguru/digest.json", "keylog_path": ".enguru/keylog.jsonl"}


def test_resume_returns_same_ask_until_recorded(tmp_path):
    repo, scope, s = start_diff(tmp_path, make_bank(["c1", "c2"], per_concept=2))
    first = progress.next_item(repo)
    assert progress.next_item(repo) == first
    assert load_progress(repo)["concepts"]["c1"]["asked_question_ids"] == ["q-c1-1"]
    answer_ask(repo, s["session_id"], first, "correct")
    assert progress.next_item(repo)["question"]["concept_id"] == "c2"


def test_stale_concept_requests_rebuild_before_ask(tmp_path):
    repo, scope, s = start_diff(tmp_path, make_bank(["c1"]), seal=True)
    (Path(repo) / "app.py").write_text("changed\n" + LINES)
    item = progress.next_item(repo)
    assert item["action"] == "rebuild_concept" and item["reason"] == "stale"
    assert item["concept_id"] == "c1" and item["asked_question_ids"] == []
    bank.seal(repo, diff_bank_path(repo, scope))
    assert progress.next_item(repo)["action"] == "ask"


def test_project_mode_topic_map_module_flow(tmp_path):
    repo = diff_repo(tmp_path)
    s = progress.start_session(repo, digest.Scope("project"), None)
    assert s["module_id"] is None and s["intensity"] is None
    item = progress.next_item(repo)
    assert item == {"action": "build_topic_map",
                    "out_path": ".enguru/banks/project/topic-map.json",
                    "digest_path": ".enguru/digest.json", "keylog_path": ".enguru/keylog.jsonl"}
    pdir = Path(repo) / ".enguru" / "banks" / "project"
    tm = {"modules": [{"id": "m1", "title": "M1", "concepts": [concept("a"), concept("b")]},
                      {"id": "m2", "title": "M2", "concepts": [concept("c", module="m2")]}]}
    write_bank(pdir / "topic-map.json", tm)
    item = progress.next_item(repo)
    assert item == {"action": "build_module", "module_id": "m1",
                    "out_path": ".enguru/banks/project/m1.json",
                    "digest_path": ".enguru/digest.json", "keylog_path": ".enguru/keylog.jsonl"}
    write_bank(pdir / "m1.json", make_bank(["a", "b"], per_concept=2, scope_key="project"))
    sid = s["session_id"]
    for cid, outcome in (("a", "correct"), ("b", "partial")):
        item = progress.next_item(repo)
        assert item["question"]["concept_id"] == cid
        answer_ask(repo, sid, item, outcome, scope="project")
    assert progress.next_item(repo) == {"action": "done", "uncovered": []}
    assert load_progress(repo)["sessions"][-1]["module_id"] == "m1"
    s2 = restart(repo, digest.Scope("project"), None)
    assert s2["module_id"] == "m1"  # b is not solid yet
    item = progress.next_item(repo)
    assert item["question"]["concept_id"] == "b"
    answer_ask(repo, s2["session_id"], item, "correct", scope="project")
    assert restart(repo, digest.Scope("project"), None)["module_id"] == "m2"


def test_suggest_certificate_counts_since_last_certificate(tmp_path):
    repo, scope, s = start_diff(tmp_path, make_bank(["c1"]))
    sid = s["session_id"]
    for day in ("2026-10-01", "2026-10-05", "2026-10-06"):
        progress.record(repo, rec(sid, "c1", "q-c1-1", "partial", timestamp=day + "T09:00:00Z"))
    for i in range(5):
        progress.record(repo, rec(sid, "c1", "q-c1-1", "partial"))
    progress.record(repo, rec(sid, "c1", "q-c1-1", "skipped"))
    cert = Path(repo) / ".enguru" / "certificates"
    cert.mkdir(parents=True)
    (cert / "2026-09-01.json").write_text("{}")
    (cert / "2026-10-05.json").write_text("{}")
    out = progress.end_session(repo)
    assert out["scored_since_last_certificate"] == 6
    assert out["suggest_certificate"] is True


def test_status_and_keylog(tmp_path):
    repo, scope, s = start_diff(tmp_path, make_bank(["c1", "c2"], [finding("f1")]))
    sid = s["session_id"]
    progress.record(repo, rec(sid, "c1", "q-c1-1", "correct"))
    progress.record(repo, rec(sid, "c2", "q-c2-1", "skipped"))
    progress.record(repo, rec(sid, "f1", None, "incorrect", kind="ladder_progress",
                              qkind="finding", ladder_rung_reached=2, ladder_length=3))
    st = progress.status(repo)
    assert st == {"sessions": 1, "answers": 3,
                  "concepts": {"solid": 1, "needs_revisit": 1}, "revisit": ["c2"],
                  "findings": {"f1": {"status": "in_progress", "rung": 2}},
                  "current_session": sid}
    progress.keylog_add(repo, {"question_id": "q-c1-1", "issue": "key misses x", "evidence": "app.py:3"})
    progress.keylog_add(repo, {"question_id": "q-c2-1", "issue": "i", "evidence": "e"})
    lines = (Path(repo) / ".enguru" / "keylog.jsonl").read_text().splitlines()
    assert [json.loads(l)["question_id"] for l in lines] == ["q-c1-1", "q-c2-1"]
    with pytest.raises(progress.ProgressError):
        progress.keylog_add(repo, {"question_id": "q"})


def test_cli_session_next_record_status_keylog(tmp_path):
    repo = diff_repo(tmp_path)
    scope = diff_scope(repo)
    write_bank(diff_bank_path(repo, scope), make_bank(["c1"]))
    s = run_cli(["session", "start", "--diff", "--range", "HEAD~1..HEAD"], repo)
    assert s["recommend_intensity"] == "quick"
    assert run_cli(["session", "set-intensity", "quick"], repo)["intensity"] == "quick"
    item = run_cli(["next"], repo)
    assert item["action"] == "ask"
    r = rec(s["session_id"], "c1", item["question"]["id"], "correct")
    assert run_cli(["record"], repo, stdin=json.dumps(r)) == {"ok": True, "concept_status": "solid"}
    code, out = run_cli_raw(["record"], repo, stdin=json.dumps({"kind": "answer"}))
    assert code == 2 and out["error"] == "state_corrupt"
    code, out = run_cli_raw(["record"], repo, stdin="not json")
    assert code == 2 and out["error"] == "state_corrupt"
    assert run_cli(["status"], repo)["answers"] == 1
    kl = {"question_id": "q", "issue": "i", "evidence": "e"}
    assert run_cli(["keylog", "add"], repo, stdin=json.dumps(kl)) == {"ok": True}
    out = run_cli(["session", "end"], repo)
    assert out["solid"] == ["c1"]


def test_cli_session_start_errors(tmp_path):
    code, out = run_cli_raw(["session", "start", "--diff"], tmp_path)
    assert code == 2 and out["error"] == "not_git"
    repo = make_repo(tmp_path, [{"a.py": "x\n"}], ["init"])
    code, out = run_cli_raw(["session", "start", "--diff", "--range", "HEAD..HEAD"], repo)
    assert code == 2 and out["error"] == "empty_diff"
    assert not (repo / ".enguru" / "lock").exists()


def test_digest_prep_reuses_session_worktree_range_id(tmp_path):
    repo = make_repo(tmp_path, [{"a.py": "x\n"}], ["init"])
    (repo / "a.py").write_text("y\n")
    s = run_cli(["session", "start", "--diff", "--uncommitted", "--quick"], repo)
    assert s["range_id"].startswith("worktree-")
    tr = tmp_path / "transcripts"
    tr.mkdir()
    run_cli(["digest", "prep", "--diff", "--uncommitted", "--transcripts-root", str(tr)], repo)
    meta = json.loads((repo / ".enguru" / "work" / "rules.json").read_text())
    assert meta["scope"]["range_id"] == s["range_id"]


def test_project_mode_revisit_capped(tmp_path):
    repo = diff_repo(tmp_path)
    pdir = Path(repo) / ".enguru" / "banks" / "project"
    ids = ["a", "b", "c", "d"]
    write_bank(pdir / "topic-map.json",
               {"modules": [{"id": "m1", "title": "M1", "concepts": [concept(i) for i in ids]}]})
    write_bank(pdir / "m1.json", make_bank(ids, per_concept=2, scope_key="project"))
    s = progress.start_session(repo, digest.Scope("project"), None)
    for cid in ("a", "b", "c"):
        progress.record(repo, rec(s["session_id"], cid, "q-%s-1" % cid, "skipped", scope="project"))
    s2 = restart(repo, digest.Scope("project"), None)
    served = []
    while True:
        item = progress.next_item(repo)
        if item["action"] == "done":
            break
        served.append(item["question"]["concept_id"])
        answer_ask(repo, s2["session_id"], item, "partial", scope="project")
    assert served == ["a", "b", "d"]


def test_failed_rebuild_skips_concept_for_session(tmp_path):
    repo, scope, s = start_diff(tmp_path, make_bank([("r1", 1), ("c2", 2)]))
    answer_ask(repo, s["session_id"], progress.next_item(repo), "skipped")
    restart(repo, scope)
    assert progress.next_item(repo)["action"] == "rebuild_concept"
    item = progress.next_item(repo)  # the analyst produced nothing new
    assert item["action"] == "ask" and item["question"]["concept_id"] == "c2"
    assert load_progress(repo)["current"]["skipped"] == {"r1": "rebuild_failed:revisit"}


def test_record_requires_matching_current_session(tmp_path):
    repo, scope, s = start_diff(tmp_path, make_bank(["c1"]))
    with pytest.raises(progress.ProgressError) as e:
        progress.record(repo, rec("s-other", "c1", "q-c1-1"))
    assert e.value.code == "state_corrupt"
    code, out = run_cli_raw(["record"], repo, stdin=json.dumps(rec("s-other", "c1", "q-c1-1")))
    assert code == 2 and out["error"] == "state_corrupt"
    progress.end_session(repo)
    with pytest.raises(progress.ProgressError):
        progress.record(repo, rec(s["session_id"], "c1", "q-c1-1"))


def test_pending_findings_request_verification_first(tmp_path):
    b = make_bank(["c1"], [finding("f1", verdict="pending"), finding("f2", verdict="pending")])
    repo, scope, s = start_diff(tmp_path, b)
    assert progress.next_item(repo) == {
        "action": "verify_findings", "bank_path": ".enguru/banks/diff/%s.json" % scope.range_id,
        "concept_ids": ["f1", "f2"]}
    b["findings"] = [dict(b["findings"][0], verdict="confirmed")]
    write_bank(diff_bank_path(repo, scope), b)
    item = progress.next_item(repo)
    assert item["action"] == "ladder" and item["concept_id"] == "f1"


def test_module_with_completed_finding_concept_advances(tmp_path):
    repo = diff_repo(tmp_path)
    pdir = Path(repo) / ".enguru" / "banks" / "project"
    write_bank(pdir / "topic-map.json", {"modules": [
        {"id": "m1", "title": "M1", "concepts": [concept("a"), concept("f")]},
        {"id": "m2", "title": "M2", "concepts": [concept("z", module="m2")]}]})
    write_bank(pdir / "m1.json", make_bank(["a"], [finding("f")], scope_key="project"))
    s = progress.start_session(repo, digest.Scope("project"), None)
    assert s["module_id"] == "m1"
    progress.record(repo, rec(s["session_id"], "a", "q-a-1", "correct", scope="project"))
    assert progress.next_item(repo)["action"] == "ladder"
    finish_ladder(repo, s["session_id"], "f")
    assert progress.next_item(repo) == {"action": "done", "uncovered": []}
    assert restart(repo, digest.Scope("project"), None)["module_id"] == "m2"


def test_pending_cleared_by_answer_on_same_concept(tmp_path):
    repo, scope, s = start_diff(tmp_path, make_bank(["c1", "c2"], per_concept=2))
    assert progress.next_item(repo)["question"]["concept_id"] == "c1"
    progress.record(repo, rec(s["session_id"], "c1", "q-other", "partial"))
    assert progress.next_item(repo)["question"]["concept_id"] == "c2"


def test_fix_check_rejected_unless_recheck(tmp_path):
    repo, scope, s = start_diff(tmp_path, make_bank(["c1"], [finding("f1")]))
    with pytest.raises(progress.ProgressError) as e:
        progress.record(repo, rec(s["session_id"], "f1", kind="fix_check", qkind="finding",
                                  fix_verdict="fixed"))
    assert e.value.code == "state_corrupt"


def test_quick_limit_holds_when_every_answer_is_skipped(tmp_path):
    repo, scope, s = start_diff(tmp_path, make_bank(["c%d" % i for i in range(8)]), "quick")
    asked = []
    while True:
        item = progress.next_item(repo)
        if item["action"] == "done":
            break
        asked.append(item["question"]["concept_id"])
        answer_ask(repo, s["session_id"], item, "skipped")
    assert asked == ["c0", "c1", "c2", "c3", "c4"]
    assert item["uncovered"] == ["c5", "c6", "c7"]


def test_thorough_skipped_concept_gets_second_question(tmp_path):
    repo, scope, s = start_diff(tmp_path, make_bank(["a", "b"], per_concept=3), "thorough")
    answer_ask(repo, s["session_id"], progress.next_item(repo), "skipped")
    assert progress.next_item(repo)["question"]["id"] == "q-a-2"
