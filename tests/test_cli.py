import subprocess
import sys

from conftest import run_cli, run_cli_raw


def test_cli_check_outputs_single_json(tmp_path):
    out = run_cli(["check"], cwd=tmp_path)
    assert out["ok"] is True and "python" in out
    assert out["git"] is False
    assert isinstance(out["transcripts_dir"], bool)


def test_cli_check_detects_git(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert run_cli(["check"], cwd=tmp_path)["git"] is True


def test_run_cli_raw_returns_code(tmp_path):
    code, out = run_cli_raw(["check"], cwd=tmp_path)
    assert code == 0 and out["ok"] is True
