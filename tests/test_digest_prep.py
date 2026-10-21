import pytest
import json
import subprocess

from conftest import (assistant, make_repo, run_cli, run_cli_raw, tool_result,
                      user, write_transcript)
from enguru_lib import digest, schema, transcripts


def _tu(name, **inp):
    return {"type": "tool_use", "id": "t", "name": name, "input": inp}


def _setup(tmp_path, entries_fn, files=None):
    r = make_repo(tmp_path, [files or {"a.py": "x = 1\n"}], ["init"])
    root = tmp_path / "tx"
    enc = transcripts.encode_project_dir(r)
    entries = entries_fn(str(r))
    write_transcript(root, enc, "s1", entries)
    return r, root


def _prep(r, root, kind="project"):
    return digest.prep(r, digest.Scope(kind), root)


def _rules(r):
    return json.loads((r / ".enguru/work/rules.json").read_text())


def test_chunks_written_and_pending_listed(tmp_path):
    def mk(c):
        return [user("please add a feature", cwd=c, uuid="u1"),
                assistant([{"type": "text", "text": "ok"}, _tu("Edit", file_path=c + "/a.py")], cwd=c, uuid="a1")]
    r, root = _setup(tmp_path, mk)
    out = _prep(r, root)
    assert [p["chunk_id"] for p in out["pending"]] == ["s1__0"]
    chunk = json.loads(open(out["pending"][0]["chunk_path"]).read())
    assert schema.validate(chunk, schema.load_schema("chunk")) == []
    assert [t["id"] for t in chunk["turns"]] == ["u1", "a1"]
    assert chunk["turns"][1]["files"] == ["a.py"]
    assert out["pending"][0]["out_path"].endswith("work/extracted/s1__0.json")
    prep = json.loads((r / ".enguru/work/prep.json").read_text())
    assert prep["chunks"][0]["turn_ids"] == ["u1", "a1"]
    assert "rules.json" in [p.name for p in (r / ".enguru/work").iterdir()]


def test_cached_chunks_not_pending(tmp_path):
    def mk(c):
        return [user("hello", cwd=c, uuid="u1")]
    r, root = _setup(tmp_path, mk)
    out = _prep(r, root)
    h = json.loads((r / ".enguru/work/prep.json").read_text())["chunks"][0]["content_hash"]
    cache = r / ".enguru/cache/extractor"
    cache.mkdir(parents=True)
    (cache / (h + "__0.json")).write_text("{}")
    out = _prep(r, root)
    assert out["pending"] == [] and out["cached"] == ["s1__0"]
    assert not (r / ".enguru/work/chunks/s1__0.json").exists()
    prep = json.loads((r / ".enguru/work/prep.json").read_text())
    assert prep["chunks"][0]["turn_ids"] == ["u1"] and prep["chunks"][0]["cached"]


def test_assistant_text_kept_only_near_edits(tmp_path):
    def mk(c):
        return [user("q", cwd=c, uuid="u1"),
                assistant([{"type": "text", "text": "just talking"}], cwd=c, uuid="a1"),
                user("q2", cwd=c, uuid="u2"),
                user("q3", cwd=c, uuid="u3"),
                user("q4", cwd=c, uuid="u4"),
                assistant([{"type": "text", "text": "about to edit"}], cwd=c, uuid="a2"),
                assistant([_tu("Write", file_path=c + "/a.py")], cwd=c, uuid="a3")]
    r, root = _setup(tmp_path, mk)
    out = _prep(r, root)
    ids = [t["id"] for t in json.load(open(out["pending"][0]["chunk_path"]))["turns"]]
    assert "a1" not in ids and "a2" in ids and "a3" not in ids


def test_turn_text_capped(tmp_path):
    r, root = _setup(tmp_path, lambda c: [user("a" * 50000, cwd=c, uuid="u1")])
    out = _prep(r, root)
    text = json.load(open(out["pending"][0]["chunk_path"]))["turns"][0]["text"]
    assert text == "a" * 1500 + " …[truncated]"


def test_secrets_redacted_in_chunks(tmp_path):
    secret = "AKIA" + "A" * 16
    def mk(c):
        return [user("key is " + secret, cwd=c, uuid="u1"),
                assistant([_tu("Bash", command="ls")], cwd=c, uuid="a1"),
                tool_result("t", "boom " + secret, is_error=True, cwd=c, uuid="t1")]
    r, root = _setup(tmp_path, mk)
    out = _prep(r, root)
    raw = open(out["pending"][0]["chunk_path"]).read()
    assert secret not in raw and "[REDACTED]" in raw
    turns = json.loads(raw)["turns"]
    assert [t["role"] for t in turns] == ["user", "tool"]


