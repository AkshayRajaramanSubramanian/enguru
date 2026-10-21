import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

CLI = ROOT / "scripts" / "enguru.py"


def run_cli_raw(args, cwd, stdin=None):
    """Run the CLI; return (exit_code, parsed_stdout_json)."""
    proc = subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=str(cwd),
        input=stdin,
        capture_output=True,
        text=True,
    )
    return proc.returncode, json.loads(proc.stdout)


def run_cli(args, cwd, stdin=None):
    """Run the CLI, assert exit 0, return parsed JSON."""
    code, out = run_cli_raw(args, cwd, stdin)
    assert code == 0, out
    return out
