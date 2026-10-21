#!/usr/bin/env python3
"""Eval for the decision-extractor agent.

Usage: python evals/extractor/run.py --cases evals/extractor/cases.jsonl

Cases (one labeled turn each) are batched into chunks of CASES_PER_CHUNK. Every
case contributes its context turns and its target turn to the chunk. The turn
before a target is always an assistant turn that carries `files` (it "made
edits"), so the keyword baseline's rule (prior turn has edits) can fire fairly;
the same chunk is fed to the agent and to digest.keyword_events.
Scoring is per target turn id: a turn counts as predicted-pushback if any
pushback event cites it. Prints one JSON object (pushback and keywords_pushback
precision/recall) on stdout; details go to stderr. Exits 1 if targets are missed.
"""
import argparse
import json
import random
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from enguru_lib import digest, schema  # noqa: E402

CASES_PER_CHUNK = 8
WORKERS = 6
TIMEOUT_S = 300
MODEL = "sonnet"  # orchestrating model for `claude -p`; the agent itself is haiku
TARGET_PR = 0.85
TARGET_RECALL = 0.85
TARGET_MARGIN = 0.20


def load_cases(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def build_chunks(cases, seed=7):
    """Return [(chunk, {turn_id: case})]. Order is shuffled deterministically."""
    cases = list(cases)
    random.Random(seed).shuffle(cases)
    chunks = []
    for ci in range(0, len(cases), CASES_PER_CHUNK):
        group = cases[ci:ci + CASES_PER_CHUNK]
        turns, targets = [], {}
        for case in group:
            files = ["src/%s.py" % case["id"]]
            def add(role, text, with_files):
                tid = "k%d-t%03d" % (ci // CASES_PER_CHUNK + 1, len(turns) + 1)
                turns.append({"id": tid, "role": role, "text": text,
                              "files": list(files) if with_files else [], "errors": []})
                return tid
            for c in case["context"]:
                add(c["role"], c["text"], c["role"] == "assistant")
            # target turns that are assistant narration carry files too
            tid = add(case["turn"]["role"], case["turn"]["text"],
                      case["turn"]["role"] == "assistant")
            targets[tid] = case
        chunks.append(({"chunk_id": "chunk-%02d" % (ci // CASES_PER_CHUNK + 1),
                        "session_id": "eval", "turns": turns}, targets))
    return chunks


def run_agent(chunk, workdir):
    cp = workdir / (chunk["chunk_id"] + ".json")
    op = workdir / (chunk["chunk_id"] + ".out.json")
    cp.write_text(json.dumps(chunk), encoding="utf-8")
    prompt = ("Use the enguru:decision-extractor agent with chunk_path=%s out_path=%s" % (cp, op))
    cmd = ["claude", "-p", "--plugin-dir", str(ROOT), "--permission-mode", "bypassPermissions",
           "--model", MODEL, prompt]
    last = ""
    for _attempt in range(2):
        if op.exists():
            op.unlink()
        try:
            proc = subprocess.run(cmd, cwd=str(workdir), capture_output=True, text=True,
                                  timeout=TIMEOUT_S)
            last = (proc.stdout + proc.stderr)[-300:]
        except subprocess.TimeoutExpired:
            last = "timeout"
            continue
        if op.exists():
            try:
                out = json.loads(op.read_text(encoding="utf-8"))
            except ValueError:
                last = "invalid json"
                continue
            errs = schema.validate(out, schema.load_schema("extracted"))
            if not errs:
                return out
            last = "schema: %s" % errs[:2]
    print("chunk %s failed: %s" % (chunk["chunk_id"], last), file=sys.stderr)
    return {"chunk_id": chunk["chunk_id"], "events": []}


def pushback_ids(events):
    ids = set()
    for e in events:
        if e["label"] == "pushback":
            ids.update(e["source_turn_ids"])
        # keyword events use the digest event shape
    return ids


def keyword_pushback_ids(chunk):
    ids = set()
    for e in digest.keyword_events(chunk):
        if e["label"] == "pushback":
            ids.update(e["source"]["turn_ids"])
    return ids


def pr(predicted, targets):
    tp = fp = fn = 0
    for tid, case in targets.items():
        actual = case["label"] == "pushback"
        pred = tid in predicted
        tp += actual and pred
        fp += (not actual) and pred
        fn += actual and (not pred)
    return {"precision": round(tp / (tp + fp), 3) if tp + fp else 0.0,
            "recall": round(tp / (tp + fn), 3) if tp + fn else 0.0}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True)
    ap.add_argument("--out", help="also write the result JSON here")
    args = ap.parse_args(argv)
    chunks = build_chunks(load_cases(args.cases))
    all_targets, agent_pred, kw_pred = {}, set(), set()
    with tempfile.TemporaryDirectory(prefix="enguru-eval-") as tmp:
        workdir = Path(tmp)
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            outputs = list(ex.map(lambda c: run_agent(c[0], workdir), chunks))
    for (chunk, targets), out in zip(chunks, outputs):
        all_targets.update(targets)
        valid = {t["id"] for t in chunk["turns"]}
        agent_pred |= pushback_ids([e for e in out["events"]
                                    if set(e["source_turn_ids"]) <= valid])
        kw_pred |= keyword_pushback_ids(chunk)
    result = {"pushback": pr(agent_pred, all_targets),
              "keywords_pushback": pr(kw_pred, all_targets)}
    # misses to stderr for prompt iteration
    for tid, case in sorted(all_targets.items()):
        actual, pred = case["label"] == "pushback", tid in agent_pred
        if actual != pred:
            print("%s %s: label=%s form=%s text=%r" % (
                "FN" if actual else "FP", case["id"], case["label"], case["form"],
                case["turn"]["text"]), file=sys.stderr)
    text = json.dumps(result)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    p, k = result["pushback"], result["keywords_pushback"]
    ok = (p["precision"] >= TARGET_PR and p["recall"] >= TARGET_RECALL
          and p["recall"] >= k["recall"] + TARGET_MARGIN)
    if not ok:
        print("targets missed", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
