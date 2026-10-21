import hashlib

from conftest import assistant, tool_result, user, write_transcript
from enguru_lib import transcripts as T


def test_string_and_block_content_normalized(tmp_path, repo):
    root = tmp_path / "proj"
    enc = T.encode_project_dir(repo)
    write_transcript(root, enc, "s1", [
        user("hi", cwd=repo),
        assistant([{"type": "thinking", "thinking": "secret"},
                   {"type": "text", "text": "plan"}], cwd=repo),
        {"type": "summary", "summary": "x"},
    ])
    sessions, skipped = T.load_sessions(repo, root)
    assert skipped == 0
    assert len(sessions) == 1
    assert [t.text for t in sessions[0].turns] == ["hi", "plan"]
    assert [t.role for t in sessions[0].turns] == ["user", "assistant"]


def test_tool_result_errors_become_tool_turn(tmp_path, repo):
    root = tmp_path / "proj"
    write_transcript(root, T.encode_project_dir(repo), "s1", [
        assistant([{"type": "tool_use", "id": "t1", "name": "Bash",
                    "input": {"command": "x"}}], cwd=repo),
        tool_result("t1", "e" * 900, is_error=True, cwd=repo),
        tool_result("t2", [{"type": "text", "text": "ok"}], cwd=repo),
    ])
    sessions, _ = T.load_sessions(repo, root)
    turns = sessions[0].turns
    assert turns[0].tool_uses[0].name == "Bash"
    assert turns[1].role == "tool"
    assert len(turns[1].tool_errors[0]) == 500
    assert turns[2].role == "tool" and turns[2].tool_errors == []


def test_other_repo_cwd_filtered(tmp_path, repo):
    root = tmp_path / "proj"
    write_transcript(root, T.encode_project_dir(repo), "s1", [user("hi", cwd="/elsewhere")])
    write_transcript(root, T.encode_project_dir(repo), "s2",
                     [user("x", cwd=str(repo) + "2", session_id="s2")])
    write_transcript(root, T.encode_project_dir(repo), "s3",
                     [user("in", cwd=repo / "sub", session_id="s3")])
    sessions, _ = T.load_sessions(repo, root)
    assert [s.id for s in sessions] == ["s3"]


def test_fallback_scan_when_encoded_dir_missing(tmp_path, repo):
    root = tmp_path / "proj"
    write_transcript(root, "some-other-dirname", "s1", [user("hi", cwd=repo)])
    sessions, _ = T.load_sessions(repo, root)
    assert len(sessions) == 1


def test_missing_root(tmp_path, repo):
    assert T.load_sessions(repo, tmp_path / "nope") == ([], 0)


def test_repo_path_with_spaces_and_symlink(tmp_path):
    real = tmp_path / "my app"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    root = tmp_path / "proj"
    write_transcript(root, T.encode_project_dir(real), "s1", [user("hi", cwd=str(real))])
    sessions, _ = T.load_sessions(link, root)
    assert len(sessions) == 1


def test_non_utf8_line_skipped_and_counted(tmp_path, repo):
    root = tmp_path / "proj"
    p = write_transcript(root, T.encode_project_dir(repo), "s1",
                         [user("hi", cwd=repo)], extra_bytes=b"\xff\xfe garbage\n")
    sessions, skipped = T.load_sessions(repo, root)
    assert len(sessions) == 1 and skipped == 1
    assert sessions[0].content_hash == hashlib.sha256(p.read_bytes()).hexdigest()


def test_edited_files_relative(tmp_path, repo):
    root = tmp_path / "proj"
    write_transcript(root, T.encode_project_dir(repo), "s1", [
        assistant([
            {"type": "tool_use", "id": "a", "name": "Edit",
             "input": {"file_path": str(repo / "src/a.py")}},
            {"type": "tool_use", "id": "b", "name": "MultiEdit",
             "input": {"file_path": str(repo / "b.py")}},
            {"type": "tool_use", "id": "c", "name": "NotebookEdit",
             "input": {"notebook_path": str(repo / "n.ipynb")}},
            {"type": "tool_use", "id": "d", "name": "Write",
             "input": {"file_path": "/etc/outside"}},
            {"type": "tool_use", "id": "e", "name": "Read",
             "input": {"file_path": str(repo / "r.py")}},
        ], cwd=repo)])
    sessions, _ = T.load_sessions(repo, root)
    assert T.edited_files(sessions[0].turns[0], repo) == {"src/a.py", "b.py", "n.ipynb"}



def test_enguru_cut_index():
    def turn(i, role, text, tool_uses=()):
        return T.Turn(id=str(i), session_id="s", timestamp="", role=role, text=text,
                      tool_uses=list(tool_uses))
    cli = T.ToolUse("Bash", {"command": 'python3 "/p/scripts/enguru.py" next'})
    s = T.Session("s", None, "h", [turn(0, "user", "hi"), turn(1, "assistant", "ok", [cli]),
                                    turn(2, "user", "later")])
    assert T.enguru_cut_index(s) == 1
    assert T.is_enguru_session(s)
    clean = T.Session("s", None, "h", [turn(0, "user", "hi")])
    assert T.enguru_cut_index(clean) is None
    assert not T.is_enguru_session(clean)
