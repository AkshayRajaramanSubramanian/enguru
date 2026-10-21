#!/usr/bin/env python3
"""Eval for the finding-verifier agent on the seeded repo.

Usage: python3 evals/run_verifier.py

Builds the seeded repo, writes each finding to .enguru/work/findings/<concept_id>.json
(as `bank finding` would) and runs the enguru:finding-verifier agent in verify mode
through `claude -p --plugin-dir <repo>`, one call per finding, in parallel:
  - evals/planted-findings.json: the 5 planted bugs, stated correctly (true);
  - evals/false-findings.json: 7 plausible but wrong findings (the code prevents each).
    Two are half-true: the smell is real (an unchecked header slice, a field that looks
    unloaded) but code in another file makes the consequence impossible (requireAuth
    rejects a missing header; findBySessionToken selects stripe_customer_id).
A verdict must be schema-valid with the right concept_id and mode; a missing or invalid
verdict counts as a harness failure.

Prints {"false_confirmed", "false_total", "planted_confirmed", "planted_total",
"planted_confirmed_rate", "missing_verdicts", "verdicts"}. Exits 1 unless
false_confirmed == 0 and missing_verdicts == 0 (planted confirmation is reported, aim >= 4/5).
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

sys.path.insert(0, str(common.ROOT / "scripts"))
from enguru_lib import schema  # noqa: E402

TIMEOUT_S = 600
TOOLS = "Task Agent Read Grep Glob Write"
EVALS = common.ROOT / "evals"


def verdict_of(repo, cid):
    p = repo / ".enguru" / "work" / "verdicts" / ("%s.json" % cid)
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if schema.validate(v, schema.load_schema("verdict")) or v["concept_id"] != cid \
            or v["mode"] != "verify":
        return None
    return v


def main():
    planted = json.loads((EVALS / "planted-findings.json").read_text(encoding="utf-8"))
    false = json.loads((EVALS / "false-findings.json").read_text(encoding="utf-8"))
    cases = [(f, True) for f in planted] + [(f, False) for f in false]
    with tempfile.TemporaryDirectory(prefix="enguru-verifier-") as tmp:
        repo = common.build_seeded_repo(Path(tmp) / "shopfront")
        (repo / ".enguru" / "work" / "findings").mkdir(parents=True)
        (repo / ".enguru" / "work" / "verdicts").mkdir(parents=True)
        for f, _ in cases:
            (repo / ".enguru" / "work" / "findings" / ("%s.json" % f["concept_id"])).write_text(
                json.dumps(f), encoding="utf-8")

        def one(case):
            cid = case[0]["concept_id"]
            prompt = ("Use the enguru:finding-verifier agent with mode=verify "
                      "finding_json=.enguru/work/findings/%s.json "
                      "out_path=.enguru/work/verdicts/%s.json" % (cid, cid))
            common.claude(prompt, repo, TOOLS, TIMEOUT_S,
                          accept=lambda _o: verdict_of(repo, cid) is not None)
            return verdict_of(repo, cid)

        verdicts = common.pmap(one, cases)
    out, false_confirmed, planted_confirmed, missing = {}, 0, 0, 0
    for (f, is_true), v in zip(cases, verdicts):
        cid = f["concept_id"]
        if v is None:
            missing += 1
            out[cid] = "missing"
            print("MISSING %s" % cid, file=sys.stderr)
            continue
        out[cid] = v["verdict"]
        if is_true and v["verdict"] == "confirmed":
            planted_confirmed += 1
        if not is_true and v["verdict"] == "confirmed":
            false_confirmed += 1
        print("%s %s -> %s: %s" % ("PLANTED" if is_true else "FALSE", cid, v["verdict"],
                                    v["reason"]), file=sys.stderr)
    result = {"false_confirmed": false_confirmed, "false_total": len(false),
              "planted_confirmed": planted_confirmed, "planted_total": len(planted),
              "planted_confirmed_rate": round(planted_confirmed / len(planted), 3),
              "missing_verdicts": missing, "verdicts": out}
    return common.emit("verifier", result, false_confirmed == 0 and missing == 0)


if __name__ == "__main__":
    sys.exit(main())
