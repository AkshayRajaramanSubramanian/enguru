"""Blast-radius candidates: changed symbols from diff hunks + git grep callers."""
import os
import re
from collections import Counter

from .gitinfo import _git
from .redact import is_env_file

_JS = [r"function\s+(\w+)",
       r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>",
       r"class\s+(\w+)",
       r"^\s*(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{"]
_PATTERNS = {
    ".py": [r"^\s*(?:async\s+)?def\s+(\w+)", r"^\s*class\s+(\w+)"],
    ".js": _JS, ".jsx": _JS, ".ts": _JS, ".tsx": _JS,
    ".go": [r"func\s+(?:\([^)]*\)\s*)?(\w+)"],
    ".java": [r"(?:public|private|protected)[\w\s<>\[\],]*\s(\w+)\s*\("],
    ".rb": [r"def\s+(?:self\.)?(\w+)", r"class\s+(\w+)"],
    ".rs": [r"fn\s+(\w+)", r"(?:struct|enum|trait)\s+(\w+)"],
}
_COMPILED = {ext: [re.compile(p) for p in pats]
             for ext, pats in _PATTERNS.items()}
_JS_EXTS = {".js", ".jsx", ".ts", ".tsx"}
# The JS method pattern also matches control-flow headers like `if (x) {`.
_JS_CONTROL = {"if", "for", "while", "switch", "catch", "with", "return"}

_IDENT = re.compile(r"[A-Za-z_]\w{3,}")
_STOPLIST = {"return", "function", "class", "const", "import", "from", "self",
             "this", "true", "false", "null", "None", "string", "number"}
_DROP = {"main", "init", "test", "setUp", "constructor", "render"}
_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|_test\.|\.test\.|\.spec\.|(^|/)test_")


def _changed_lines(hunk):
    return [l[1:] for l in hunk.lines if l[:1] in ("+", "-")]


def changed_symbols(hunks) -> set:
    found = set()
    fallback = Counter()
    for h in hunks:
        ext = os.path.splitext(h.path)[1].lower()
        lines = _changed_lines(h)
        pats = _COMPILED.get(ext)
        if pats is None:
            for line in lines:
                for m in _IDENT.findall(line):
                    if m not in _STOPLIST:
                        fallback[m] += 1
            continue
        for line in lines:
            for pat in pats:
                m = pat.search(line)
                if m and not (ext in _JS_EXTS and m.group(1) in _JS_CONTROL):
                    found.add(m.group(1))
    ranked = sorted(((n, c) for n, c in fallback.items() if n not in _DROP),
                    key=lambda nc: (-nc[1], nc[0]))
    found.update(n for n, _ in ranked[:10])
    return {s for s in found if len(s) >= 3 and s not in _DROP}


def blast_radius(repo, symbols, changed_paths, cap: int = 50) -> list:
    results = set()
    for sym in sorted(symbols):
        code, out = _git(repo, "grep", "-z", "-n", "-w", "-F", "-e", sym,
                         check=False)
        if code not in (0, 1):
            raise RuntimeError("git grep failed for %r" % sym)
        for rec in out.split("\n"):
            parts = rec.split("\0", 2)
            if len(parts) < 3:
                continue
            path, num = parts[0], parts[1]
            if (path in changed_paths or is_env_file(path)
                    or not num.isdigit()):
                continue
            results.add((sym, path, int(num)))
    rows = [{"path": p, "line": n, "symbol": s,
             "kind": "test" if _TEST_PATH.search(p) else "caller"}
            for s, p, n in sorted(results)]
    return rows[:cap]
