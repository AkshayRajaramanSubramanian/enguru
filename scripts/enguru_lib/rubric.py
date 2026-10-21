"""Deterministic answer scores and skill levels. All numbers come from config/rubric.json."""
import json
from fractions import Fraction
from datetime import datetime, timezone
from pathlib import Path

from . import paths

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "rubric.json"

SKILLS = ["comprehension", "review_judgment", "defect_detection", "systems_thinking"]
SKILL_SOURCES = {
    "comprehension": ["explain_it"],
    "review_judgment": ["review_diff", "findings"],
    "defect_detection": ["spot_the_flaw", "findings"],
    "systems_thinking": ["what_if"],
}
LEVEL_ORDER = ["not_yet_rated", "novice", "developing", "proficient", "senior_ready"]


def load_config(path=None) -> dict:
    with open(path or CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _mult(table, i):
    return table[i] if isinstance(i, int) and 0 <= i < len(table) else 0


def _finding_rung(rec):
    n = rec.get("ladder_length") or 1
    rung = rec.get("ladder_rung_reached") or 1
    return min(max(rung, 1), n), n


def answer_score(rec, cfg):
    """Score of one answer record, or None when it is not scored."""
    if rec.get("kind") != "answer" or rec.get("outcome") == "skipped":
        return None
    if rec.get("question_kind") == "finding":
        if rec.get("revealed"):
            return 0.0
        rung, n = _finding_rung(rec)
        score = 1 - (rung - 1) / n
        return score * (cfg["invalid_fix_mult"] if rec.get("fix_valid") is False else 1)
    outcome = rec.get("outcome")
    if outcome == "partial" and rec.get("misconception_stated"):
        base = cfg["base"]["partial_misconception"]
    else:
        base = cfg["base"].get(outcome, 0.0)
    return (base * _mult(cfg["hint_mult"], rec.get("hints_used", 0))
            * _mult(cfg["followup_mult"], rec.get("followups_used", 0)))


def record_skills(rec):
    """The skills one answer record counts toward."""
    if rec.get("question_kind") == "finding":
        return [s for s, src in SKILL_SOURCES.items() if "findings" in src]
    for s, src in SKILL_SOURCES.items():
        if rec.get("style") in src:
            return [s]
    return [rec["skill"]] if rec.get("skill") in SKILL_SOURCES else []


def _weight(rec, cfg):
    return cfg["difficulty_weight"].get(rec.get("difficulty"), 1)


def _pct(items, cfg):
    total = sum(_weight(r, cfg) for r, _ in items)
    if not total:
        return 0.0
    return sum(s * _weight(r, cfg) for r, s in items) / total * 100


def _misconception_repeat(recs, limit):
    by = {}
    for i, r in enumerate(recs):
        by.setdefault(r["concept_id"], []).append((i, r))
    for items in by.values():
        mis = [i for i, r in items if r.get("misconception_stated")]
        if len(mis) >= limit and not any(
                r.get("outcome") == "correct" and i > mis[-1] for i, r in items):
            return True
    return False


def _proficient_unmet(items, cfg):
    p = cfg["levels"]["proficient"]
    recs = [r for r, _ in items]
    unmet = []
    n = len(items)
    if n < p["min"]:
        unmet.append("needs %d more scored answers" % (p["min"] - n)
                     if p["min"] - n != 1 else "needs 1 more scored answer")
    mid = sum(1 for r in recs if r.get("difficulty") in ("intermediate", "advanced"))
    if mid < p["min_intermediate_plus"]:
        unmet.append("needs %d more intermediate or advanced answers" % (p["min_intermediate_plus"] - mid))
    adv = sum(1 for r in recs if r.get("difficulty") == "advanced")
    if adv < p["min_advanced_attempted"]:
        unmet.append("needs at least %d advanced question attempted" % p["min_advanced_attempted"])
    pct = _pct(items, cfg)
    if pct < p["pct"]:
        unmet.append("needs weighted score of %g%% or higher (now %.1f%%)" % (p["pct"], pct))
    if _misconception_repeat(recs, p["misconception_repeat"]):
        unmet.append("a misconception was repeated without a later correct answer")
    return unmet


def _senior_unmet(items, cfg):
    s = cfg["levels"]["senior_ready"]
    recs = [r for r, _ in items]
    unmet = []
    n = len(items)
    if n < s["min"]:
        unmet.append("needs %d more scored answers" % (s["min"] - n)
                     if s["min"] - n != 1 else "needs 1 more scored answer")
    adv = [(r, sc) for r, sc in items if r.get("difficulty") == "advanced"]
    if len(adv) < s["min_advanced"]:
        unmet.append("needs %d more advanced answers" % (s["min_advanced"] - len(adv)))
    elif _pct(adv, cfg) < s["advanced_pct"]:
        unmet.append("needs %g%% or higher on advanced questions (now %.1f%%)"
                     % (s["advanced_pct"], _pct(adv, cfg)))
    ranges = {r["range_id"] for r in recs if r.get("scope") == "diff" and r.get("range_id")}
    if len(ranges) < s["min_diff_ranges"]:
        unmet.append("needs ≥%d distinct diff ranges (has %d)" % (s["min_diff_ranges"], len(ranges)))
    if s["requires_project"] and not any(r.get("scope") == "project" for r in recs):
        unmet.append("needs project-scope evidence")
    fr = []
    for r in recs:
        if r.get("question_kind") == "finding":
            rung, ln = _finding_rung(r)
            fr.append(Fraction(1) if r.get("revealed") else Fraction(rung, ln))
    if fr and sum(fr) * 2 > len(fr):
        unmet.append("findings need an average ladder rung of at most half the ladder (now %.0f%%)"
                     % (float(sum(fr)) / len(fr) * 100))
    return unmet


def _skill_level(items, cfg):
    lv = cfg["levels"]
    n = len(items)
    pct = _pct(items, cfg) if n else 0.0
    out = {"scored": n, "weighted_pct": round(pct, 4)}
    if n < lv["min_rated"]:
        k = lv["min_rated"] - n
        out.update(level="not_yet_rated",
                   reasons=["needs %d more scored answer%s" % (k, "" if k == 1 else "s")])
        return out
    prof = _proficient_unmet(items, cfg)
    senior = _senior_unmet(items, cfg)
    if not prof and not senior:
        out.update(level="senior_ready", reasons=[])
    elif not prof:
        out.update(level="proficient", reasons=senior)
    elif pct >= lv["developing_pct"]:
        out.update(level="developing", reasons=prof)
    else:
        out.update(level="novice",
                   reasons=["needs weighted score of %g%% or higher (now %.1f%%)"
                            % (lv["developing_pct"], pct)])
    return out


def windowed(records, cfg) -> dict:
    """Per skill, the (record, score) pairs a level is computed from: the most
    recent `window` scored answers, oldest first."""
    per = {s: [] for s in SKILLS}
    for rec in records:
        score = answer_score(rec, cfg)
        if score is None:
            continue
        for s in record_skills(rec):
            per[s].append((rec, score))
    for s, items in per.items():
        items.sort(key=lambda it: it[0].get("timestamp", ""))
        per[s] = items[-cfg["window"]:]
    return per


def skill_levels(records, cfg) -> dict:
    return {s: _skill_level(items, cfg) for s, items in windowed(records, cfg).items()}


EVIDENCE_FIELDS = ("concept_id", "session_id", "scope", "range_id", "question_kind", "style",
                   "difficulty", "outcome", "misconception_stated", "ladder_rung_reached",
                   "revealed")


def skill_evidence(records, cfg) -> dict:
    """Per skill, the windowed answer records a level was computed from, trimmed
    to the fields the certificate narrative cites."""
    return {s: [{k: r.get(k) for k in EVIDENCE_FIELDS} for r, _ in items]
            for s, items in windowed(records, cfg).items()}


def _discovered_at(answers, cid):
    """Ladder step at which the developer last worked a finding out, or None."""
    rung = None
    for a in sorted(answers, key=lambda a: a.get("timestamp", "")):
        if (a.get("kind") == "answer" and a.get("question_kind") == "finding"
                and a.get("concept_id") == cid and a.get("outcome") != "skipped"
                and not a.get("revealed")):
            rung = a.get("ladder_rung_reached") or 1
    return rung


# ------------------------------------------------------------------ CLI helper

def _bank_findings(repo):
    out = []
    root = Path(repo) / paths.STATE_DIRNAME / "banks"
    for p in sorted(root.glob("**/*.json")) if root.is_dir() else []:
        try:
            data = paths.read_json(p)
        except (paths.StateCorrupt, OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.extend(f for f in data.get("findings", []) if isinstance(f, dict))
    return out


def _previous(cert_dir, today):
    names = sorted(p for p in cert_dir.glob("*.json")
                   if len(p.stem) == 10 and p.stem != today) if cert_dir.is_dir() else []
    for p in reversed(names):
        try:
            data = paths.read_json(p)
        except (paths.StateCorrupt, OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("skills"), dict):
            return data["skills"]
    return None


def cert_levels(repo, save=False, today=None) -> dict:
    from . import progress
    repo = Path(repo)
    cfg = load_config()
    prog = progress.load(repo)
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    scored = [a for a in prog["answers"] if answer_score(a, cfg) is not None]
    bank_f = {}
    for f in _bank_findings(repo):
        if f.get("verdict") == "confirmed" and f.get("concept_id"):
            bank_f[f["concept_id"]] = f
    findings = []
    for cid, st in prog["findings"].items():
        ladder = bank_f.get(cid, {}).get("ladder")
        findings.append({"concept_id": cid, "status": st.get("status", "open"),
                         "rung": st.get("rung", 1),
                         "ladder_length": len(ladder) if isinstance(ladder, list) else None,
                         "discovered_at": _discovered_at(prog["answers"], cid)})
    for cid, f in bank_f.items():  # confirmed but never started: open
        if cid not in prog["findings"]:
            ladder = f.get("ladder")
            findings.append({"concept_id": cid, "status": "open", "rung": 1,
                             "ladder_length": len(ladder) if isinstance(ladder, list) else None,
                             "discovered_at": None})
    open_critical = sum(
        1 for cid, f in bank_f.items()
        if f.get("severity") == "critical"
        and prog["findings"].get(cid, {}).get("status", "open") in ("open", "in_progress")
        and not prog["findings"].get(cid, {}).get("explained"))
    out = {"skills": skill_levels(prog["answers"], cfg),
           "evidence": skill_evidence(prog["answers"], cfg),
           "previous": _previous(repo / paths.STATE_DIRNAME / "certificates", today),
           "scopes": {"project": any(a.get("scope") == "project" for a in scored),
                      "diff_ranges": sorted({a["range_id"] for a in scored
                                             if a.get("scope") == "diff" and a.get("range_id")})},
           "findings": findings, "revisit_count": len(prog["revisit"]),
           "open_critical": open_critical}
    if save:
        cert = paths.state_dir(repo) / "certificates"
        cert.mkdir(parents=True, exist_ok=True)
        saved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        paths.atomic_write_json(cert / ("%s.json" % today),
                                dict(out, date=today, saved_at=saved_at))
    return out
