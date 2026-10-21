import subprocess
from datetime import datetime

from conftest import make_repo
from enguru_lib import gitinfo


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), check=True,
                          capture_output=True, text=True).stdout.strip()


def test_commit_log_and_revert_flag(tmp_path):
    r = make_repo(tmp_path, [{"a.py": "1\n"}, {"a.py": "1\n2\n", "b.py": "x\n"},
                             {"a.py": "1\n"}],
                  ["first", "second", 'Revert "second"'])
    commits = gitinfo.commit_log(r)
    assert [c.subject for c in commits] == ['Revert "second"', "second", "first"]
    assert [c.is_revert for c in commits] == [True, False, False]
    second = commits[1]
    assert sorted(second.files) == [("a.py", 1, 0), ("b.py", 1, 0)]
    assert len(second.sha) == 40 and second.date.startswith("20")
    assert gitinfo.commit_log(r, limit=1)[0].subject == 'Revert "second"'
    assert gitinfo.is_git_repo(r)
    assert not gitinfo.is_git_repo(tmp_path)


def test_revert_by_body(tmp_path):
    r = make_repo(tmp_path, [{"a": "1"}, {"a": "2"}],
                  ["one", "undo\n\nThis reverts commit abc123."])
    assert gitinfo.commit_log(r)[0].is_revert


def test_churn_ranks_most_changed(tmp_path):
    r = make_repo(tmp_path, [{"a.py": "1\n", "b.py": "1\n"},
                             {"a.py": "1\n2\n3\n"}, {"a.py": "x\n"}],
                  ["c1", "c2", "c3"])
    top = gitinfo.churn_files(gitinfo.commit_log(r))
    assert top[0][0] == "a.py" and top[0][1] > top[1][1]
    assert len(gitinfo.churn_files(gitinfo.commit_log(r), top=1)) == 1


def test_dependency_history_detects_manifest_commits(tmp_path):
    r = make_repo(tmp_path, [{"a.py": "1"}, {"package.json": "{}"},
                             {"sub/requirements.txt": "x"}], ["a", "b", "c"])
    deps = gitinfo.dependency_history(gitinfo.commit_log(r))
    assert [d["file"] for d in deps] == ["sub/requirements.txt", "package.json"]
    assert set(deps[0]) == {"sha", "date", "file"}


def test_range_id_format(tmp_path):
    assert gitinfo.range_id(tmp_path, "a" * 40, "b" * 40) == "aaaaaaa..bbbbbbb"
    assert (gitinfo.worktree_range_id(datetime(2026, 10, 21, 9, 8, 7))
            == "worktree-20261021090807")


def test_uncommitted_hunks(tmp_path):
    r = make_repo(tmp_path, [{"a.py": "one\ntwo\nthree\n"}], ["c1"])
    (r / "a.py").write_text("one\nTWO\nthree\n")
    hunks, excluded = gitinfo.diff_hunks(r, None, None, True)
    assert excluded == []
    assert len(hunks) == 1
    h = hunks[0]
    assert (h.id, h.path, h.old_start, h.new_start) == ("h-1", "a.py", 1, 1)
    assert "-two" in h.lines and "+TWO" in h.lines
    assert not any(l.startswith("@@") for l in h.lines)


def test_diff_excludes_binary_lock_and_minified(tmp_path):
    r = make_repo(tmp_path, [{"README": "x"},
                             {"img.png": b"\x89PNG\x00\x01\x02", "package-lock.json": "{}\n",
                              "dist/a.min.js": "var a;\n", "src/a.py": "print(1)\n",
                              ".env": "SECRET=1\n"}],
                  ["c1", "c2"])
    base = git(r, "rev-parse", "HEAD~1")
    hunks, excluded = gitinfo.diff_hunks(r, base, "HEAD", False)
    assert {h.path for h in hunks} == {"src/a.py"}
    assert set(excluded) == {"img.png", "package-lock.json", "dist/a.min.js", ".env"}
    assert [h.id for h in hunks] == ["h-1"]


def test_diff_handles_delete_and_spaces(tmp_path):
    r = make_repo(tmp_path, [{"gone.py": "a\n", "my file é.py": "a\n"},
                             {"gone.py": None, "my file é.py": "b\n"}], ["c1", "c2"])
    hunks, _ = gitinfo.diff_hunks(r, git(r, "rev-parse", "HEAD~1"), None, False)
    assert {h.path for h in hunks} == {"gone.py", "my file é.py"}


def test_default_base_fallback_chain(tmp_path):
    r = make_repo(tmp_path, [{"a": "1"}, {"a": "2"}], ["c1", "c2"])
    first = git(r, "rev-parse", "HEAD~1")
    git(r, "checkout", "-q", "-b", "feat", first)
    (r / "b").write_text("x")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "feat")
    assert gitinfo.resolve_default_base(r) == first
    git(r, "checkout", "-q", "--detach")
    assert gitinfo.resolve_default_base(r) == first


