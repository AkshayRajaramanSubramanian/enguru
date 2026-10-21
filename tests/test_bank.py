import copy
import json
import subprocess
from pathlib import Path

from conftest import ROOT, make_repo, run_cli, run_cli_raw
from enguru_lib import bank, paths

FIX = ROOT / "tests" / "fixtures" / "schemas" / "bank.json"
LINES10 = "".join("line %d\n" % i for i in range(1, 11))


def base_bank():
    b = json.loads(FIX.read_text())
    b["concepts"][0]["evidence_refs"] = [{"file": "a.py", "lines": [1, 5]}]
    return b


def mk(tmp_path):
    return make_repo(tmp_path, [{"a.py": LINES10}], ["init"])


def sha(repo):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True).stdout.strip()


def test_valid_bank_has_no_problems(tmp_path):
    repo = mk(tmp_path)
    assert bank.validate_bank(repo, base_bank(), {"events": []}) == []


def test_missing_file_and_out_of_range_lines_flagged(tmp_path):
    repo = mk(tmp_path)
    b = base_bank()
    b["concepts"][0]["evidence_refs"] = [{"file": "gone.py", "lines": [1, 2]},
                                         {"file": "a.py", "lines": [5, 99]}]
    b["questions"][0]["answer_key"]["must_have"][0]["evidence_ref"] = {"file": "a.py", "lines": [0, 2]}
    probs = bank.validate_bank(repo, b, {"events": []})
    ids = [p["item_id"] for p in probs]
    assert ids.count("c1") == 2
    assert "q1" in ids
    assert any("gone.py" in p["reason"] for p in probs)


def test_unsafe_paths_flagged(tmp_path):
    repo = mk(tmp_path)
    b = base_bank()
    b["concepts"][0]["evidence_refs"] = [{"file": "/etc/passwd", "lines": [1, 1]},
                                         {"file": "../x.py", "lines": [1, 1]},
                                         {"file": ".env", "lines": [1, 1]}]
    assert len(bank.validate_bank(repo, b, {"events": []})) >= 3


def test_unknown_commit_and_event_flagged(tmp_path):
    repo = mk(tmp_path)
    b = base_bank()
    b["concepts"][0]["evidence_refs"] = [{"commit": "deadbeef" * 5},
                                         {"commit": sha(repo)},
                                         {"event": "ev-missing"},
                                         {"event": "ev-1"}]
    probs = bank.validate_bank(repo, b, {"events": [{"id": "ev-1"}]})
    reasons = " ".join(p["reason"] for p in probs)
    assert len(probs) == 2
    assert "deadbeef" in reasons and "ev-missing" in reasons


def test_string_evidence_ref_forms(tmp_path):
    repo = mk(tmp_path)
    b = base_bank()
    mh = b["questions"][0]["answer_key"]["must_have"][0]
    mh["evidence_ref"] = "a.py:2-4"
    assert bank.validate_bank(repo, b, {"events": []})[0]["item_id"] == "$"  # schema rejects string
    refs = list(bank._refs_in_answer_key({"must_have": [mh], "nice_to_have": [], "misconceptions": []}))
    assert refs == [{"file": "a.py", "lines": [2, 4]}]
    assert bank.parse_ref("a.py") == {"file": "a.py"}
    assert bank.parse_ref("a.py:3") == {"file": "a.py", "lines": [3, 3]}
    assert bank.parse_ref("commit:abc") == {"commit": "abc"}
    assert bank.parse_ref("event:ev-1") == {"event": "ev-1"}


def test_empty_must_have_and_schema_error(tmp_path):
    repo = mk(tmp_path)
    b = base_bank()
    b["questions"][0]["answer_key"]["must_have"] = []
    probs = bank.validate_bank(repo, b, {"events": []})
    assert probs and probs[0]["item_id"] == "$"
    assert "minItems" in probs[0]["reason"]


def test_finding_without_fallback_question_flagged(tmp_path):
    repo = mk(tmp_path)
    b = base_bank()
    b["concepts"][0]["kind"] = "finding"
    assert bank.validate_bank(repo, b, {"events": []}) == []
    b["questions"] = []
    probs = bank.validate_bank(repo, b, {"events": []})
    assert [p["item_id"] for p in probs] == ["c1"]
    assert "fallback" in probs[0]["reason"]


