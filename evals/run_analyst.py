#!/usr/bin/env python3
"""Eval for the project-analyst agent: recall of planted bugs in the seeded repo.

Usage: python3 evals/run_analyst.py [--keep DIR]

Builds the seeded repo (evals/seeded-repo, real git history) in a temp dir, runs
`digest prep --project` + `digest merge` (no transcripts), then follows the interview
skill's build steps through `claude -p --plugin-dir <repo>`:
  topic_map -> bank validate (one re-dispatch with the problems) -> seal -> promote;
  module mode for every module in parallel -> validate (one re-dispatch) -> drop the
  ids still failing, as the skill does.

Matching rule: a planted bug counts as found if any finding in a module bank (any
verdict; all are "pending" here) both
  - has lines.file equal to the planted file and [start, end] overlapping the planted
    line range widened by TOLERANCE lines on each side, and
  - has a `consequence` matching the bug's `match_terms` regex (case-insensitive),
or does the same for one of the bug's `also` locations, each with its own match_terms.

Prints {"planted_found", "planted_total", "recall", "invalid_items", "modules",
"findings", "missed", "matches", "finding_list"}: matches maps each planted id to the
matching finding's location and the matched term; finding_list holds every finding's
{file, start, end, consequence}; invalid_items counts validation problems still present after the
re-dispatch (before any drop). Exits 1 if recall < 0.8 or the topic map never validates.
"""
import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

TARGET_RECALL = 0.8
TOLERANCE = 3
TIMEOUT_S = 1500
TOOLS = "Task Agent Read Grep Glob Write Bash(git *)"
DIGEST = ".enguru/digest.json"
KEYLOG = ".enguru/keylog.jsonl"
TOPIC = ".enguru/banks/project/topic-map.json"


def _file_ok(path):
    def accept(_stdout):
        try:
            json.loads(Path(path).read_text(encoding="utf-8"))
            return True
        except (OSError, ValueError):
            return False
    return accept


def build(repo, fields, out_rel):
    """Dispatch the analyst, validate, re-dispatch once with the problems.
    Returns (data or None, problems after the re-dispatch)."""
    out = repo / out_rel
    base = "Use the enguru:project-analyst agent with " + " ".join(
        "%s=%s" % kv for kv in fields + [("digest_path", DIGEST), ("out_path", out_rel),
                                         ("keylog_path", KEYLOG)])
    prompt = base
    problems = [{"item_id": "$", "reason": "not run"}]
    for attempt in range(2):
        if out.exists():
            out.unlink()
        common.claude(prompt, repo, TOOLS, TIMEOUT_S, accept=_file_ok(out))
        if not out.exists():
            problems = [{"item_id": "$", "reason": "no output file"}]
        else:
            res = common.enguru(repo, "bank", "validate", out_rel)
            problems = res.get("problems", [{"item_id": "$", "reason": res.get("message", "?")}])
        if not problems:
            break
        prompt = (base + "\n\nYour previous output at out_path failed validation. Fix these items "
                  "and rewrite the whole file:\n" + json.dumps(problems))
    if not out.exists():
        return None, problems
    try:
        return json.loads(out.read_text(encoding="utf-8")), problems
    except ValueError:
        return None, problems


def overlaps(finding, loc):
    L = finding.get("lines") or {}
    a, b = loc["lines"]
    return (L.get("file") == loc["file"] and isinstance(L.get("start"), int)
            and isinstance(L.get("end"), int)
            and L["start"] <= b + TOLERANCE and L["end"] >= a - TOLERANCE)


