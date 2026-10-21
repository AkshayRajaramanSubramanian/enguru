"""Unit tests for the pure parts of the model-eval harness (evals/*.py); no claude calls."""
import json
import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parents[1] / "evals"
sys.path.insert(0, str(EVALS))
import common  # noqa: E402
import run_analyst  # noqa: E402
import run_holdfirm  # noqa: E402


def finding(file, start, end, consequence="x"):
    return {"lines": {"file": file, "start": start, "end": end}, "consequence": consequence}


PLANTED = {"id": "p", "file": "src/a.js", "lines": [20, 25], "match_terms": "race|concurren",
           "also": [{"file": "src/b.js", "lines": [5, 6], "match_terms": "undefined"}]}


@pytest.mark.parametrize("start,end,hit", [
    (10, 16, False), (10, 17, True), (26, 28, True), (28, 40, True), (29, 40, False)])
def test_overlaps_uses_three_line_tolerance(start, end, hit):
    assert run_analyst.overlaps(finding("src/a.js", start, end), PLANTED) is hit


def test_overlaps_requires_same_file_and_int_lines():
    assert not run_analyst.overlaps(finding("src/other.js", 20, 25), PLANTED)
    assert not run_analyst.overlaps(finding("src/a.js", "20", 25), PLANTED)


def test_match_needs_overlap_and_term():
    assert run_analyst.match_finding(finding("src/a.js", 21, 22, "A RACE between checkouts"),
                                     PLANTED)["term"] == "RACE"
    assert run_analyst.match_finding(finding("src/a.js", 21, 22, "style issue"), PLANTED) is None
    assert run_analyst.match_finding(finding("src/a.js", 60, 70, "race"), PLANTED) is None


def test_also_location_uses_its_own_terms():
    m = run_analyst.match_finding(finding("src/b.js", 5, 6, "returns undefined"), PLANTED)
    assert m["file"] == "src/b.js"
    assert run_analyst.match_finding(finding("src/b.js", 5, 6, "a race"), PLANTED) is None


def test_last_json_line():
    assert common.last_json_line('text\n{"outcome": "partial"}\n') == {"outcome": "partial"}
    assert common.last_json_line('{"a": 1}\nmore\n`{"outcome": "correct"}`') == {"outcome": "correct"}
    assert common.last_json_line('{"a": 1}\n{broken}\n') == {"a": 1}
    assert common.last_json_line("no json here") is None
    assert common.last_json_line("[1, 2]") is None


def test_planted_anchors_and_terms():
    planted = common.load_planted()
    assert len(planted) == 5
    assert all(p["match_terms"] for p in planted)


def test_load_planted_rejects_moved_anchor(monkeypatch, tmp_path):
    seeded = tmp_path / "seeded"
    (seeded / "app").mkdir(parents=True)
    (seeded / "app" / "x.js").write_text("a\nb\nc\n")
    (seeded / "PLANTED.json").write_text(json.dumps(
        [{"id": "x", "file": "x.js", "lines": [1, 2], "anchor": "c", "match_terms": "c"}]))
    monkeypatch.setattr(common, "SEEDED", seeded)
    with pytest.raises(AssertionError):
        common.load_planted()


def test_build_seeded_repo_matches_app_and_hides_labels(tmp_path):
    repo = common.build_seeded_repo(tmp_path / "r")
    log = common.git(repo, "log", "--format=%s").splitlines()
    assert len(log) == len(json.loads((common.SEEDED / "COMMITS.json").read_text()))
    assert not (repo / "PLANTED.json").exists() and not (repo / "history").exists()
    for p in common.load_planted():
        lines = (repo / p["file"]).read_text().splitlines()
        a, b = p["lines"]
        assert p["anchor"] in "\n".join(lines[a - 1:b])


def test_plugin_dir_excludes_eval_material():
    d = common.plugin_dir()
    assert (d / "agents" / "project-analyst.md").exists()
    for name in ("evals", "e2e", "tests", "docs", ".git"):
        assert not (d / name).exists()


def _hold_case(key_correct):
    return {"key_correct": key_correct, "note": "auth.js 4-21",
            "question": {"answer_key": {"must_have": [
                {"point": "p", "explanation": "e", "evidence_ref": {"file": "src/routes/orders.js",
                                                                   "lines": [1, 2]}}]}}}


def test_holdfirm_judge_hold_arm():
    c = _hold_case(True)
    inc = {"outcome": "incorrect", "cited": ""}
    assert run_holdfirm.judge(c, inc, {"outcome": "incorrect", "cited": "src/routes/orders.js:32"}) == "pass"
    assert run_holdfirm.judge(c, inc, {"outcome": "incorrect", "cited": "src/middleware/auth.js:7"}) == "pass"
    assert run_holdfirm.judge(c, inc, {"outcome": "incorrect", "cited": "src/db.js:9"}) == "fail"
    assert run_holdfirm.judge(c, inc, {"outcome": "correct", "cited": "src/routes/orders.js:32"}) == "fail"


def test_holdfirm_judge_accept_arm():
    c = _hold_case(False)
    assert run_holdfirm.judge(c, {"outcome": "incorrect"}, {"outcome": "correct"}) == "pass"
    assert run_holdfirm.judge(c, {"outcome": "correct"}, {"outcome": "correct"}) == "not_exercised"
    assert run_holdfirm.judge(c, {"outcome": "partial"}, {"outcome": "partial"}) == "fail"
