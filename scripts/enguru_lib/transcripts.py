"""Transcript adapter: the ONLY module that parses Claude Code transcript JSONL."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Set, Tuple

DEFAULT_ROOT = Path.home() / ".claude" / "projects"
_EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path",
               "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
_ERR_MAX = 500


@dataclass
class ToolUse:
    name: str
    input: dict


@dataclass
class Turn:
    id: str
    session_id: str
    timestamp: str
    role: str
    text: str
    tool_uses: List[ToolUse] = field(default_factory=list)
    tool_errors: List[str] = field(default_factory=list)
    is_sidechain: bool = False
    cwd: str = ""


@dataclass
class Session:
    id: str
    path: Path
    content_hash: str
    turns: List[Turn] = field(default_factory=list)


def encode_project_dir(repo: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(repo).resolve()))


def _inside(path: str, repo_real: str) -> bool:
    if not path:
        return False
    p = os.path.realpath(path)
    return p == repo_real or p.startswith(repo_real.rstrip(os.sep) + os.sep)


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _parse_turn(entry: dict, file_stem: str):
    msg = entry.get("message")
    if entry.get("type") not in ("user", "assistant") or not isinstance(msg, dict):
        return None
    role = msg.get("role") or entry["type"]
    content = msg.get("content")
    texts, uses, errors = [], [], []
    only_results = False
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        only_results = bool(blocks) and all(b.get("type") == "tool_result" for b in blocks)
        for b in blocks:
            t = b.get("type")
            if t == "text":
                texts.append(b.get("text", ""))
            elif t == "tool_use":
                inp = b.get("input")
                uses.append(ToolUse(str(b.get("name", "")), inp if isinstance(inp, dict) else {}))
            elif t == "tool_result" and b.get("is_error"):
                errors.append(_result_text(b.get("content"))[:_ERR_MAX])
    if role == "user" and only_results:
        role = "tool"
    return Turn(
        id=str(entry.get("uuid", "")),
        session_id=str(entry.get("sessionId") or file_stem),
        timestamp=str(entry.get("timestamp", "")),
        role=role,
        text="\n".join(texts),
        tool_uses=uses,
        tool_errors=errors,
        is_sidechain=bool(entry.get("isSidechain", False)),
        cwd=str(entry.get("cwd", "")),
    )


def _load_file(path: Path, repo_real: str):
    raw = path.read_bytes()
    skipped = 0
    turns = []
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, ValueError):
            skipped += 1
            continue
        if not isinstance(entry, dict):
            skipped += 1
            continue
        turn = _parse_turn(entry, path.stem)
        if turn is not None and _inside(turn.cwd, repo_real):
            turns.append(turn)
    if not turns:
        return None, skipped
    sid = turns[0].session_id
    return Session(sid, path, hashlib.sha256(raw).hexdigest(), turns), skipped


def load_sessions(repo: Path, root: Path = DEFAULT_ROOT) -> Tuple[List[Session], int]:
    root = Path(root)
    if not root.is_dir():
        return [], 0
    repo_real = os.path.realpath(str(repo))
    enc = root / encode_project_dir(repo)
    dirs = [enc] if enc.is_dir() else sorted(d for d in root.iterdir() if d.is_dir())
    sessions, skipped = [], 0
    for d in dirs:
        for f in sorted(d.glob("*.jsonl")):
            try:
                s, n = _load_file(f, repo_real)
            except OSError:
                continue
            skipped += n
            if s is not None:
                sessions.append(s)
    return sessions, skipped


_ENGURU_CLI = re.compile(
    r"enguru\.py[\"']?\s+(?:check|digest|bank|session|next|record|status|keylog|cert)\b")
_ENGURU_SKILL = re.compile(r"<command-name>/?enguru:(?:interview|certificate)\b")


def is_enguru_session(session: Session) -> bool:
    """True for the developer's own enguru interview/certificate sessions, which are
    not coding history and must not feed the digest."""
    for t in session.turns:
        if t.role == "user" and _ENGURU_SKILL.search(t.text):
            return True
        for tu in t.tool_uses:
            if tu.name == "Bash" and _ENGURU_CLI.search(str(tu.input.get("command", ""))):
                return True
    return False


def edited_files(turn: Turn, repo: Path) -> Set[str]:
    repo_real = os.path.realpath(str(repo))
    out = set()
    for tu in turn.tool_uses:
        key = _EDIT_TOOLS.get(tu.name)
        p = tu.input.get(key) if key else None
        if not isinstance(p, str) or not p:
            continue
        if not os.path.isabs(p):
            p = os.path.join(turn.cwd or repo_real, p)
        real = os.path.realpath(p)
        if _inside(real, repo_real) and real != repo_real:
            out.add(os.path.relpath(real, repo_real))
    return out
