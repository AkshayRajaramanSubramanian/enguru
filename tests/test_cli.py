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


def _run_env(args, cwd, env_extra):
    import json
    import os
    from conftest import CLI
    env = dict(os.environ, **env_extra)
    proc = subprocess.run([sys.executable, str(CLI), *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True)
    return proc.returncode, json.loads(proc.stdout)


def test_check_transcripts_dir_honours_env_root(tmp_path):
    root = tmp_path / "tx"
    code, out = _run_env(["check"], tmp_path, {"ENGURU_TRANSCRIPTS_ROOT": str(root)})
    assert code == 0 and out["transcripts_dir"] is False
    root.mkdir()
    code, out = _run_env(["check"], tmp_path, {"ENGURU_TRANSCRIPTS_ROOT": str(root)})
    assert out["transcripts_dir"] is True


def test_digest_prep_reads_env_transcripts_root(tmp_path):
    from conftest import make_repo, user, write_transcript
    from enguru_lib import transcripts
    r = make_repo(tmp_path, [{"a.py": "x = 1\n"}], ["init"])
    root = tmp_path / "tx"
    write_transcript(root, transcripts.encode_project_dir(r), "s1",
                     [user("please add a feature", cwd=str(r), uuid="u1")])
    code, out = _run_env(["digest", "prep", "--project"], r, {"ENGURU_TRANSCRIPTS_ROOT": str(root)})
    assert code == 0 and [p["chunk_id"] for p in out["pending"]] == ["s1__0"]
    # the explicit flag still wins over the environment
    code, out = _run_env(["digest", "prep", "--project", "--transcripts-root", str(tmp_path / "none")],
                         r, {"ENGURU_TRANSCRIPTS_ROOT": str(root)})
    assert code == 0 and out["pending"] == []