def test_finding_lines_and_flaw_checked(tmp_path):
    repo = mk(tmp_path)
    b = base_bank()
    b["findings"][0]["lines"] = {"file": "a.py", "start": 3, "end": 50}
    probs = bank.validate_bank(repo, b, {"events": []})
    assert [p["item_id"] for p in probs] == ["c1"]
    b = base_bank()
    q = b["questions"][0]
    q["style"] = "spot_the_flaw"
    q["flaw"] = {"original": "a\nb\n", "flawed": "a\nb\n", "category": "off_by_one",
                 "location": {"file": "a.py", "lines": [1, 2]}}
    probs = bank.validate_bank(repo, b, {"events": []})
    assert [p["item_id"] for p in probs] == ["q1"]
    q["flaw"]["flawed"] = "a\nc\n"
    assert bank.validate_bank(repo, b, {"events": []}) == []


def test_flaw_region_ok_cases():
    orig = "".join("l%d\n" % i for i in range(10))
    one = orig.replace("l3\n", "X\n")
    assert bank.flaw_region_ok(orig, one)
    two = orig.replace("l1\n", "X\n").replace("l8\n", "Y\n")
    assert not bank.flaw_region_ok(orig, two)
    six = "".join(("X%d\n" % i if 2 <= i < 8 else "l%d\n" % i) for i in range(10))
    assert not bank.flaw_region_ok(orig, six)
    big = "".join("l%d\n" % i for i in range(41))
    assert not bank.flaw_region_ok(big, big.replace("l3\n", "X\n"))
    assert not bank.flaw_region_ok(orig, orig)


def test_seal_then_stale_after_edit(tmp_path):
    repo = mk(tmp_path)
    (repo / "b.py").write_text("x\n")
    b = base_bank()
    b["concepts"][0]["evidence_refs"].append({"file": "nope.py", "lines": [1, 1]})
    b["questions"][0]["answer_key"]["must_have"][0]["evidence_ref"] = {"file": "b.py", "lines": [1, 1]}
    bp = tmp_path / "bank.json"
    bp.write_text(json.dumps(b))
    bank.seal(repo, bp)
    sealed = json.loads(bp.read_text())
    h = sealed["concepts"][0]["evidence_hashes"]
    assert set(h) == {"a.py", "b.py"}
    assert all(v.startswith("sha256:") and len(v) == 71 for v in h.values())
    assert bank.stale_concepts(repo, sealed["concepts"]) == ([], [])
    (repo / "a.py").write_text("changed\n")
    assert bank.stale_concepts(repo, sealed["concepts"]) == (["c1"], [])


def test_retired_when_files_deleted(tmp_path):
    repo = mk(tmp_path)
    b = base_bank()
    bp = tmp_path / "bank.json"
    bp.write_text(json.dumps(b))
    bank.seal(repo, bp)
    sealed = json.loads(bp.read_text())
    (repo / "a.py").unlink()
    assert bank.stale_concepts(repo, sealed["concepts"]) == ([], ["c1"])
    nohash = [dict(sealed["concepts"][0], evidence_hashes={})]
    assert bank.stale_concepts(repo, nohash) == ([], [])


def test_seal_topic_map(tmp_path):
    repo = mk(tmp_path)
    c = base_bank()["concepts"][0]
    tm = {"modules": [{"id": "m%d" % i, "title": "t", "concepts": [dict(c, id="c%d" % i)]} for i in range(4)]}
    p = tmp_path / "tm.json"
    p.write_text(json.dumps(tm))
    bank.seal(repo, p)
    out = json.loads(p.read_text())
    assert all(m["concepts"][0]["evidence_hashes"] for m in out["modules"])


def test_drop_items(tmp_path):
    b = base_bank()
    b["concepts"].append(dict(b["concepts"][0], id="c2"))
    b["questions"].append(dict(copy.deepcopy(b["questions"][0]), id="q2", concept_id="c2"))
    b["questions"].append(dict(copy.deepcopy(b["questions"][0]), id="q3", concept_id="c1"))
    b["findings"].append(dict(copy.deepcopy(b["findings"][0]), concept_id="c2"))
    bp = tmp_path / "bank.json"
    bp.write_text(json.dumps(b))
    bank.drop_items(bp, ["q3"])
    out = json.loads(bp.read_text())
    assert [q["id"] for q in out["questions"]] == ["q1", "q2"]
    bank.drop_items(bp, ["c2"])
    out = json.loads(bp.read_text())
    assert [c["id"] for c in out["concepts"]] == ["c1"]
    assert [q["id"] for q in out["questions"]] == ["q1"]
    assert [f["concept_id"] for f in out["findings"]] == ["c1"]


