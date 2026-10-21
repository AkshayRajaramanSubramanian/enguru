"""Question-bank validation, evidence sealing, staleness and verdicts."""
import difflib
import hashlib
import subprocess
from pathlib import Path

from . import paths, schema
from .redact import is_env_file

MAX_FLAW_ORIGINAL_LINES = 40
MAX_FLAW_CHANGED_LINES = 5


# ---------------------------------------------------------------- references

def parse_ref(ref):
    """Normalise an evidence ref (dict or string form) to a dict, or None."""
    if isinstance(ref, dict):
        return ref
    if not isinstance(ref, str) or not ref:
        return None
    if ref.startswith("commit:"):
        return {"commit": ref[len("commit:"):]}
    if ref.startswith("event:"):
        return {"event": ref[len("event:"):]}
    path, sep, tail = ref.rpartition(":")
    if sep and path and tail:
        a, dash, b = tail.partition("-")
        if a.isdigit() and (not dash or b.isdigit()):
            return {"file": path, "lines": [int(a), int(b) if dash else int(a)]}
    return {"file": ref}


def _refs_in_answer_key(key):
    for section in ("must_have", "nice_to_have", "misconceptions"):
        for item in key.get(section) or []:
            if isinstance(item, dict) and "evidence_ref" in item:
                r = parse_ref(item["evidence_ref"])
                if r:
                    yield r


def _concept_refs(concept, questions, findings):
    """All refs tied to a concept: its own, its questions', its finding's."""
    out = [r for r in (parse_ref(x) for x in concept.get("evidence_refs") or []) if r]
    for q in questions:
        if q.get("concept_id") != concept.get("id"):
            continue
        out.extend(_refs_in_answer_key(q.get("answer_key") or {}))
        loc = (q.get("flaw") or {}).get("location")
        if isinstance(loc, dict) and "file" in loc:
            out.append({"file": loc["file"]})
    for f in findings:
        if f.get("concept_id") == concept.get("id") and isinstance(f.get("lines"), dict):
            out.append({"file": f["lines"].get("file")})
    return out


def _safe_path(repo, rel):
    """Return the resolved Path for a repo-relative path, or a problem string."""
    if not isinstance(rel, str) or not rel:
        return None, "empty file path"
    if rel.startswith("/") or rel.startswith("\\") or (len(rel) > 1 and rel[1] == ":"):
        return None, "absolute path not allowed: %s" % rel
    if ".." in Path(rel).parts:
        return None, "path escapes the repo: %s" % rel
    if is_env_file(rel):
        return None, "env file not allowed: %s" % rel
    root = Path(repo).resolve()
    p = (root / rel).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None, "path escapes the repo: %s" % rel
    return p, None


def _line_count(p):
    try:
        return len(p.read_bytes().decode("utf-8", "replace").splitlines())
    except OSError:
        return None


def _check_file_lines(repo, file, lines):
    """Return a problem string or None."""
    p, err = _safe_path(repo, file)
    if err:
        return err
    if not p.is_file():
        return "file not found: %s" % file
    if lines is None:
        return None
    if (not isinstance(lines, (list, tuple)) or len(lines) != 2
            or not all(isinstance(n, int) and not isinstance(n, bool) for n in lines)):
        return "lines must be [start, end] integers for %s" % file
    n = _line_count(p)
    a, b = lines
    if not (1 <= a <= b <= n):
        return "lines %s out of range 1..%d in %s" % (list(lines), n, file)
    return None


def _commit_exists(repo, sha):
    if not isinstance(sha, str) or not sha or sha.startswith("-"):
        return False
    try:
        r = subprocess.run(["git", "cat-file", "-e", sha + "^{commit}"], cwd=str(repo),
                           capture_output=True)
    except OSError:
        return False
    return r.returncode == 0


def _check_ref(repo, ref, event_ids):
    probs = []
    if "file" in ref:
        p = _check_file_lines(repo, ref["file"], ref.get("lines"))
        if p:
            probs.append(p)
    if "commit" in ref and not _commit_exists(repo, ref["commit"]):
        probs.append("unknown commit: %s" % ref["commit"])
    if "event" in ref and ref["event"] not in event_ids:
        probs.append("unknown event id: %s" % ref["event"])
    return probs


# ---------------------------------------------------------------- validation

def flaw_region_ok(original: str, flawed: str) -> bool:
    a, b = original.splitlines(), flawed.splitlines()
    if len(a) > MAX_FLAW_ORIGINAL_LINES or original == flawed:
        return False
    ops = [o for o in difflib.SequenceMatcher(None, a, b).get_opcodes() if o[0] != "equal"]
    if len(ops) != 1:
        return False
    _, i1, i2, j1, j2 = ops[0]
    return (i2 - i1) <= MAX_FLAW_CHANGED_LINES and (j2 - j1) <= MAX_FLAW_CHANGED_LINES


