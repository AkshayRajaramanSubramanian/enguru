import json

import pytest

from conftest import (assistant, make_repo, run_cli, run_cli_raw, user,
                      write_transcript)
from enguru_lib import digest, schema, transcripts


def _tu(name, **inp):
    return {"type": "tool_use", "id": "t", "name": name, "input": inp}


def _setup(tmp_path, entries_fn):
    r = make_repo(tmp_path, [{"a.py": "x = 1\n"}], ["init"])
    root = tmp_path / "tx"
    write_transcript(root, transcripts.encode_project_dir(r), "s1", entries_fn(str(r)))
    return r, root


def _mk(c):
    return [user("please add a feature", cwd=c, uuid="u1", ts="2026-10-01T12:00:00.000Z"),
            assistant([{"type": "text", "text": "ok"}, _tu("Edit", file_path=c + "/a.py")],
                      cwd=c, uuid="a1", ts="2026-10-01T12:01:00.000Z"),
            user("no, revert that", cwd=c, uuid="u2", ts="2026-10-01T12:02:00.000Z")]


def _prep(r, root):
    return digest.prep(r, digest.Scope("project"), root)


def _ex(r, cid, events):
    d = r / ".enguru/work/extracted"
    d.mkdir(parents=True, exist_ok=True)
    (d / (cid + ".json")).write_text(json.dumps({"chunk_id": cid, "events": events}))


def _ev(turns, label="choice", text="use sqlite", conf=0.9):
    return {"label": label, "paraphrase": text, "files": ["a.py"],
            "confidence": conf, "source_turn_ids": turns}


def _digest(r):
    return json.loads((r / ".enguru/digest.json").read_text())


def test_events_with_unknown_turn_ids_dropped(tmp_path):
    r, root = _setup(tmp_path, _mk)
    _prep(r, root)
    _ex(r, "s1__0", [_ev(["a1"]), _ev(["a1", "zzz"]), _ev(["nope"])])
    out = digest.merge(r)
    assert out["events_dropped"] == 2 and out["fallback_chunks"] == []
    d = _digest(r)
    assert [e["origin"] for e in d["events"]] == ["haiku"]
    ev = d["events"][0]
    assert ev["timestamp"] == "2026-10-01T12:01:00.000Z" and ev["session_id"] == "s1"
    assert ev["source"] == {"turn_ids": ["a1"]} and "confidence" not in ev
    assert d["meta"]["events_dropped"] == 2 and d["meta"]["events_kept"] == 1


def test_missing_extractor_output_uses_keywords(tmp_path):
    def mk(c):
        return [user("add a feature", cwd=c, uuid="u1"),
                assistant([{"type": "text", "text": "done"}, _tu("Edit", file_path=c + "/a.py")], cwd=c, uuid="a1"),
                user("hmm could we do it another way", cwd=c, uuid="u2"),
                assistant([{"type": "text", "text": "ok"}, _tu("Edit", file_path=c + "/a.py")], cwd=c, uuid="a2"),
                user("no, revert that", cwd=c, uuid="u3")]
    r, root = _setup(tmp_path, mk)
    _prep(r, root)
    out = digest.merge(r)
    assert out["fallback_chunks"] == ["s1__0"]
    d = _digest(r)
    kw = [(e["label"], e["source"]["turn_ids"]) for e in d["events"] if e["origin"] == "keywords"]
    assert kw == [("request", ["u1"]), ("pushback", ["u3"])]
    assert d["meta"]["fallback_chunks"] == ["s1__0"]


def test_keyword_events_unit():
    chunk = {"chunk_id": "c", "session_id": "s", "turns": [
        {"id": "u1", "role": "user", "text": "build it", "files": [], "errors": []},
        {"id": "a1", "role": "assistant", "text": "ok", "files": ["x.py"], "errors": []},
        {"id": "t1", "role": "tool", "text": "", "files": [], "errors": []},
        {"id": "u2", "role": "user", "text": "That's not what I wanted", "files": [], "errors": []}]}
    evs = digest.keyword_events(chunk)
    assert [(e["label"], e["origin"]) for e in evs] == [("request", "keywords"), ("pushback", "keywords")]
    assert evs[1]["files"] == ["x.py"]


def test_invalid_output_falls_back(tmp_path):
    r, root = _setup(tmp_path, _mk)
    _prep(r, root)
    (r / ".enguru/work/extracted").mkdir(parents=True, exist_ok=True)
    (r / ".enguru/work/extracted/s1__0.json").write_text('{"chunk_id": "s1__0", "events": [{"label": "bad"}]}')
    assert digest.merge(r)["fallback_chunks"] == ["s1__0"]


def test_valid_output_cached_and_reused_next_prep(tmp_path):
    r, root = _setup(tmp_path, _mk)
    _prep(r, root)
    _ex(r, "s1__0", [_ev(["a1"])])
    digest.merge(r)
    out = _prep(r, root)
    assert out["pending"] == [] and out["cached"] == ["s1__0"]
    res = digest.merge(r)
    assert res["fallback_chunks"] == [] and res["events_kept"] >= 1
    assert [e["origin"] for e in _digest(r)["events"]].count("haiku") == 1


def test_invalid_cache_deleted_and_flagged(tmp_path):
    r, root = _setup(tmp_path, _mk)
    _prep(r, root)
    _ex(r, "s1__0", [_ev(["a1"])])
    digest.merge(r)
    cache = list((r / ".enguru/cache/extractor").glob("*.json"))
    assert len(cache) == 1
    cache[0].write_text("{}")
    _prep(r, root)
    res = digest.merge(r)
    assert res["fallback_chunks"] == ["s1__0"] and not cache[0].exists()
    assert _digest(r)["meta"]["fallback_chunks"] == ["s1__0"]  # noted in digest.meta