def test_no_base_when_unresolvable(tmp_path):
    r = make_repo(tmp_path, [{"a": "1"}], ["c1"])
    git(r, "branch", "-m", "trunk")
    assert gitinfo.resolve_default_base(r) is None


def test_file_map_skips_env(tmp_path):
    r = make_repo(tmp_path, [{"main.py": "x", ".env": "S=1", "package.json": "{}",
                              "src/main.rs": "fn main(){}", "cmd/x/main.go": "p"}],
                  ["c1"])
    m = gitinfo.file_map(r)
    paths = [f["path"] for f in m["files"]]
    assert ".env" not in paths and "main.py" in paths
    assert m["stack"] == ["package.json"]
    assert set(m["entry_points"]) == {"main.py", "src/main.rs", "cmd/x/main.go"}
    assert all(isinstance(f["size"], int) for f in m["files"])


def test_file_map_without_git(tmp_path):
    d = tmp_path / "plain"
    (d / "node_modules").mkdir(parents=True)
    (d / "node_modules" / "x.js").write_text("x")
    (d / ".hidden").mkdir()
    (d / ".hidden" / "y").write_text("y")
    (d / "app.py").write_text("print()")
    (d / ".env").write_text("S=1")
    m = gitinfo.file_map(d)
    assert [f["path"] for f in m["files"]] == ["app.py"]
    assert m["entry_points"] == ["app.py"]


def test_excluded_hunk_body_cannot_forge_headers(tmp_path):
    body = "\n".join("l%d" % i for i in range(30)) + "\n"
    r = make_repo(tmp_path, [{".env": body, "src/a.py": "x\n"}], ["c1"])
    changed = body.replace("l2\n", "-- x\n").replace("l25\n", "++ y\n")
    (r / ".env").write_text(changed)
    (r / "src" / "a.py").write_text("y\n")
    git(r, "config", "diff.noprefix", "true")
    hunks, excluded = gitinfo.diff_hunks(r, None, None, True)
    assert excluded == [".env"]
    assert {h.path for h in hunks} == {"src/a.py"}
    assert not any("x" in l or "y" in l for h in hunks if h.path == ".env" for l in h.lines)


def test_uncommitted_includes_untracked_files(tmp_path):
    r = make_repo(tmp_path, [{"a.py": "one\n", ".gitignore": "ignored.py\n"}], ["c1"])
    (r / "new.py").write_text("def f():\n    return 1\n")
    (r / "ignored.py").write_text("x = 1\n")
    (r / ".env.local").write_text("SECRET=1\n")
    (r / "pic.bin").write_bytes(b"\x00\x01\x02binary")
    (r / "yarn.lock").write_text("lock\n")
    hunks, excluded = gitinfo.diff_hunks(r, None, None, True)
    assert [(h.path, h.old_start, h.new_start, h.lines) for h in hunks] == [
        ("new.py", 0, 1, ["+def f():", "+    return 1"])]
    assert set(excluded) == {".env.local", "pic.bin", "yarn.lock"}


def test_uncommitted_untracked_file_lines_capped(tmp_path):
    r = make_repo(tmp_path, [{"a.py": "one\n"}], ["c1"])
    (r / "big.py").write_text("".join("x%d = 1\n" % i for i in range(gitinfo.UNTRACKED_MAX_LINES + 50)))
    (h,), _ = gitinfo.diff_hunks(r, None, None, True)
    assert len(h.lines) == gitinfo.UNTRACKED_MAX_LINES + 1
    assert h.lines[-1] == "… [50 more lines truncated]"


def test_uncommitted_in_repo_without_commits(tmp_path):
    r = make_repo(tmp_path, [], [])
    (r / "staged.py").write_text("a = 1\n")
    git(r, "add", "staged.py")
    (r / "loose.py").write_text("b = 2\n")
    hunks, _ = gitinfo.diff_hunks(r, None, None, True)
    assert {h.path: h.lines for h in hunks} == {"staged.py": ["+a = 1"], "loose.py": ["+b = 2"]}


def test_untracked_symlinks_skipped(tmp_path):
    import os
    r = make_repo(tmp_path, [{"a.py": "one\n"}], ["c1"])
    (r / "real.py").write_text("b = 2\n")
    os.symlink("real.py", r / "link.py")
    os.symlink("/etc/hostname", r / "outside.txt")
    hunks, _ = gitinfo.diff_hunks(r, None, None, True)
    assert [h.path for h in hunks] == ["real.py"]


def test_empty_tree_computed_from_git(tmp_path):
    r = make_repo(tmp_path, [], [])
    assert gitinfo.empty_tree(r) == gitinfo.EMPTY_TREE
    assert gitinfo.empty_tree(tmp_path / "nonexistent") == gitinfo.EMPTY_TREE