def is_topic_map(data):
    return isinstance(data, dict) and "modules" in data and "questions" not in data


def validate_bank(repo, bank: dict, digest: dict) -> list:
    problems = []

    def add(item_id, reason):
        problems.append({"item_id": item_id, "reason": reason})

    topic = is_topic_map(bank)
    errs = schema.validate(bank, schema.load_schema("topic-map" if topic else "bank"))
    for e in errs:
        add("$", e)
    if errs or not isinstance(bank, dict):
        return problems
    event_ids = {e.get("id") for e in (digest or {}).get("events", []) if isinstance(e, dict)}

    def check(item_id, ref):
        for p in _check_ref(repo, ref, event_ids):
            add(item_id, p)

    if topic:
        concepts = [c for m in bank["modules"] for c in m["concepts"]]
        questions, findings = [], []
    else:
        concepts, questions, findings = bank["concepts"], bank["questions"], bank["findings"]

    # Ids end up in work-file names and shell arguments (rm -f, --ids, --out).
    def safe(item_id, value, what):
        if not paths.safe_id(value):
            add(item_id, "unsafe id in %s: %r (letters, digits, . _ - only; no ..)"
                % (what, value))

    for m in bank.get("modules", []) if topic else []:
        safe(m["id"], m["id"], "module id")
    for c in concepts:
        safe(c["id"], c["id"], "concept id")
        if "module" in c:
            safe(c["id"], c["module"], "concept module")
    for q in questions:
        safe(q["id"], q["id"], "question id")
        safe(q["id"], q["concept_id"], "question concept_id")
    for f in findings:
        safe(f["concept_id"], f["concept_id"], "finding concept_id")

    for c in concepts:
        for r in c["evidence_refs"]:
            check(c["id"], r)
    for q in questions:
        key = q["answer_key"]
        if not key.get("must_have"):
            add(q["id"], "must_have is empty")
        for r in _refs_in_answer_key(key):
            check(q["id"], r)
        flaw = q.get("flaw")
        if flaw:
            loc = flaw["location"]
            p = _check_file_lines(repo, loc["file"], loc["lines"])
            if p:
                add(q["id"], "flaw location: " + p)
            if not flaw_region_ok(flaw["original"], flaw["flawed"]):
                add(q["id"], "flaw region not a single small change")
        if q.get("fix_commit") and not _commit_exists(repo, q["fix_commit"]):
            add(q["id"], "unknown commit: %s" % q["fix_commit"])
    for f in findings:
        L = f["lines"]
        p = _check_file_lines(repo, L["file"], [L["start"], L["end"]])
        if p:
            add(f["concept_id"], p)
    if not topic:
        with_q = {q["concept_id"] for q in questions}
        ids = {c["id"] for c in concepts}
        for f in findings:
            if f["concept_id"] not in ids:
                add(f["concept_id"], "finding refers to unknown concept")
        for c in concepts:
            has_finding = c["kind"] == "finding" or any(f["concept_id"] == c["id"] for f in findings)
            if has_finding and c["id"] not in with_q:
                add(c["id"], "finding concept has no fallback (non-finding) question")
    return problems


# ------------------------------------------------------------------- sealing

def _hash_file(p):
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def _bank_concepts(data):
    if is_topic_map(data):
        return [c for m in data["modules"] for c in m["concepts"]]
    return data["concepts"]


def shape_problem(data, allow_topic_map=False):
    """Why data is not a usable bank (or, if allowed, topic map), or None.
    A cheap structural guard for the mutating commands, not full validation."""
    def objs(v, key):
        return isinstance(v, list) and all(isinstance(x, dict) and key in x for x in v)

    if not isinstance(data, dict):
        return "expected a JSON object"
    if is_topic_map(data):
        if not allow_topic_map:
            return "this is a topic map, not a question bank"
        mods = data["modules"]
        if not isinstance(mods, list) or not all(
                isinstance(m, dict) and objs(m.get("concepts"), "id") for m in mods):
            return "malformed topic map: modules need concepts with ids"
        return None
    for section, key in (("concepts", "id"), ("questions", "id"), ("findings", "concept_id")):
        if not objs(data.get(section), key):
            return "malformed bank: '%s' must be a list of objects with '%s'" % (section, key)
    if not all("concept_id" in q for q in data["questions"]):
        return "malformed bank: every question needs 'concept_id'"
    return None