def _big(n, para_len=400):
    return {"version": 1,
            "meta": {"scope": "project", "range_id": None, "sessions_found": 1, "sessions_used": 1,
                     "events_kept": n, "events_dropped": 0, "fallback_chunks": [],
                     "skipped_lines": 0, "truncation": []},
            "file_map": {}, "git": {}, "diff": None,
            "events": [{"id": "ev-%d" % i, "label": "request" if i % 2 else "choice",
                        "paraphrase": "x" * para_len, "files": [], "session_id": "s",
                        "timestamp": "2026-10-01T12:%02d:00Z" % (i % 60),
                        "source": {"turn_ids": ["t"]}, "origin": "haiku"} for i in range(n)]}


def test_budget_order():
    d = _big(250)
    out = digest.apply_budget(d)
    assert out["meta"]["truncation"] == ["excerpts:300", "excerpts:150", "drop:request"]
    assert len(json.dumps(out)) <= 80000
    assert all(e["label"] != "request" for e in out["events"])
    assert all(len(e["paraphrase"]) <= 150 for e in out["events"])
    assert out["meta"]["events_kept"] == len(out["events"])


def test_budget_noop_under_cap():
    d = _big(3, 50)
    assert digest.apply_budget(d)["meta"]["truncation"] == []


def test_budget_drop_oldest_then_counts():
    d = _big(400)
    out = digest.apply_budget(d, cap_tokens=8000)
    t = out["meta"]["truncation"]
    assert t[:3] == ["excerpts:300", "excerpts:150", "drop:request"]
    assert t[3].startswith("drop:oldest:")
    d = _big(400)
    out = digest.apply_budget(d, cap_tokens=500)
    assert out["events"] == [] and out["meta"]["truncation"][-1] == "events:counts"
    assert sum(out["meta"]["event_counts"].values()) > 0 and out["meta"]["events_kept"] == 0


def test_digest_validates_against_schema(tmp_path):
    r, root = _setup(tmp_path, _mk)
    _prep(r, root)
    _ex(r, "s1__0", [_ev(["a1"])])
    digest.merge(r)
    d = _digest(r)
    assert schema.validate(d, schema.load_schema("digest")) == []
    assert [e["id"] for e in d["events"]] == ["ev-%d" % (i + 1) for i in range(len(d["events"]))]
    ts = [e["timestamp"] for e in d["events"]]
    assert ts == sorted(ts)
    assert d["meta"]["scope"] == "project" and d["diff"] is None


def test_cli_merge(tmp_path):
    r, root = _setup(tmp_path, _mk)
    code, out = run_cli_raw(["digest", "merge"], r)
    assert code == 2 and out["error"] == "state_corrupt" and "prep" in out["message"]
    run_cli(["digest", "prep", "--project", "--transcripts-root", str(root)], r)
    out = run_cli(["digest", "merge"], r)
    assert out["ok"] is True and set(out) == {"ok", "events_kept", "events_dropped",
                                              "fallback_chunks", "truncation"}


def _file_map(n):
    files = [{"path": "src/pkg%d/module_%04d.py" % (i % 40, i), "size": 1000 + i}
             for i in range(n)]
    files.append({"path": "app.py", "size": 10})
    return {"files": files, "stack": ["pyproject.toml"], "entry_points": ["app.py"]}


def test_budget_caps_large_file_map_and_keeps_events():
    d = _big(40, 200)
    d["file_map"] = _file_map(1600)
    d["git"] = {"churn_files": [["src/pkg3/module_0003.py", 50]]}
    out = digest.apply_budget(d)
    assert schema.validate(out, schema.load_schema("digest")) == []
    assert len(json.dumps(out)) / 4 <= 20000
    assert len(out["events"]) == 40 and out["meta"]["events_kept"] == 40
    fm = out["file_map"]
    kept = {f["path"] for f in fm["files"]}
    assert "app.py" in kept and "src/pkg3/module_0003.py" in kept  # entry point, churn
    assert any(t.startswith("file_map:capped:") for t in out["meta"]["truncation"])
    n_kept = int(next(t for t in out["meta"]["truncation"]
                      if t.startswith("file_map:capped:")).split(":")[2].split("/")[0])
    assert n_kept == len(fm["files"]) and n_kept < 1601
    assert sum(fm["file_counts"]["by_dir"].values()) == 1601 - n_kept
    assert sum(fm["file_counts"]["by_ext"].values()) == 1601 - n_kept


def test_budget_caps_large_diff_hunks_and_keeps_events():
    d = _big(40, 200)
    d["diff"] = {"hunks": [{"id": "h-%d" % i, "path": "f%d.py" % i, "old_start": 1, "new_start": 1,
                            "lines": ["+line %d of a long generated hunk body" % j
                                      for j in range(400)]} for i in range(12)],
                 "excluded": [], "blast_radius_candidates": []}
    out = digest.apply_budget(d)
    assert schema.validate(out, schema.load_schema("digest")) == []
    assert len(json.dumps(out)) / 4 <= 20000
    assert len(out["events"]) == 40
    hunks = out["diff"]["hunks"]
    assert [h["id"] for h in hunks][:1] == ["h-0"]
    assert any(t.startswith("hunks:") for t in out["meta"]["truncation"])
    assert any("truncated" in h["lines"][-1] for h in hunks)


def test_budget_drop_oldest_is_linear(monkeypatch):
    calls = []
    real = digest._size
    monkeypatch.setattr(digest, "_size", lambda d: calls.append(1) or real(d))
    digest.apply_budget(_big(2000), cap_tokens=60000)
    assert len(calls) < 50
