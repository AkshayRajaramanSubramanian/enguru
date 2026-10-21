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


def _transcripts_root(flag=None):
    """--transcripts-root, else $ENGURU_TRANSCRIPTS_ROOT, else ~/.claude/projects."""
    raw = flag or os.environ.get("ENGURU_TRANSCRIPTS_ROOT")
    return Path(raw) if raw else Path.home() / ".claude" / "projects"


def cmd_check(args):
    emit({
        "ok": True,
        "python": "%d.%d.%d" % sys.version_info[:3],
        "git": _is_git_repo(os.getcwd()),
        "transcripts_dir": _transcripts_root().is_dir(),
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
    root = _transcripts_root(args.transcripts_root)
    try:
        scope = digest.resolve_scope(repo, not args.diff, args.range, args.uncommitted)
        if scope.uncommitted:
            from enguru_lib import progress
            scope.range_id = progress.current_worktree_range_id(repo) or scope.range_id
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


def _load_bank_shaped(path, allow_topic_map=False):
    """Load a bank for a mutating command; exit 2 if it has the wrong shape."""
    from enguru_lib import bank
    data = _load_bank_json(path)
    problem = bank.shape_problem(data, allow_topic_map)
    if problem:
        fail("state_corrupt", "%s: %s" % (path, problem))
    return data


def _validation_problems(repo, data):
    from enguru_lib import bank, paths
    digest = {"events": []}
    dp = repo / ".enguru" / "digest.json"
    if dp.exists():
        try:
            digest = paths.read_json(dp)
        except (paths.StateCorrupt, OSError, ValueError):
            digest = {"events": []}
    return bank.validate_bank(repo, data, digest)


def cmd_bank_validate(args):
    repo = _repo_root(os.getcwd())
    path = _bank_path(repo, args.path)
    data = _load_bank_json(path)
    problems = _validation_problems(repo, data)
    emit({"ok": not problems, "problems": problems})


def _inside_state(repo, path, what):
    state = (repo / ".enguru").resolve()
    if state not in Path(path).resolve().parents:
        fail("state_corrupt", "%s must be inside .enguru/: %s" % (what, path))


def cmd_bank_promote(args):
    """Move a validated, sealed build from its work path to the live bank path."""
    repo = _repo_root(os.getcwd())
    work, dest = _bank_path(repo, args.work_path), _bank_path(repo, args.bank_path)
    _inside_state(repo, work, "work_path")
    _inside_state(repo, dest, "bank_path")
    data = _load_bank_shaped(work, allow_topic_map=True)
    problems = _validation_problems(repo, data)
    if problems:
        fail("state_corrupt", "%s still has %d validation problem(s); not promoted: %s"
             % (args.work_path, len(problems),
                "; ".join("%s: %s" % (p["item_id"], p["reason"]) for p in problems[:5])))
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(work), str(dest))
    emit({"ok": True, "bank_path": args.bank_path})


def cmd_bank_seal(args):
    from enguru_lib import bank
    repo = _repo_root(os.getcwd())
    path = _bank_path(repo, args.path)
    _load_bank_shaped(path, allow_topic_map=True)
    ids = [i for i in args.ids.split(",") if i] if args.ids is not None else None
    bank.seal(repo, path, ids)
    emit({"ok": True})


def cmd_bank_drop(args):
    from enguru_lib import bank
    repo = _repo_root(os.getcwd())
    path = _bank_path(repo, args.path)
    _load_bank_shaped(path)
    ids = [i for i in args.ids.split(",") if i]
    bank.drop_items(path, ids)
    emit({"ok": True, "dropped": ids})


def cmd_bank_apply_verdicts(args):
    from enguru_lib import bank
    repo = _repo_root(os.getcwd())
    path = _bank_path(repo, args.path)
    _load_bank_shaped(path)
    emit(bank.apply_verdicts(repo, path))


def cmd_bank_finding(args):
    from enguru_lib import bank, paths
    repo = _repo_root(os.getcwd())
    path = _bank_path(repo, args.path)
    _load_bank_shaped(path)
    f = bank.finding_of(path, args.concept_id)
    if f is None:
        fail("state_corrupt", "no finding for concept %s in %s" % (args.concept_id, path))
    out = _bank_path(repo, args.out)
    _inside_state(repo, out, "--out")
    out.parent.mkdir(parents=True, exist_ok=True)
    paths.atomic_write_json(out, f)
    emit({"ok": True, "out": args.out})


def cmd_bank_merge(args):
    from enguru_lib import bank
    repo = _repo_root(os.getcwd())
    path, part = _bank_path(repo, args.path), _bank_path(repo, args.partial)
    if path.exists():
        _load_bank_shaped(path)
    _load_bank_shaped(part)
    bank.merge_partial(path, part)
    emit({"ok": True})


def _progress_call(fn, *a):
    from enguru_lib import digest, paths, progress
    try:
        return fn(*a)
    except progress.ProgressError as e:
        fail(e.code, e.message)
    except digest.DigestError as e:
        fail(e.code, e.message)
    except paths.LockHeld as e:
        fail("lock_held", "Another enguru session is running (%s). "
             "Run `session start --takeover` to take it over." % e)
    except paths.StateCorrupt as e:
        fail("state_corrupt", str(e))


def _stdin_json():
    try:
        return json.loads(sys.stdin.read())
    except ValueError as e:
        fail("state_corrupt", "stdin is not valid JSON: %s" % e)


def cmd_session_start(args):
    from enguru_lib import digest, progress
    repo = _repo_root(os.getcwd())
    intensity = "quick" if args.quick else "thorough" if args.thorough else None
    scope = _progress_call(digest.resolve_scope, repo, not args.diff, args.range, args.uncommitted)
    emit(_progress_call(progress.start_session, repo, scope, intensity, None, args.takeover))


def cmd_session_end(args):
    from enguru_lib import progress
    emit(_progress_call(progress.end_session, _repo_root(os.getcwd())))


def cmd_session_set_intensity(args):
    from enguru_lib import progress
    emit(_progress_call(progress.set_intensity, _repo_root(os.getcwd()), args.intensity))


def cmd_next(args):
    from enguru_lib import progress
    emit(_progress_call(progress.next_item, _repo_root(os.getcwd())))


def cmd_record(args):
    from enguru_lib import progress
    rec = _stdin_json()
    emit(_progress_call(progress.record, _repo_root(os.getcwd()), rec))


def cmd_status(args):
    from enguru_lib import progress
    emit(_progress_call(progress.status, _repo_root(os.getcwd())))


def cmd_keylog_add(args):
    from enguru_lib import progress
    entry = _stdin_json()
    _progress_call(progress.keylog_add, _repo_root(os.getcwd()), entry)
    emit({"ok": True})


def cmd_cert_levels(args):
    from enguru_lib import rubric
    emit(_progress_call(rubric.cert_levels, _repo_root(os.getcwd()), args.save))


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
    sl.add_argument("--ids", help="only rehash these concept ids (comma-separated)")
    sl.set_defaults(func=cmd_bank_seal)
    dr = bk.add_parser("drop", help="drop questions or concepts by id")
    dr.add_argument("path")
    dr.add_argument("--ids", required=True)
    dr.set_defaults(func=cmd_bank_drop)
    av = bk.add_parser("apply-verdicts", help="apply finding-verifier verdicts")
    av.add_argument("path")
    av.set_defaults(func=cmd_bank_apply_verdicts)
    pr = bk.add_parser("promote", help="move a validated build from its work path to the bank path")
    pr.add_argument("work_path")
    pr.add_argument("bank_path")
    pr.set_defaults(func=cmd_bank_promote)
    bf = bk.add_parser("finding", help="write one finding's JSON to a file")
    bf.add_argument("path")
    bf.add_argument("concept_id")
    bf.add_argument("--out", required=True)
    bf.set_defaults(func=cmd_bank_finding)
    bm = bk.add_parser("merge", help="merge a partial bank into a bank")
    bm.add_argument("path")
    bm.add_argument("partial")
    bm.set_defaults(func=cmd_bank_merge)
    se = sub.add_parser("session", help="interview sessions").add_subparsers(dest="scmd", required=True)
    ss = se.add_parser("start", help="start a session")
    smode = ss.add_mutually_exclusive_group(required=True)
    smode.add_argument("--project", action="store_true")
    smode.add_argument("--diff", action="store_true")
    ss.add_argument("--range")
    ss.add_argument("--uncommitted", action="store_true")
    sint = ss.add_mutually_exclusive_group()
    sint.add_argument("--quick", action="store_true")
    sint.add_argument("--thorough", action="store_true")
    ss.add_argument("--takeover", action="store_true", help="reclaim a lock held by another session")
    ss.set_defaults(func=cmd_session_start)
    se.add_parser("end", help="end the session").set_defaults(func=cmd_session_end)
    si = se.add_parser("set-intensity", help="choose diff intensity")
    si.add_argument("intensity", choices=["quick", "thorough"])
    si.set_defaults(func=cmd_session_set_intensity)
    sub.add_parser("next", help="next interview item").set_defaults(func=cmd_next)
    sub.add_parser("record", help="record a result (JSON on stdin)").set_defaults(func=cmd_record)
    sub.add_parser("status", help="progress summary").set_defaults(func=cmd_status)
    kl = sub.add_parser("keylog", help="answer-key corrections").add_subparsers(dest="kcmd", required=True)
    kl.add_parser("add", help="append a correction (JSON on stdin)").set_defaults(func=cmd_keylog_add)
    ce = sub.add_parser("cert", help="certificate data").add_subparsers(dest="ccmd", required=True)
    cl = ce.add_parser("levels", help="compute skill levels")
    cl.add_argument("--save", action="store_true")
    cl.set_defaults(func=cmd_cert_levels)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
