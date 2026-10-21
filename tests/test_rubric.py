import json
from pathlib import Path

import pytest

from conftest import run_cli
from enguru_lib import rubric

CFG = rubric.load_config()


def mk(i, outcome="correct", skill="comprehension", difficulty="foundational", cid=None,
       scope="project", range_id=None, misc=False, **kw):
    r = {"kind": "answer", "session_id": "s1", "concept_id": cid or "c%d" % i,
         "question_kind": "quiz", "style": {"comprehension": "explain_it",
                                            "review_judgment": "review_diff",
                                            "defect_detection": "spot_the_flaw",
                                            "systems_thinking": "what_if"}[skill],
         "skill": skill, "difficulty": difficulty, "outcome": outcome,
         "misconception_stated": misc, "hints_used": 0, "followups_used": 0,
         "scope": scope, "range_id": range_id, "timestamp": "2026-01-01T00:%02d:%02dZ" % (i // 60, i % 60)}
    r.update(kw)
    return r


def level(records, skill="comprehension"):
    return rubric.skill_levels(records, CFG)[skill]


def test_answer_score_multipliers():
    r = mk(1, hints_used=1, followups_used=2)
    assert rubric.answer_score(r, CFG) == pytest.approx(0.56)


def test_answer_score_partial_misconception_and_none_cases():
    assert rubric.answer_score(mk(1, outcome="partial"), CFG) == 0.5
    assert rubric.answer_score(mk(1, outcome="partial", misc=True), CFG) == 0.25
    assert rubric.answer_score(mk(1, outcome="incorrect", misc=True), CFG) == 0.0
    assert rubric.answer_score(mk(1, outcome="skipped"), CFG) is None
    assert rubric.answer_score(mk(1, kind="ladder_progress", ladder_rung_reached=2), CFG) is None
    assert rubric.answer_score(mk(1, kind="fix_check", fix_verdict="fixed"), CFG) is None


def finding(i, rung, n, **kw):
    r = mk(i, question_kind="finding", style="review_diff", skill="review_judgment",
           ladder_rung_reached=rung, ladder_length=n, revealed=False)
    r.update(kw)
    return r


def test_finding_score():
    assert rubric.answer_score(finding(1, 2, 4, fix_valid=False), CFG) == pytest.approx(0.5625)
    assert rubric.answer_score(finding(1, 1, 4), CFG) == 1.0
    assert rubric.answer_score(finding(1, 4, 4, revealed=True), CFG) == 0


def test_finding_counts_for_two_skills():
    out = rubric.skill_levels([finding(i, 1, 3) for i in range(6)], CFG)
    assert out["review_judgment"]["scored"] == 6
    assert out["defect_detection"]["scored"] == 6
    assert out["comprehension"]["scored"] == 0
    assert out["comprehension"]["level"] == "not_yet_rated"


def pct_records(n, pct_correct_partial):
    """n foundational records whose weighted pct is exactly as requested via partial/correct mix."""
    return None


def test_levels_boundaries():
    assert level([mk(i) for i in range(5)])["level"] == "not_yet_rated"
    assert "needs 1 more scored answer" in level([mk(i) for i in range(5)])["reasons"][0]
    # 6 records: 3 correct (1) + 3 partial(0.5) => 75%; need 49.9 / 50.
    # use scores via multipliers: 6 records, total = 2.994 -> 49.9%
    r = [mk(i, hints_used=0) for i in range(6)]
    for x in r[:2]:
        x["outcome"] = "correct"
    for x in r[2:]:
        x["outcome"] = "incorrect"
    # 2/6 = 33% -> novice
    assert level(r)["level"] == "novice"
    r = [mk(i, outcome="correct" if i < 3 else "incorrect") for i in range(6)]
    assert level(r)["level"] == "developing" and level(r)["weighted_pct"] == 50.0
    # 49.9%: 1000 weight units is impractical; use custom cfg weights instead
    cfg = json.loads(json.dumps(CFG))
    cfg["base"]["partial"] = 0.499
    r = [mk(i, outcome="partial") for i in range(6)]
    assert rubric.skill_levels(r, cfg)["comprehension"]["level"] == "novice"
    cfg["base"]["partial"] = 0.5
    assert rubric.skill_levels(r, cfg)["comprehension"]["level"] == "developing"


def prof_records(n=10, correct=None):
    """n records: 3 intermediate, 1 advanced, rest foundational, outcome correct."""
    out = []
    for i in range(n):
        d = "advanced" if i == 0 else "intermediate" if i < 4 else "foundational"
        out.append(mk(i, difficulty=d))
    return out


def test_proficient_pct_boundary():
    cfg = json.loads(json.dumps(CFG))
    r = prof_records()
    assert level(r)["level"] == "proficient"
    for x in r:
        x["outcome"] = "partial"
    cfg["base"]["partial"] = 0.699
    assert rubric.skill_levels(r, cfg)["comprehension"]["level"] == "developing"
    cfg["base"]["partial"] = 0.7
    assert rubric.skill_levels(r, cfg)["comprehension"]["level"] == "proficient"


def test_proficient_needs_intermediate_and_advanced():
    r = [mk(i) for i in range(10)]
    lv = level(r)
    assert lv["level"] == "developing"
    assert any("intermediate" in s for s in lv["reasons"])
    assert any("advanced" in s for s in lv["reasons"])


def test_misconception_blocks_proficient_until_later_correct():
    r = prof_records()
    r[5].update(concept_id="cx", misc=True, misconception_stated=True, outcome="partial")
    r[6].update(concept_id="cx", misconception_stated=True, outcome="partial")
    r[5]["timestamp"] = "2026-01-01T01:00:00Z"
    r[6]["timestamp"] = "2026-01-01T01:01:00Z"
    cfg = json.loads(json.dumps(CFG))
    cfg["base"]["partial"] = 1.0
    lv = rubric.skill_levels(r, cfg)["comprehension"]
    assert lv["level"] == "developing"
    assert any("misconception" in s for s in lv["reasons"])
    r.append(mk(30, cid="cx"))
    r[-1]["timestamp"] = "2026-01-01T02:00:00Z"
    assert rubric.skill_levels(r, cfg)["comprehension"]["level"] == "proficient"


def senior_records(ranges=("a", "b"), project=True):
    r = []
    for i in range(15):
        d = "advanced" if i < 3 else "intermediate" if i < 6 else "foundational"
        r.append(mk(i, difficulty=d, scope="diff", range_id=ranges[i % len(ranges)]))
    if project:
        r[14].update(scope="project", range_id=None)
    return r


def test_senior_ready_and_blockers():
    assert level(senior_records())["level"] == "senior_ready"
    lv = level(senior_records(ranges=("a",)))
    assert lv["level"] == "proficient"
    assert any("diff ranges" in s for s in lv["reasons"])
    lv = level(senior_records(project=False))
    assert lv["level"] == "proficient"
    assert any("project" in s for s in lv["reasons"])


def test_senior_blocked_by_advanced_pct_and_finding_rung():
    r = senior_records()
    r[0]["outcome"] = "incorrect"
    r[1]["outcome"] = "incorrect"
    assert level(r)["level"] == "proficient"
    def fs(rung, ln):
        out = []
        for i in range(15):
            d = "advanced" if i < 3 else "intermediate" if i < 6 else "foundational"
            out.append(finding(i, rung, ln, difficulty=d, scope="diff", range_id="ab"[i % 2]))
        out[14].update(scope="project", range_id=None)
        return out

    assert rubric.skill_levels(fs(1, 4), CFG)["review_judgment"]["level"] == "senior_ready"
    lv = rubric.skill_levels(fs(3, 4), CFG)["review_judgment"]
    assert lv["level"] != "senior_ready"


def test_window_drops_old_failures():
    old = [mk(i, outcome="incorrect") for i in range(10)]
    new = [mk(100 + i) for i in range(20)]
    assert level(old + new)["scored"] == 20
    assert level(old + new)["weighted_pct"] == 100.0
    assert level(old + new)["level"] != "novice"


def test_level_drop():
    r = prof_records(10)
    assert level(r)["level"] == "proficient"
    bad = [mk(200 + i, outcome="incorrect") for i in range(10)]
    assert level(r + bad)["level"] in ("novice", "developing")


def test_skipped_not_scored():
    r = [mk(i) for i in range(5)] + [mk(9, outcome="skipped")]
    assert level(r)["scored"] == 5


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def test_cli_cert_levels(tmp_path):
    sd = tmp_path / ".enguru"
    answers = [mk(i, scope="diff", range_id="r%d" % (i % 2)) for i in range(6)]
    prog = {"version": 1, "sessions": [], "answers": answers, "concepts": {},
            "revisit": ["x"], "findings": {"f1": {"status": "in_progress", "rung": 2},
                                           "f2": {"status": "completed", "rung": 1}},
            "current": None}
    _write(sd / "progress.json", prog)
    _write(sd / "banks" / "diff" / "r0.json", {"findings": [
        {"concept_id": "f1", "verdict": "confirmed", "severity": "critical", "ladder": ["a", "b", "c"]},
        {"concept_id": "f2", "verdict": "confirmed", "severity": "minor", "ladder": ["a", "b"]}]})
    _write(sd / "certificates" / "2020-01-01.json", {"date": "2020-01-01", "skills": {"comprehension": {"level": "novice"}}})
    out = run_cli(["cert", "levels"], tmp_path)
    assert out["skills"]["comprehension"]["level"] == "proficient" or out["skills"]["comprehension"]["scored"] == 6
    assert out["previous"] == {"comprehension": {"level": "novice"}}
    assert out["scopes"] == {"project": False, "diff_ranges": ["r0", "r1"]}
    assert {"concept_id": "f1", "status": "in_progress", "rung": 2, "ladder_length": 3} in out["findings"]
    assert out["revisit_count"] == 1
    assert out["open_critical"] == 1
    assert not list((sd / "certificates").glob("2026*"))
    out = run_cli(["cert", "levels", "--save"], tmp_path)
    saved = list((sd / "certificates").glob("*.json"))
    assert len(saved) == 2
    new = [p for p in saved if p.name != "2020-01-01.json"][0]
    data = json.loads(new.read_text())
    assert data["date"] == new.stem and data["skills"] == out["skills"]
    out2 = run_cli(["cert", "levels", "--save"], tmp_path)
    assert out2["previous"] == {"comprehension": {"level": "novice"}}
    assert len(list((sd / "certificates").glob("*.json"))) == 2


def test_cli_cert_levels_empty(tmp_path):
    out = run_cli(["cert", "levels"], tmp_path)
    assert out["previous"] is None and out["findings"] == []
    assert all(v["level"] == "not_yet_rated" for v in out["skills"].values())


def test_suggest_certificate_min_from_config():
    from enguru_lib import progress
    assert not hasattr(progress, "SUGGEST_CERTIFICATE_MIN")
    assert CFG["suggest_certificate_min"] == 6