def test_churn_three_edits_and_fail_then_fix(tmp_path):
    def mk(c):
        e = [assistant([_tu("Edit", file_path=c + "/a.py")], cwd=c, uuid="e%d" % i) for i in range(3)]
        return e + [assistant([_tu("Bash", command="python -m pytest -q")], cwd=c, uuid="b1"),
                    tool_result("t", "FAILED", is_error=True, cwd=c, uuid="t1"),
                    assistant([_tu("Edit", file_path=c + "/b.py")], cwd=c, uuid="e9")]
    r, root = _setup(tmp_path, mk)
    _prep(r, root)
    ev = _rules(r)["events"]
    churn = [e for e in ev if e["label"] == "churn"]
    paras = [e["paraphrase"] for e in churn]
    assert "a.py edited 3 times" in paras
    assert "fix after failing pytest" in paras
    assert all(e["origin"] == "rules" and "id" not in e for e in churn)
    a = [e for e in churn if e["paraphrase"].startswith("a.py")][0]
    assert a["files"] == ["a.py"] and a["source"] == {"turn_ids": ["e0", "e1", "e2"]}


def test_dependency_from_bash_and_manifest(tmp_path):
    r = make_repo(tmp_path, [{"package.json": "{}"}, {"package.json": '{"a":1}'}], ["init", "add dep"])
    root = tmp_path / "tx"
    c = str(r)
    write_transcript(root, transcripts.encode_project_dir(r), "s1", [
        assistant([_tu("Bash", command="npm install left-pad")], cwd=c, uuid="a1")])
    _prep(r, root)
    ev = _rules(r)["events"]
    deps = [e for e in ev if e["label"] == "dependency"]
    assert any(e["source"] == {"turn_ids": ["a1"]} and e["session_id"] == "s1" for e in deps)
    commit_evs = [e for e in deps if "commit" in e["source"]]
    assert len(commit_evs) == 2 and all(e["session_id"] is None for e in commit_evs)
    assert _rules(r)["diff"] is None and _rules(r)["git"]["commits"] == 2


def test_diff_filters_sessions_by_files(tmp_path):
    r = make_repo(tmp_path, [{"a.py": "x = 1\n", "b.py": "y = 1\n"}], ["init"])
    (r / "a.py").write_text("x = 2\n")
    root = tmp_path / "tx"
    c = str(r)
    enc = transcripts.encode_project_dir(r)
    write_transcript(root, enc, "hit", [
        user("change a", cwd=c, uuid="h1", session_id="hit", ts="2020-01-01T00:00:00Z"),
        assistant([_tu("Edit", file_path=c + "/a.py")], cwd=c, uuid="h2", session_id="hit")])
    write_transcript(root, enc, "miss", [
        user("other", cwd=c, uuid="m1", session_id="miss", ts="2020-01-01T00:00:00Z"),
        assistant([_tu("Edit", file_path=c + "/b.py")], cwd=c, uuid="m2", session_id="miss")])
    out = digest.prep(r, digest.Scope("diff", None, None, True, "worktree-x"), root)
    assert [p["chunk_id"] for p in out["pending"]] == ["hit__0"]
    rules = _rules(r)
    assert rules["sessions_found"] == 2 and rules["sessions_used"] == 1
    assert rules["diff"]["hunks"][0]["path"] == "a.py"


def test_stale_work_cleared(tmp_path):
    r, root = _setup(tmp_path, lambda c: [user("hi", cwd=c, uuid="u1")])
    _prep(r, root)
    stale = r / ".enguru/work/chunks/old__0.json"
    stale.write_text("{}")
    _prep(r, root)
    assert not stale.exists()


def test_cli_empty_diff_and_no_base_errors(tmp_path):
    r = make_repo(tmp_path, [{"a.py": "x\n"}], ["init"])
    subprocess.run(["git", "checkout", "-q", "-b", "other"], cwd=r, check=True)
    root = str(tmp_path / "tx")
    code, out = run_cli_raw(["digest", "prep", "--diff", "--uncommitted", "--transcripts-root", root], r)
    assert code == 2 and out["error"] == "empty_diff"
    code, out = run_cli_raw(["digest", "prep", "--diff", "--range", "nope..HEAD", "--transcripts-root", root], r)
    assert code == 2 and out["error"] == "no_base"
    plain = tmp_path / "plain"
    plain.mkdir()
    code, out = run_cli_raw(["digest", "prep", "--diff", "--transcripts-root", root], plain)
    assert code == 2 and out["error"] == "not_git"
    out = run_cli(["digest", "prep", "--project", "--transcripts-root", root], plain)
    assert out["pending"] == []


def test_range_errors_are_friendly(tmp_path):
    r = make_repo(tmp_path, [{"a.py": "x\n"}], ["init"])
    for rng in ("..HEAD", "nope..HEAD"):
        with pytest.raises(digest.DigestError) as e:
            digest.resolve_scope(r, False, rng, False)
        assert e.value.code == "no_base"
        assert "git" not in e.value.message and "A..B" in e.value.message