def seal(repo, bank_path, ids=None) -> None:
    """Store evidence hashes; with ids, only for those concepts."""
    data = paths.read_json(bank_path)
    questions = data.get("questions", [])
    findings = data.get("findings", [])
    only = set(ids) if ids is not None else None
    for c in _bank_concepts(data):
        if only is not None and c["id"] not in only:
            continue
        hashes = {}
        for ref in _concept_refs(c, questions, findings):
            f = ref.get("file")
            if not f:
                continue
            p, err = _safe_path(repo, f)
            if err or not p.is_file():
                continue
            hashes[f] = _hash_file(p)
        c["evidence_hashes"] = hashes
    paths.atomic_write_json(Path(bank_path), data)


def stale_concepts(repo, concepts) -> tuple:
    stale, retired = [], []
    for c in concepts:
        hashes = c.get("evidence_hashes") or {}
        if not hashes:
            continue
        missing = changed = 0
        for f, h in hashes.items():
            p, err = _safe_path(repo, f)
            if err or not p.is_file():
                missing += 1
            elif _hash_file(p) != h:
                changed += 1
        if missing == len(hashes):
            retired.append(c["id"])
        elif missing or changed:
            stale.append(c["id"])
    return stale, retired


# ------------------------------------------------------------- drop / verdicts

def drop_items(bank_path, ids) -> None:
    ids = set(ids)
    data = paths.read_json(bank_path)
    dropped_concepts = {c["id"] for c in data["concepts"] if c["id"] in ids}
    data["concepts"] = [c for c in data["concepts"] if c["id"] not in dropped_concepts]
    data["questions"] = [q for q in data["questions"]
                         if q["id"] not in ids and q["concept_id"] not in dropped_concepts]
    data["findings"] = [f for f in data["findings"] if f["concept_id"] not in dropped_concepts]
    paths.atomic_write_json(Path(bank_path), data)


def _read_verdict(repo, cid):
    p = Path(repo) / ".enguru" / "work" / "verdicts" / (cid + ".json")
    try:
        v = paths.read_json(p)
    except (OSError, paths.StateCorrupt, ValueError):
        return None
    if not isinstance(v, dict) or schema.validate(v, schema.load_schema("verdict")):
        return None
    if v["mode"] != "verify" or v["concept_id"] != cid:
        return None
    return v["verdict"]


def apply_verdicts(repo, bank_path) -> dict:
    data = paths.read_json(bank_path)
    result = {"confirmed": [], "refuted": [], "downgraded": []}
    concepts = {c["id"]: c for c in data["concepts"]}
    keep = []
    drop_concepts = set()
    for f in data["findings"]:
        cid = f["concept_id"]
        verdict = _read_verdict(repo, cid)
        c = concepts.get(cid)
        if verdict == "confirmed":
            f["verdict"] = "confirmed"
            if c:
                c["kind"] = "finding"
            result["confirmed"].append(cid)
            keep.append(f)
            continue
        if c:
            c["kind"] = "quiz"
        if verdict == "refuted":
            result["refuted"].append(cid)
            if c and not any(q["concept_id"] == cid for q in data["questions"]):
                drop_concepts.add(cid)
        else:
            result["downgraded"].append(cid)
    data["findings"] = keep
    data["concepts"] = [c for c in data["concepts"] if c["id"] not in drop_concepts]
    paths.atomic_write_json(Path(bank_path), data)
    return result


def finding_of(bank_path, concept_id):
    """The finding for concept_id in the bank, or None."""
    for f in paths.read_json(bank_path)["findings"]:
        if f["concept_id"] == concept_id:
            return f
    return None


# --------------------------------------------------------------------- merge

def _merge_by(existing, incoming, key):
    pos = {item.get(key): i for i, item in enumerate(existing)}
    out = list(existing)
    for item in incoming:
        k = item.get(key)
        if k in pos:
            out[pos[k]] = item
        else:
            pos[k] = len(out)
            out.append(item)
    return out


def merge_partial(bank_path, partial_path) -> None:
    """Merge a bank-shaped partial: concepts and questions replace by id,
    findings replace by concept_id; new items are appended. A missing target
    bank is created from the partial."""
    part = paths.read_json(partial_path)
    if Path(bank_path).exists():
        data = paths.read_json(bank_path)
    else:
        Path(bank_path).parent.mkdir(parents=True, exist_ok=True)
        data = {"scope_key": part.get("scope_key", "project"), "concepts": [], "questions": [],
                "findings": []}
    for section, key in (("concepts", "id"), ("questions", "id"), ("findings", "concept_id")):
        data[section] = _merge_by(data.get(section, []), part.get(section) or [], key)
    paths.atomic_write_json(Path(bank_path), data)
