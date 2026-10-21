#!/usr/bin/env python3
"""End-to-end run of the enguru plugin with a scripted student.

Usage: python3 e2e/run_e2e.py [--model sonnet] [--max-turns 25] [--keep]

1. Builds e2e/sample-app as a real git repo in a temp dir (main with a buggy commit and its
   fix, then a feature branch), and installs e2e/sample-transcripts under a temp transcripts
   root at the encoded project path, rewriting their cwd to the temp repo.
   The interviewer is pointed at that root with ENGURU_TRANSCRIPTS_ROOT rather than a temp
   HOME: the claude CLI keeps its credentials and session store under the real HOME, which
   `--resume` and auth both need.
2. Runs `/enguru:interview`, `/enguru:interview --diff --quick`,
   `/enguru:interview --diff --thorough` and `/enguru:certificate` through
   `claude -p --plugin-dir <temp copy of the plugin without e2e/ and evals/>`, continuing each
   interview with `--resume`.
3. Answers each interviewer turn with a separate tool-less `claude -p` call that plays
   e2e/student_persona.md. The driver itself types `stop` in the first session as soon as
   the ladder has moved to rung 2, and in any session that reaches --max-turns.
   The analyst decides the modules and their order, so the first module does not always
   hold a confirmed finding. Session 1 reaching a ladder is a precondition for (c) and (d):
   when it does not, the driver starts over from a fresh repo (up to --attempts times, all
   recorded in the output) and, after the last attempt, reports (c) and (d) as not exercised.
   The whole run is capped by --max-minutes: past it, an open session gets `stop`, later
   sessions and the certificate are skipped, and the assertions report what is missing.
4. Checks (a)-(f) against .enguru/ and the interviewer transcripts, prints one JSON object,
   saves it to e2e/results/last_run.json, and exits 1 if any assertion fails.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "scripts"))
import common  # noqa: E402  (plugin_dir: the plugin copy without evals/ and e2e/)
from enguru_lib import rubric, transcripts  # noqa: E402

SAMPLE = HERE / "sample-app"
TX_SRC = HERE / "sample-transcripts"
PERSONA = HERE / "student_persona.md"
RESULTS = HERE / "results"
TX_PLACEHOLDER = "/home/dev/teamnotes"
DEV_NAME, DEV_EMAIL = "Sam Rivera", "sam@example.com"

# Exactly what the skills run: python3 (the CLI), git (the analyst's read-only history and the
# certificate's user name), rm (the interview skill clears stale verdict files and failed work
# files under .enguru/work/ with `rm -f`, so an old verdict is never applied), date (session
# dates). Read/Write/Grep/Glob for code and .enguru files, Agent/Task for the subagents.
INTERVIEWER_TOOLS = ("Bash(python3:*) Bash(git:*) Bash(rm:*) Bash(date:*) "
                     "Read Write Grep Glob Agent Task")
FIRST_TURN_TIMEOUT = 3600   # check, digest, analyst builds and verification happen here
TURN_TIMEOUT = 1500         # a later turn can still rebuild a module or a revisit concept
STUDENT_TIMEOUT = 300
STOP_TURN_TIMEOUT = 300     # time allowed for the `stop` turn once the run deadline has passed
DEADLINE = {"at": float("inf")}  # wall-clock cap for the whole run (--max-minutes)
SESSIONS = [("project", "/enguru:interview"),
            ("diff-quick", "/enguru:interview --diff --quick"),
            ("diff-thorough", "/enguru:interview --diff --thorough")]
LEVEL_LABELS = {"not_yet_rated": "Not yet rated", "novice": "Novice", "developing": "Developing",
                "proficient": "Proficient", "senior_ready": "Senior-ready"}
SKILL_NAMES = {"comprehension": "Comprehension", "review_judgment": "Review judgment",
               "defect_detection": "Defect detection", "systems_thinking": "Systems thinking"}
NEXT_CALL = re.compile(r"enguru\.py\"?\s+next\b")


def log(msg):
    print("[e2e %s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), file=sys.stderr, flush=True)


# ------------------------------------------------------------------ setup

def build_sample_repo(dest):
    dest.mkdir(parents=True)
    common.git(dest, "init", "-q", "-b", "main")
    common.git(dest, "config", "user.name", DEV_NAME)
    common.git(dest, "config", "user.email", DEV_EMAIL)
    commits = json.loads((SAMPLE / "COMMITS.json").read_text(encoding="utf-8"))
    branch = "main"
    for i, c in enumerate(commits):
        if c["branch"] != branch:
            common.git(dest, "checkout", "-q", "-b", c["branch"])
            branch = c["branch"]
        for f in c["files"]:
            path, src = (f, "app/" + f) if isinstance(f, str) else (f["path"], f["from"])
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SAMPLE / src, target)
            common.git(dest, "add", path)
        date = "2026-09-%02dT11:00:00Z" % (1 + 2 * i)
        common.git(dest, "commit", "-q", "-m", c["message"],
                   env={"GIT_AUTHOR_NAME": DEV_NAME, "GIT_AUTHOR_EMAIL": DEV_EMAIL,
                        "GIT_COMMITTER_NAME": DEV_NAME, "GIT_COMMITTER_EMAIL": DEV_EMAIL,
                        "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    for p in (SAMPLE / "app").rglob("*"):
        if p.is_file():
            rel = p.relative_to(SAMPLE / "app")
            assert (dest / rel).read_bytes() == p.read_bytes(), "sample tree mismatch: %s" % rel
    return dest


def install_transcripts(repo, root):
    d = root / transcripts.encode_project_dir(repo)
    d.mkdir(parents=True)
    real = str(repo.resolve())
    for src in sorted(TX_SRC.glob("*.jsonl")):
        (d / src.name).write_text(src.read_text(encoding="utf-8").replace(TX_PLACEHOLDER, real),
                                  encoding="utf-8")
    sessions, _ = transcripts.load_sessions(repo, root)
    assert len(sessions) == len(list(TX_SRC.glob("*.jsonl"))), "sample transcripts not matched"
    return d


# ------------------------------------------------------------------ claude calls

def run_claude(args, cwd, timeout, env=None):
    """One `claude -p` call with JSON output. Returns (result_text, ok, seconds)."""
    t0 = time.time()
    timeout = min(timeout, max(DEADLINE["at"] - t0, STOP_TURN_TIMEOUT))
    try:
        proc = subprocess.run(["claude", "-p", "--output-format", "json", *args], cwd=str(cwd),
                              env=env, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return "timeout", False, time.time() - t0
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return (proc.stdout + proc.stderr)[-1000:], False, time.time() - t0
    return str(data.get("result", "")), not data.get("is_error") and proc.returncode == 0, \
        time.time() - t0


def interviewer_turn(repo, env, sid, text, first, model):
    sess = ("--session-id", sid) if first else ("--resume", sid)
    return run_claude(["--plugin-dir", str(common.plugin_dir()), "--model", model,
                       "--allowedTools", INTERVIEWER_TOOLS, *sess, text], repo,
                      FIRST_TURN_TIMEOUT if first else TURN_TIMEOUT, env)


def student_reply(history, model, cwd):
    # `--tools` is variadic: it must be followed by another flag, or it swallows the prompt.
    persona = PERSONA.read_text(encoding="utf-8")
    recent = "\n\n".join("%s: %s" % (who, msg) for who, msg in history[-3:-1])
    prompt = (persona + "\n\n## The interview so far (most recent exchanges)\n\n" +
              (recent or "(this is the interviewer's first message)") +
              "\n\n## The interviewer's latest message\n\n<interviewer>\n%s\n</interviewer>\n\n"
              "Reply with only Sam's next message." % history[-1][1])
    for _ in range(2):
        out, ok, _secs = run_claude(["--tools", "", "--model", model, prompt], cwd, STUDENT_TIMEOUT)
        if ok and out.strip():
            return out.strip()
    return "I don't know"


# ------------------------------------------------------------------ state readers

def progress(repo):
    p = repo / ".enguru" / "progress.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def current_session_id(prog):
    return (prog or {}).get("current", {}) and prog["current"].get("session_id")


def confirmed_findings(repo):
    out = []
    for p in sorted((repo / ".enguru" / "banks").glob("**/*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for f in data.get("findings", []) if isinstance(data, dict) else []:
            if f.get("verdict") == "confirmed":
                out.append(dict(f, bank=str(p.relative_to(repo))))
    return out


def transcript_tool_results(sid, repo):
    """[(command, result_text)] for the Bash calls in the interviewer's main transcript."""
    path = Path.home() / ".claude" / "projects" / transcripts.encode_project_dir(repo) / (sid + ".jsonl")
    if not path.exists():
        return []
    uses, results = [], {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        content = (e.get("message") or {}).get("content")
        if e.get("isSidechain") or not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == "Bash":
                uses.append((b.get("id"), str((b.get("input") or {}).get("command", ""))))
            elif b.get("type") == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                results[b.get("tool_use_id")] = str(c or "")
    return [(cmd, results.get(tid, "")) for tid, cmd in uses]


def refused_calls(sid, repo):
    """Tool calls in a transcript that came back as errors (permission refusals and the like)."""
    path = Path.home() / ".claude" / "projects" / transcripts.encode_project_dir(repo) / (sid + ".jsonl")
    uses, out = {}, []
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        content = (e.get("message") or {}).get("content")
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                uses[b.get("id")] = json.dumps(b.get("input"))[:120]
            elif b.get("type") == "tool_result" and b.get("is_error"):
                out.append({"call": uses.get(b.get("tool_use_id")), "error": str(b.get("content"))[:160]})
    return out


# ------------------------------------------------------------------ interview driver

def run_interview(repo, env, name, command, model, max_turns, student_cwd):
    sid = str(uuid.uuid4())
    log("%s: %s (claude session %s)" % (name, command, sid))
    text, ok, secs = interviewer_turn(repo, env, sid, command, True, model)
    turns = [{"interviewer": text, "ok": ok, "seconds": round(secs)}]
    history = [("Interviewer", text)]
    enguru_sid = current_session_id(progress(repo))
    out = {"name": name, "command": command, "claude_session": sid, "enguru_session": enguru_sid,
           "turns": turns, "stopped_by": None, "end_text": None, "ended": False}
    while True:
        prog = progress(repo)
        if prog and enguru_sid is None:
            enguru_sid = out["enguru_session"] = current_session_id(prog)
        if prog and enguru_sid and not current_session_id(prog):
            last = [s for s in prog["sessions"] if s["id"] == enguru_sid]
            out["ended"] = bool(last and last[0].get("ended"))
            out["end_text"] = history[-1][1]
            break
        if not ok and text == "timeout":
            out["stopped_by"] = "interviewer_timeout"
            break
        reply = None
        if out["stopped_by"]:  # `stop` already sent and the session is still open
            out["stopped_by"] += "+not_ended"
            break
        if name == "project" and prog and any(
                a.get("kind") == "ladder_progress" and a.get("session_id") == enguru_sid
                for a in prog["answers"]):
            reply, out["stopped_by"] = "stop", "persona_stop_mid_ladder"
        elif len(turns) >= max_turns:
            reply, out["stopped_by"] = "stop", "max_turns"
        elif time.time() >= DEADLINE["at"]:
            reply, out["stopped_by"] = "stop", "deadline"
        if reply is None:
            reply = student_reply(history, model, student_cwd)
        log("%s turn %d: student -> %r" % (name, len(turns), reply[:80]))
        history.append(("Sam", reply))
        text, ok, secs = interviewer_turn(repo, env, sid, reply, False, model)
        turns.append({"student": reply, "interviewer": text, "ok": ok, "seconds": round(secs)})
        history.append(("Interviewer", text))
    out["progress_after"] = progress(repo)
    return out


# ------------------------------------------------------------------ assertions

def check(ok, detail, component):
    return {"pass": bool(ok), "detail": detail, "component": None if ok else component}


def assert_a(answers):
    """answers: the evaluated run's records plus those of any discarded attempt."""
    bad = [a for a in answers if (a.get("followups_used") or 0) > 3]
    return check(not bad, "%d answer records (all attempts), %d with followups_used > 3"
                 % (len(answers), len(bad)),
                 "skills/interview/references/grading.md (follow-up limit) or progress.record")


def assert_b(prog):
    bad = [a for a in prog["answers"] if a.get("revealed")
           and (not isinstance(a.get("ladder_length"), int)
                or not isinstance(a.get("ladder_rung_reached"), int)
                or a["ladder_rung_reached"] < a["ladder_length"])]
    reveals = [a for a in prog["answers"] if a.get("revealed")]
    return check(not bad, "%d revealed ladder records, %d revealed before the last rung "
                 "(or without a ladder_length/rung)"
                 % (len(reveals), len(bad)),
                 "skills/interview/references/discovery.md (no reveal while rungs remain)")


def stopped_finding(s1, findings):
    """The confirmed finding whose ladder session 1 stopped on, and its saved rung."""
    prog = s1["progress_after"] or {"answers": [], "findings": {}}
    sid = s1["enguru_session"]
    cids = [a["concept_id"] for a in prog["answers"]
            if a.get("kind") == "ladder_progress" and a.get("session_id") == sid]
    if not cids:
        return None, None
    cid = cids[-1]
    f = next((x for x in findings if x["concept_id"] == cid), None)
    return f, prog["findings"].get(cid, {}).get("rung")


def assert_c(s1, finding):
    if s1["stopped_by"] != "persona_stop_mid_ladder" or finding is None:
        return check(False, "not exercised: session 1 never stopped partway through a ladder "
                     "(stopped_by=%s)" % s1["stopped_by"],
                     "project-analyst / finding-verifier (no confirmed finding reached) or driver")
    file = finding["lines"]["file"]
    text = s1["end_text"] or ""
    ok = s1["ended"] and finding.get("severity") == "critical" and file in text
    return check(ok, "session ended=%s, severity=%s, end text names %s: %s"
                 % (s1["ended"], finding.get("severity"), file, file in text),
                 "skills/interview/SKILL.md step 6 / discovery.md section 6 (disclosure) "
                 "or progress.end_session (undisclosed_critical)")


def first_next(repo, claude_sid):
    for cmd, res in transcript_tool_results(claude_sid, repo):
        if NEXT_CALL.search(cmd):
            return common.last_json_line(res) or {"unparsed": res[:300]}
    return None


def assert_d(repo, s2, finding, rung):
    if finding is None or rung is None:
        return check(False, "not exercised: no saved ladder rung from session 1",
                     "project-analyst / finding-verifier or driver"), None
    nxt = first_next(repo, s2["claude_session"])
    ok = (isinstance(nxt, dict) and nxt.get("action") == "ladder"
          and nxt.get("concept_id") == finding["concept_id"] and nxt.get("rung") == rung)
    got = None if nxt is None else {k: nxt.get(k) for k in ("action", "concept_id", "rung")}
    return check(ok, "saved %s at rung %s; session 2 first next: %s"
                 % (finding["concept_id"], rung, got),
                 "progress.next_item (resume the in-progress ladder)"), got


def assert_e(repo, prog):
    certs = sorted((repo / ".enguru" / "certificates").glob("????-??-??.json"))
    if not certs:
        return check(False, "no certificate JSON written", "skills/certificate/SKILL.md"), None
    cert = json.loads(certs[-1].read_text(encoding="utf-8"))
    md_path = certs[-1].with_suffix(".md")
    md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    want = {s: v["level"] for s, v in rubric.skill_levels(prog["answers"], rubric.load_config()).items()}
    got = {s: v.get("level") for s, v in cert.get("skills", {}).items()}
    in_md = {}
    for skill, level in want.items():
        name, label = SKILL_NAMES[skill], LEVEL_LABELS[level]
        in_md[skill] = bool(re.search(re.escape(name) + r"[\s\S]{0,300}?" + re.escape(label), md))
    ok = want == got and bool(md) and all(in_md.values())
    detail = {"recomputed": want, "certificate": got, "md": str(md_path.relative_to(repo)) if md else None,
              "label_in_md": in_md,
              "disclaimer_in_md": "AI-produced self-assessment for personal growth, not a credential." in md}
    return check(ok, detail, "rubric.cert_levels (levels) or skills/certificate/SKILL.md (labels)"), detail


def assert_f(repo):
    status = common.git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()
    outside = [ln for ln in status if ln[3:].strip() != ".gitignore"]
    ignored = subprocess.run(["git", "check-ignore", "-q", ".enguru"], cwd=str(repo)).returncode == 0
    return check(not outside and ignored, {"status": status, "enguru_ignored": ignored},
                 "a skill or agent wrote outside .enguru/ (see status)")


# ------------------------------------------------------------------ main

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=common.MODEL)
    ap.add_argument("--max-turns", type=int, default=25)
    ap.add_argument("--keep", action="store_true", help="keep the temp dir for debugging")
    ap.add_argument("--max-minutes", type=float, default=120,
                    help="wall-clock cap for the whole run; sessions still open are stopped")
    ap.add_argument("--attempts", type=int, default=3,
                    help="fresh starts allowed when session 1 reaches no ladder")
    args = ap.parse_args(argv)
    t0 = time.time()
    DEADLINE["at"] = t0 + args.max_minutes * 60
    attempts, discarded_answers = [], []
    for attempt in range(1, args.attempts + 1):
        tmp = Path(tempfile.mkdtemp(prefix="enguru-e2e-")).resolve()
        try:
            repo = build_sample_repo(tmp / "teamnotes")
            tx_root = tmp / "home" / ".claude" / "projects"
            install_transcripts(repo, tx_root)
            student_cwd = tmp / "student"
            student_cwd.mkdir()
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        env = dict(os.environ, ENGURU_TRANSCRIPTS_ROOT=str(tx_root))
        log("attempt %d: repo %s, plugin copy %s" % (attempt, repo, common.plugin_dir()))
        first = run_interview(repo, env, *SESSIONS[0], args.model, args.max_turns, student_cwd)
        modules = [m.get("id") for m in (json.loads((repo / ".enguru/banks/project/topic-map.json")
                                                    .read_text(encoding="utf-8")).get("modules", [])
                                         if (repo / ".enguru/banks/project/topic-map.json").exists()
                                         else [])]
        attempts.append({"attempt": attempt, "session1_stopped_by": first["stopped_by"],
                         "modules": modules})
        if (first["stopped_by"] == "persona_stop_mid_ladder" or attempt == args.attempts
                or time.time() >= DEADLINE["at"]):
            break
        discarded_answers += (progress(repo) or {}).get("answers", [])
        # Precondition, not an assertion: the analyst chose the modules and the first one held
        # no confirmed finding, so session 1 had no ladder to stop on. Start over.
        log("attempt %d: session 1 reached no ladder (first module %s); retrying"
            % (attempt, modules[:1]))
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    try:
        sessions = [first]
        for name, cmd in SESSIONS[1:]:
            if time.time() >= DEADLINE["at"]:
                log("%s: skipped, run deadline reached" % name)
                sessions.append({"name": name, "command": cmd, "claude_session": "",
                                 "enguru_session": None, "turns": [], "stopped_by": "deadline_skipped",
                                 "end_text": None, "ended": False, "progress_after": None})
                continue
            sessions.append(run_interview(repo, env, name, cmd, args.model, args.max_turns,
                                          student_cwd))
        cert_sid, cert_text, cert_ok = "", "skipped: run deadline reached", False
        if time.time() < DEADLINE["at"]:
            log("certificate")
            cert_sid = str(uuid.uuid4())
            cert_text, cert_ok, _ = run_claude(
                ["--plugin-dir", str(common.plugin_dir()), "--model", args.model, "--allowedTools",
                 INTERVIEWER_TOOLS, "--session-id", cert_sid, "/enguru:certificate"],
                repo, TURN_TIMEOUT, env)

        prog = progress(repo) or {"answers": [], "findings": {}, "sessions": []}
        findings = confirmed_findings(repo)
        finding, rung = stopped_finding(sessions[0], findings)
        planted = json.loads((SAMPLE / "PLANTED.json").read_text(encoding="utf-8"))
        d_result, d_next = assert_d(repo, sessions[1], finding, rung)
        e_result, _ = assert_e(repo, prog)
        assertions = {
            "a_followups_le_3": assert_a(prog["answers"] + discarded_answers),
            "b_no_early_reveal": assert_b(prog),
            "c_stop_discloses_file": assert_c(sessions[0], finding),
            "d_resume_at_saved_rung": d_result,
            "e_certificate_levels": e_result,
            "f_only_gitignore_changed": assert_f(repo),
        }
        result = {
            "ok": all(a["pass"] for a in assertions.values()),
            "assertions": assertions,
            "evidence": {
                "model": args.model,
                "attempts": attempts,
                "minutes": round((time.time() - t0) / 60, 1),
                "sessions": [{"name": s["name"], "command": s["command"],
                              "claude_session": s["claude_session"],
                              "enguru_session": s["enguru_session"], "turns": len(s["turns"]),
                              "turn_ok": [t["ok"] for t in s["turns"]],
                              "stopped_by": s["stopped_by"], "ended": s["ended"],
                              "students": [t.get("student") for t in s["turns"][1:]],
                              "end_text": s["end_text"],
                              "errored_tool_calls": refused_calls(s["claude_session"], repo)}
                             for s in sessions],
                "stopped_finding": finding and {k: finding.get(k) for k in
                                                ("concept_id", "severity", "lines", "bank")},
                "saved_rung": rung,
                "session2_first_next": d_next,
                "matches_planted": bool(finding and finding["lines"]["file"] in {p["file"] for p in planted}),
                "confirmed_findings": [{k: f.get(k) for k in ("concept_id", "severity", "lines", "bank")}
                                       for f in findings],
                "answers": [{k: a.get(k) for k in ("session_id", "kind", "concept_id", "question_kind",
                                                   "style", "outcome", "followups_used", "hints_used",
                                                   "ladder_rung_reached", "ladder_length", "revealed")}
                            for a in prog["answers"]],
                "finding_states": prog.get("findings"),
                "record_calls_refused": [
                    c for s in sessions for c in refused_calls(s["claude_session"], repo)
                    if c["call"] and re.search(r"enguru\.py\W* (record|keylog)", c["call"])
                    and not c["error"].startswith("Exit code")],
                "certificate_turn_ok": cert_ok,
                "certificate_errored_tool_calls": refused_calls(cert_sid, repo),
                "certificate_reply": cert_text[-1500:],
            },
        }
    finally:
        if args.keep:
            log("kept %s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    text = json.dumps(result, indent=1)
    print(text)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "last_run.json").write_text(text + "\n", encoding="utf-8")
    failed = [k for k, v in assertions.items() if not v["pass"]]
    if failed:
        print("failed: %s" % ", ".join("%s -> %s" % (k, assertions[k]["component"]) for k in failed),
              file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
