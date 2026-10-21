# Guided discovery for real findings

Used for every `ladder` action. A verifier has independently confirmed this is a real issue in the developer's code. The goal is that the developer finds it, names it and fixes it in their own head, not that you tell them.

The action gives `concept_id`, `rung` (1-based), `step` (`question`, `expected_realization`), `ladder_length`, `severity`, `announce`, and `finding` (`lines`, `consequence`, `fix`, `ladder`). Keep `finding.consequence`, `finding.fix`, `finding.lines` and later rungs to yourself until the end.

## 1. Announce

If `announce` is true, say this line first, verbatim, on its own:

> This next one is a real issue in your code, not an exercise.

Then present `step.question`. (When `announce` is false you are resuming or moving down; just present the rung's question. When resuming in a new session, say in one line that you are picking up the real issue from last time.)

## 2. Moving down the ladder

Judge each answer against `step.expected_realization`, and against `finding.consequence` in case the developer has already found the issue.

- **The developer names the issue and its consequence** (at any rung): go to section 3 at this rung.
- **They reach this rung's realization but not the issue yet:** ask once, without new information, what follows from that ("and what could go wrong there?"). If that names the issue, go to section 3; otherwise move down as below.
- **A wrong answer, "I don't know", or `hint`:** if this is the last rung (`rung == ladder_length`), go to section 4. Otherwise move to the next, narrower rung. Say briefly that it isn't that (no explanation of why, which would reveal it), then record the new rung and call `next`, which returns that rung:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/enguru.py" record <<'JSON'
{"kind": "ladder_progress", "session_id": "<session_id>", "concept_id": "<concept_id>",
 "question_kind": "finding", "ladder_rung_reached": <rung + 1>, "ladder_length": <ladder_length>,
 "scope": "<scope>", "range_id": <range_id or null>, "intensity": <intensity or null>,
 "timestamp": "<YYYY-MM-DDTHH:MM:SSZ>"}
JSON
```

Rules:
- **No reveal while rungs remain.** Never state the issue, the consequence, the lines or the fix before the developer does, or before the last resort.
- The three-follow-up limit does not apply to ladders.
- Recording each rung means a `stop` mid-ladder resumes at the same rung next session.

## 3. Completion: name it, then fix it

1. **Name it.** The developer states the problem and its consequence in their own words. If one half is missing (the problem without what it causes, or vice versa), ask for it; do not supply it.
2. **Fix it.** Ask how they would fix it. Check the proposal against the code (read `finding.lines.file` around the cited lines and its callers as needed) and against `finding.fix`, **including new problems the fix would introduce** (a broken caller, a new race, a check in the wrong place, swallowed errors). A different fix that the code shows is sound is valid. `fix_valid` is the verdict on their first proposal; if it is invalid or they don't know, say what is wrong with it and walk through a sound fix. Enguru never edits their code; the fix is theirs to make.

Then record:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/enguru.py" record <<'JSON'
{"kind": "answer", "session_id": "<session_id>", "concept_id": "<concept_id>",
 "question_id": null, "question_kind": "finding",
 "style": "review_diff", "skill": "review_judgment", "difficulty": "advanced",
 "outcome": "correct", "must_haves_hit": [], "misconception_stated": false,
 "hints_used": 0, "followups_used": 0,
 "ladder_rung_reached": <rung where they named it>, "ladder_length": <ladder_length>,
 "fix_valid": <true|false>, "revealed": false,
 "scope": "<scope>", "range_id": <range_id or null>, "intensity": <intensity or null>,
 "timestamp": "<YYYY-MM-DDTHH:MM:SSZ>"}
JSON
```

Close with the file and lines and one sentence on why it matters, then call `next`.

## 4. Last resort

If the developer is still stuck after the final rung (`rung == ladder_length`), explain the issue, its consequence and the fix, framed around the steps they worked through ("you saw that ..., and at the last step ..., which means ..."). Then record the same answer record as section 3 with `"outcome": "dont_know"`, `"ladder_rung_reached": <ladder_length>`, `"fix_valid": null`, `"revealed": true`. The finding returns to the revisit queue for a later session. Call `next`.

## 5. Skip and stop

- **`skip`:** do not explain anything. Record the section 3 record with `"outcome": "skipped"`, `"ladder_rung_reached": <current rung>`, `"fix_valid": null`, `"revealed": true` (not scored; the finding goes back to the queue for a later session, from the first rung). Call `next`.
- **`stop`:** every rung reached is already recorded; go to the end step.

## 6. Disclosure at session end

`session end` lists in `undisclosed_critical` every critical finding not yet worked through (skipped, stopped partway, or never reached). Disclose **each one**, area and file only, with this copy:

> A real critical issue remains in **<title>** (`<file>`). I'm not explaining it here so you can work it out yourself; run `/enguru:interview` again to continue.

Never add hints, lines, the consequence or the fix to the disclosure.