def _verdict(repo, cid, verdict, mode="verify"):
    d = repo / ".enguru" / "work" / "verdicts"
    d.mkdir(parents=True, exist_ok=True)
    (d / (cid + ".json")).write_text(json.dumps(
        {"concept_id": cid, "mode": mode, "verdict": verdict, "reason": "r"}))


def test_apply_verdicts_three_outcomes(tmp_path):
    repo = mk(tmp_path)
    b = base_bank()
    c0, q0, f0 = b["concepts"][0], b["questions"][0], b["findings"][0]
    c0["kind"] = "finding"
    cs, qs, fs = [], [], []
    for cid in ["ca", "cb", "cc", "cd", "ce"]:
        cs.append(dict(copy.deepcopy(c0), id=cid))
        qs.append(dict(copy.deepcopy(q0), id="q-" + cid, concept_id=cid))
        fs.append(dict(copy.deepcopy(f0), concept_id=cid))
    b["concepts"], b["questions"], b["findings"] = cs, qs, fs
    _verdict(repo, "ca", "confirmed")
    _verdict(repo, "cb", "refuted")
    _verdict(repo, "cc", "uncertain")
    # cd: missing verdict; ce: invalid (wrong mode)
    _verdict(repo, "ce", "confirmed", mode="fix_check")
    bp = tmp_path / "bank.json"
    bp.write_text(json.dumps(b))
    res = bank.apply_verdicts(repo, bp)
    assert res == {"confirmed": ["ca"], "refuted": ["cb"], "downgraded": ["cc", "cd", "ce"]}
    out = json.loads(bp.read_text())
    assert [f["concept_id"] for f in out["findings"]] == ["ca"]
    assert out["findings"][0]["verdict"] == "confirmed"
    kinds = {c["id"]: c["kind"] for c in out["concepts"]}
    assert kinds["ca"] == "finding" and kinds["cc"] == "quiz" and kinds["cd"] == "quiz"
    assert "cb" in kinds  # questions kept, concept kept


def test_refuted_concept_without_questions_dropped(tmp_path):
    repo = mk(tmp_path)
    b = base_bank()
    b["concepts"][0]["kind"] = "finding"
    b["questions"] = []
    bp = tmp_path / "bank.json"
    bp.write_text(json.dumps(b))
    _verdict(repo, "c1", "refuted")
    bank.apply_verdicts(repo, bp)
    out = json.loads(bp.read_text())
    assert out["concepts"] == [] and out["findings"] == []


def test_cli_bank_commands(tmp_path):
    repo = mk(tmp_path)
    bp = repo / ".enguru" / "bank.json"
    bp.parent.mkdir()
    bp.write_text(json.dumps(base_bank()))
    out = run_cli(["bank", "validate", ".enguru/bank.json"], repo)
    assert out == {"ok": True, "problems": []}
    run_cli(["bank", "seal", str(bp)], repo)
    assert json.loads(bp.read_text())["concepts"][0]["evidence_hashes"]
    out = run_cli(["bank", "apply-verdicts", str(bp)], repo)
    assert out["downgraded"] == ["c1"]
    out = run_cli(["bank", "drop", str(bp), "--ids", "q1,zzz"], repo)
    assert json.loads(bp.read_text())["questions"] == []
    # topic map
    c = base_bank()["concepts"][0]
    tm = {"modules": [{"id": "m%d" % i, "title": "t", "concepts": [dict(c, id="c%d" % i)] * 3}
                      for i in range(4)]}
    (repo / "tm.json").write_text(json.dumps(tm))
    assert run_cli(["bank", "validate", "tm.json"], repo)["ok"] is True
    # errors
    code, out = run_cli_raw(["bank", "validate", "missing.json"], repo)
    assert code == 2 and out["error"] == "state_corrupt"
    (repo / "bad.json").write_text("{nope")
    code, out = run_cli_raw(["bank", "validate", "bad.json"], repo)
    assert code == 2 and out["error"] == "state_corrupt"