def test_cli_uncommitted_session_with_only_untracked_and_no_commits(tmp_path):
    r = make_repo(tmp_path, [], [])
    (r / "new.py").write_text("print(1)\n")
    s = run_cli(["session", "start", "--diff", "--uncommitted", "--quick"], r)
    assert s["scope"] == "diff" and s["range_id"].startswith("worktree-")
    code, out = run_cli_raw(["session", "start", "--diff", "--takeover"], r)
    assert code == 2 and out["error"] == "no_base" and "failed" not in out["message"]


def test_extractor_fan_out_capped_to_recent_sessions_and_chunks(tmp_path, monkeypatch):
    r = make_repo(tmp_path, [{"a.py": "x = 1\n"}], ["init"])
    root = tmp_path / "tx"
    enc = transcripts.encode_project_dir(r)
    c = str(r)
    for n in range(3):  # s0 oldest ... s2 newest; 3 one-turn chunks each
        write_transcript(root, enc, "s%d" % n, [
            user("request %d-%d" % (n, k), cwd=c, uuid="u%d-%d" % (n, k), session_id="s%d" % n,
                 ts="2026-10-0%dT12:0%d:00.000Z" % (n + 1, k)) for k in range(3)])
    monkeypatch.setattr(digest, "CHUNK_TURNS", 1)
    monkeypatch.setattr(digest, "load_limits", lambda: {"max_sessions": 2, "max_chunks": 4})
    out = _prep(r, root)
    assert [p["chunk_id"] for p in out["pending"]] == ["s2__0", "s2__1", "s2__2", "s1__0"]
    rules = _rules(r)
    assert rules["sessions_used"] == 2
    assert rules["truncation"] == ["sessions:capped:2/3", "chunks:capped:4/6"]
    prep = json.loads((r / ".enguru/work/prep.json").read_text())
    assert [c["chunk_id"] for c in prep["chunks"]] == ["s2__0", "s2__1", "s2__2", "s1__0"]
    res = digest.merge(r)
    assert res["truncation"][:2] == ["sessions:capped:2/3", "chunks:capped:4/6"]


def test_default_fan_out_limits_in_config():
    lim = digest.load_limits()
    assert lim == {"max_sessions": 12, "max_chunks": 12}


def test_enguru_interview_sessions_excluded(tmp_path):
    r = make_repo(tmp_path, [{"a.py": "x = 1\n"}], ["init"])
    root = tmp_path / "tx"
    enc = transcripts.encode_project_dir(r)
    c = str(r)
    write_transcript(root, enc, "work", [user("please add a feature", cwd=c, session_id="work")])
    write_transcript(root, enc, "quiz", [
        user("<command-name>/enguru:interview</command-name>", cwd=c, session_id="quiz"),
        assistant([_tu("Bash", command='python3 "/p/scripts/enguru.py" next')], cwd=c,
                  session_id="quiz")])
    write_transcript(root, enc, "quiz2", [
        user("go", cwd=c, session_id="quiz2"),
        assistant([_tu("Bash", command='python3 "/x/enguru/scripts/enguru.py" session start --project')],
                  cwd=c, session_id="quiz2")])
    out = _prep(r, root)
    # quiz is wholly enguru (dropped); quiz2 keeps its turn before the first enguru turn.
    assert sorted(p["chunk_id"] for p in out["pending"]) == ["quiz2__0", "work__0"]
    rules = _rules(r)
    assert rules["sessions_found"] == 3 and rules["sessions_used"] == 2
    assert "sessions:enguru_cut:2" in rules["truncation"]


def test_diff_hunk_lines_redacted(tmp_path):
    r = make_repo(tmp_path, [{"a.py": "x = 1\n"}, {"a.py": "x = 1\napi_key = 'abcdefghijklmnop'\n"}],
                  ["c1", "c2"])
    digest.prep(r, digest.resolve_scope(r, False, "HEAD~1..HEAD", False), tmp_path / "tx")
    lines = _rules(r)["diff"]["hunks"][0]["lines"]
    assert "abcdefghijklmnop" not in json.dumps(lines)
    assert any("[REDACTED]" in l for l in lines)


def test_enguru_session_truncated_at_first_enguru_turn(tmp_path):
    r = make_repo(tmp_path, [{"a.py": "x = 1\n"}], ["init"])
    root = tmp_path / "tx"
    enc = transcripts.encode_project_dir(r)
    c = str(r)
    write_transcript(root, enc, "mixed", [
        user("please add a feature", cwd=c, session_id="mixed"),
        user("<command-name>/enguru:interview</command-name>", cwd=c, session_id="mixed"),
        user("secret quiz answer", cwd=c, session_id="mixed")])
    write_transcript(root, enc, "allquiz", [
        user("<command-name>/enguru:interview</command-name>", cwd=c, session_id="allquiz")])
    out = _prep(r, root)
    assert [p["chunk_id"] for p in out["pending"]] == ["mixed__0"]
    assert "secret quiz answer" not in json.dumps(
        json.loads((r / ".enguru/work/chunks/mixed__0.json").read_text()))
    rules = _rules(r)
    assert rules["sessions_used"] == 1
    assert "sessions:enguru_cut:2" in rules["truncation"]