def match_finding(finding, planted):
    """Return {"file", "lines", "term"} for the location of `planted` this finding hits
    (overlap plus a match_terms hit in its consequence), or None."""
    locs = [{"file": planted["file"], "lines": planted["lines"],
             "match_terms": planted["match_terms"]}] + planted.get("also", [])
    text = str(finding.get("consequence", ""))
    for loc in locs:
        if not overlaps(finding, loc):
            continue
        m = re.search(loc["match_terms"], text, re.IGNORECASE)
        if m:
            return {"file": loc["file"], "lines": loc["lines"], "term": m.group(0)}
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", help="build the repo here and keep it (for debugging)")
    args = ap.parse_args(argv)
    planted = common.load_planted()
    tmp = None
    if args.keep:
        repo = Path(args.keep)
    else:
        tmp = tempfile.TemporaryDirectory(prefix="enguru-analyst-")
        repo = Path(tmp.name) / "shopfront"
    common.build_seeded_repo(repo)
    empty = repo.parent / "no-transcripts"
    empty.mkdir(exist_ok=True)
    common.enguru(repo, "digest", "prep", "--project", "--transcripts-root", str(empty))
    common.enguru(repo, "digest", "merge")

    invalid = 0
    tm, problems = build(repo, [("mode", "topic_map")], ".enguru/work/build-topic-map.json")
    if problems or tm is None:
        print("topic map failed: %s" % problems, file=sys.stderr)
        return common.emit("analyst", {"planted_found": 0, "planted_total": len(planted),
                                       "recall": 0.0, "invalid_items": len(problems),
                                       "error": "topic_map_invalid"}, False)
    common.enguru(repo, "bank", "seal", ".enguru/work/build-topic-map.json")
    res = common.enguru(repo, "bank", "promote", ".enguru/work/build-topic-map.json", TOPIC)
    assert res.get("ok"), res
    modules = [m["id"] for m in tm["modules"]]

    def one(mid):
        out_rel = ".enguru/work/build-%s.json" % mid
        data, probs = build(repo, [("mode", "module"), ("module_id", mid),
                                   ("topic_map_path", TOPIC)], out_rel)
        if data is not None and probs and all(p["item_id"] != "$" for p in probs):
            common.enguru(repo, "bank", "drop", out_rel,
                          "--ids", ",".join(sorted({p["item_id"] for p in probs})))
            res = common.enguru(repo, "bank", "validate", out_rel)
            data = json.loads((repo / out_rel).read_text(encoding="utf-8")) if res.get("ok") else None
        elif probs:
            data = None
        return mid, data, probs

    results = common.pmap(one, modules)
    findings = []
    for mid, data, probs in results:
        invalid += len(probs)
        for p in probs:
            print("INVALID %s: %s %s" % (mid, p["item_id"], p["reason"]), file=sys.stderr)
        if data is None:
            print("MODULE %s: no valid bank" % mid, file=sys.stderr)
            continue
        for f in data.get("findings", []):
            findings.append(f)
            L = f["lines"]
            print("FINDING %s %s:%s-%s %s" % (mid, L["file"], L["start"], L["end"],
                                             f["consequence"][:160]), file=sys.stderr)
    found, missed, matches = 0, [], {}
    for p in planted:
        hit = None
        for f in findings:
            m = match_finding(f, p)
            if m:
                L = f["lines"]
                hit = {"finding": "%s:%s-%s" % (L["file"], L["start"], L["end"]),
                       "planted_location": "%s:%s-%s" % (m["file"], m["lines"][0], m["lines"][1]),
                       "term": m["term"]}
                break
        if hit:
            found += 1
            matches[p["id"]] = hit
        else:
            missed.append(p["id"])
    recall = round(found / len(planted), 3)
    result = {"planted_found": found, "planted_total": len(planted), "recall": recall,
              "invalid_items": invalid, "modules": len(modules), "findings": len(findings),
              "missed": missed, "matches": matches,
              "finding_list": [{"file": f["lines"]["file"], "start": f["lines"]["start"],
                                "end": f["lines"]["end"], "consequence": f["consequence"]}
                               for f in findings]}
    if tmp:
        tmp.cleanup()
    return common.emit("analyst", result, recall >= TARGET_RECALL)


if __name__ == "__main__":
    sys.exit(main())
