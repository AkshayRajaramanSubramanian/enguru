#!/usr/bin/env python3
"""Enguru CLI entry: version gate + argparse dispatch."""
import json
import sys

if sys.version_info < (3, 9):
    print(json.dumps({
        "error": "python_too_old",
        "message": "Enguru needs Python 3.9+. Install it from https://www.python.org/downloads/",
    }))
    sys.exit(2)

import argparse
import os
import subprocess
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def emit(obj):
    print(json.dumps(obj))


def fail(code, msg):
    print(json.dumps({"error": code, "message": msg}))
    sys.exit(2)


def _is_git_repo(cwd):
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd, capture_output=True, text=True,
        )
    except OSError:
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


def cmd_check(args):
    emit({
        "ok": True,
        "python": "%d.%d.%d" % sys.version_info[:3],
        "git": _is_git_repo(os.getcwd()),
        "transcripts_dir": (Path.home() / ".claude" / "projects").is_dir(),
    })


def _repo_root(cwd):
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                           capture_output=True, text=True)
    except OSError:
        return Path(cwd)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else Path(cwd)


def cmd_digest_prep(args):
    from enguru_lib import digest
    repo = _repo_root(os.getcwd())
    root = Path(args.transcripts_root) if args.transcripts_root else Path.home() / ".claude" / "projects"
    try:
        scope = digest.resolve_scope(repo, not args.diff, args.range, args.uncommitted)
        emit(digest.prep(repo, scope, root))
    except digest.DigestError as e:
        fail(e.code, e.message)


def cmd_digest_merge(args):
    from enguru_lib import digest
    repo = _repo_root(os.getcwd())
    try:
        emit(digest.merge(repo))
    except digest.DigestError as e:
        fail(e.code, e.message)


def _bank_path(repo, raw):
    p = Path(raw)
    return p if p.is_absolute() else repo / p


def _load_bank_json(path):
    from enguru_lib import paths
    try:
        return paths.read_json(path)
    except FileNotFoundError:
        fail("state_corrupt", "file not found: %s" % path)
    except (paths.StateCorrupt, OSError, ValueError) as e:
        fail("state_corrupt", str(e))


def cmd_bank_validate(args):
    from enguru_lib import bank, paths
    repo = _repo_root(os.getcwd())
    path = _bank_path(repo, args.path)
    data = _load_bank_json(path)
    digest = {"events": []}
    dp = repo / ".enguru" / "digest.json"
    if dp.exists():
        try:
            digest = paths.read_json(dp)
        except (paths.StateCorrupt, OSError, ValueError):
            digest = {"events": []}
    problems = bank.validate_bank(repo, data, digest)
    emit({"ok": not problems, "problems": problems})


def cmd_bank_seal(args):
    from enguru_lib import bank
    repo = _repo_root(os.getcwd())
    path = _bank_path(repo, args.path)
    _load_bank_json(path)
    bank.seal(repo, path)
    emit({"ok": True})


def cmd_bank_drop(args):
    from enguru_lib import bank
    repo = _repo_root(os.getcwd())
    path = _bank_path(repo, args.path)
    _load_bank_json(path)
    ids = [i for i in args.ids.split(",") if i]
    bank.drop_items(path, ids)
    emit({"ok": True, "dropped": ids})


def cmd_bank_apply_verdicts(args):
    from enguru_lib import bank
    repo = _repo_root(os.getcwd())
    path = _bank_path(repo, args.path)
    _load_bank_json(path)
    emit(bank.apply_verdicts(repo, path))


def build_parser():
    p = argparse.ArgumentParser(prog="enguru")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="environment check").set_defaults(func=cmd_check)
    dg = sub.add_parser("digest", help="digest commands").add_subparsers(dest="dcmd", required=True)
    pp = dg.add_parser("prep", help="write transcript chunks and rule events")
    mode = pp.add_mutually_exclusive_group(required=True)
    mode.add_argument("--project", action="store_true")
    mode.add_argument("--diff", action="store_true")
    pp.add_argument("--range")
    pp.add_argument("--uncommitted", action="store_true")
    pp.add_argument("--transcripts-root", help=argparse.SUPPRESS)
    pp.set_defaults(func=cmd_digest_prep)
    mg = dg.add_parser("merge", help="merge extractor output and rule events into the digest")
    mg.set_defaults(func=cmd_digest_merge)
    bk = sub.add_parser("bank", help="question bank commands").add_subparsers(dest="bcmd", required=True)
    v = bk.add_parser("validate", help="validate a bank or topic map")
    v.add_argument("path")
    v.set_defaults(func=cmd_bank_validate)
    sl = bk.add_parser("seal", help="store evidence hashes")
    sl.add_argument("path")
    sl.set_defaults(func=cmd_bank_seal)
    dr = bk.add_parser("drop", help="drop questions or concepts by id")
    dr.add_argument("path")
    dr.add_argument("--ids", required=True)
    dr.set_defaults(func=cmd_bank_drop)
    av = bk.add_parser("apply-verdicts", help="apply finding-verifier verdicts")
    av.add_argument("path")
    av.set_defaults(func=cmd_bank_apply_verdicts)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