def test_merge_partial_replaces_by_id_and_adds_new(tmp_path):
    repo = mk(tmp_path)
    bp = repo / "bank.json"
    paths.atomic_write_json(bp, base_bank())
    part = copy.deepcopy(base_bank())
    part["concepts"][0]["title"] = "New title"
    q2 = copy.deepcopy(part["questions"][0])
    q2["id"] = "q2"
    part["questions"][0]["prompt"] = "replaced"
    part["questions"].append(q2)
    part["findings"][0]["severity"] = "critical"
    pp = repo / "part.json"
    paths.atomic_write_json(pp, part)
    bank.merge_partial(bp, pp)
    out = paths.read_json(bp)
    assert [c["title"] for c in out["concepts"]] == ["New title"]
    assert [(q["id"], q["prompt"]) for q in out["questions"]] == [("q1", "replaced"), ("q2", "p")]
    assert [f["severity"] for f in out["findings"]] == ["critical"]
    run_cli(["bank", "merge", "bank.json", "part.json"], repo)


def test_merge_partial_creates_missing_target_bank(tmp_path):
    repo = mk(tmp_path)
    pp = repo / "part.json"
    paths.atomic_write_json(pp, base_bank())
    run_cli(["bank", "merge", "new/m9.json", "part.json"], repo)
    out = paths.read_json(repo / "new" / "m9.json")
    assert out == {"scope_key": "project", "concepts": base_bank()["concepts"],
                   "questions": base_bank()["questions"], "findings": base_bank()["findings"]}


def test_seal_with_ids_only_rehashes_those_concepts(tmp_path):
    repo = mk(tmp_path)
    b = base_bank()
    b["concepts"].append(dict(copy.deepcopy(b["concepts"][0]), id="c2"))
    bp = tmp_path / "bank.json"
    bp.write_text(json.dumps(b))
    bank.seal(repo, bp)
    (repo / "a.py").write_text("changed\n")
    run_cli(["bank", "seal", str(bp), "--ids", "c2"], repo)
    out = json.loads(bp.read_text())
    assert bank.stale_concepts(repo, out["concepts"]) == (["c1"], [])
    run_cli(["bank", "seal", str(bp)], repo)
    assert bank.stale_concepts(repo, json.loads(bp.read_text())["concepts"]) == ([], [])


def test_cli_bank_finding_writes_one_finding(tmp_path):
    repo = mk(tmp_path)
    b = base_bank()
    bp = repo / ".enguru" / "bank.json"
    bp.parent.mkdir()
    bp.write_text(json.dumps(b))
    cid = b["findings"][0]["concept_id"]
    out = run_cli(["bank", "finding", ".enguru/bank.json", cid,
                   "--out", ".enguru/work/findings/%s.json" % cid], repo)
    assert out == {"ok": True, "out": ".enguru/work/findings/%s.json" % cid}
    written = json.loads((repo / ".enguru" / "work" / "findings" / (cid + ".json")).read_text())
    assert written == b["findings"][0]
    code, out = run_cli_raw(["bank", "finding", ".enguru/bank.json", "nope",
                             "--out", ".enguru/work/x.json"], repo)
    assert code == 2 and out["error"] == "state_corrupt"
    code, out = run_cli_raw(["bank", "finding", ".enguru/bank.json", cid, "--out", "x.json"], repo)
    assert code == 2 and out["error"] == "state_corrupt"
    assert not (repo / "x.json").exists()


def test_mutating_bank_commands_reject_wrong_shapes(tmp_path):
    repo = mk(tmp_path)
    c = base_bank()["concepts"][0]
    tm = {"modules": [{"id": "m1", "title": "t", "concepts": [c]}]}
    (repo / "tm.json").write_text(json.dumps(tm))
    (repo / "bad.json").write_text(json.dumps({"concepts": [{"id": "c1"}], "questions": "x"}))
    (repo / "list.json").write_text("[]")
    (repo / "ok.json").write_text(json.dumps(base_bank()))
    cmds = [["drop", "{p}", "--ids", "c1"], ["apply-verdicts", "{p}"],
            ["merge", "ok.json", "{p}"], ["merge", "{p}", "ok.json"],
            ["finding", "{p}", "c1", "--out", ".enguru/f.json"]]
    for p in ("tm.json", "bad.json", "list.json"):
        for cmd in cmds:
            args = ["bank"] + [a.format(p=p) for a in cmd]
            code, out = run_cli_raw(args, repo)
            assert code == 2 and out["error"] == "state_corrupt", (args, out)
    for p in ("bad.json", "list.json"):
        code, out = run_cli_raw(["bank", "seal", p], repo)
        assert code == 2 and out["error"] == "state_corrupt"
    assert run_cli(["bank", "seal", "tm.json"], repo) == {"ok": True}
    assert json.loads((repo / "ok.json").read_text()) == base_bank()
