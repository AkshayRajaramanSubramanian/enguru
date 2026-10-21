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


def build_parser():
    p = argparse.ArgumentParser(prog="enguru")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="environment check").set_defaults(func=cmd_check)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
