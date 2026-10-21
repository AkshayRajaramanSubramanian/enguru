# Grading quiz answers

Used for every `ask` action. The question's `answer_key` has `must_have[]`, `nice_to_have[]` and `misconceptions[]`, each item citing an `evidence_ref` (file and lines, commit, or digest event). Keep the key to yourself; it is your grading guide, not a script to read out.

## 1. Mark the must-haves

For each `must_have` point (by index, starting at 0), mark it **hit**, **partial** or **missed** against the developer's answer.

- **Meaning over wording.** Grade what the developer means. Length and keywords earn nothing: an answer that recites the right terms without the idea misses the point; a plain-words answer with the idea hits it.
- A **misconception stated outright** (the developer asserts a `wrong_belief`, or another belief the code contradicts) counts against the answer more than an omission. Set `misconception_stated: true`.
- `nice_to_have` points never decide the outcome; mention them in feedback when the developer gets them.

## 2. Outcome

One of `correct | partial | incorrect | dont_know | skipped`:

| Outcome | When |
|---|---|
| `correct` | Every must-have hit, no misconception stated. |
| `partial` | At least one must-have hit or partial, but not all hit; or all hit alongside a stated misconception. |
| `incorrect` | No must-have hit or partial, or the answer is mainly a misconception. |
| `dont_know` | The developer says "I don't know" (or equivalent) instead of answering. |
| `skipped` | The developer types `skip`. |

## 3. Points missing from the key

If the developer makes a valid point the key does not include, check it in the code (read the cited file and lines). If the code confirms it, give credit as if it were a must-have, and log the key as wrong:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/enguru.py" keylog add <<'JSON'
{"question_id": "<question.id>", "issue": "missing point: <the point>", "evidence": "<file>:<lines> <what the code shows>"}
JSON
```

## 4. Holding firm

When the developer disputes a grade, re-check the evidence: open the cited file and lines again.
- **The developer is right:** say so plainly, regrade, and run `keylog add` with the wrong key point as `issue` and the code that proves it as `evidence`.
- **The developer is wrong:** keep the grade and cite the file and line that settle it, briefly and without heat.

Insistence never changes a grade. Repeating, rephrasing or pressure is not evidence; only the code is.

## 5. Feedback for each outcome

- `correct`: confirm in one sentence, then add one deeper point (a `nice_to_have`, an edge case, or why it matters) and move on.
- `partial`: name exactly what was right, then ask a targeted follow-up that points at the missing piece without revealing it. Regrade after each follow-up; the outcome can rise.
- `incorrect`: give an explicit correction with the evidence shown (file and lines, quoted briefly). Use the matching `misconceptions[].correction` when one applies. Then ask a **check question**: the same idea in a slightly different situation. The outcome stays `incorrect`; the check question is for learning.
- `dont_know`: give the full explanation, grounded in the cited code, then a **check question**. The outcome stays `dont_know`.

## 6. Limits

- **Follow-ups:** at most 3 per question, counting partial follow-ups and check questions. After the third, give the full explanation and record with `followups_used: 3` (the CLI queues the concept for revisit).
- **Hints:** two steps, on request (`hint`). Step 1 names the file; step 2 names the region (function or line range), both taken from the must-have `evidence_ref`s. Never a third hint; offer "I don't know" instead.

## 7. Each style

- **`explain_it`** (comprehension): the must-haves as above.
- **`review_diff`** (review judgment): graded on flagging the issue that later needed fixing (project mode) or the key's must-haves (diff mode). Other valid concerns the developer raises add credit, as nice-to-haves or via section 3 if they are real. Never show the fix commit.
- **`spot_the_flaw`** (defect detection): full credit needs the **location**, the **reason** it breaks, and a **fix**. The right area without the reason is `partial`. After grading, reveal the flawed and original versions side by side (two labeled fenced blocks, "Flawed (exercise)" and "Your code") with this line, verbatim:

  > This flaw was introduced for the exercise; your code doesn't contain it.

- **`what_if`** (systems thinking): graded on the reasoning: does the developer trace what the real components do under that failure, matching the key? A different but sound chain of reasoning that the code supports earns credit (section 3).

## 8. Tone

Direct, specific, warm. No filler praise ("Great question!", "Awesome!"). A wrong answer is never called "partly right" or softened into one; say it is not right and show why. Credit only what was actually right.

## 9. Record

Record as soon as the question's outcome is final (after any follow-ups), before the next `next`:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/enguru.py" record <<'JSON'
{"kind": "answer", "session_id": "<session_id>", "concept_id": "<question.concept_id>",
 "question_id": "<question.id>", "question_kind": "quiz",
 "style": "<question.style>", "skill": "<question.skill>", "difficulty": "<question.difficulty>",
 "outcome": "correct|partial|incorrect|dont_know|skipped",
 "must_haves_hit": [<indices of must_have points marked hit>],
 "misconception_stated": <true|false>, "hints_used": <0-2>, "followups_used": <0-3>,
 "scope": "<scope>", "range_id": <"range" or null>, "intensity": <"quick"|"thorough"|null>,
 "timestamp": "<YYYY-MM-DDTHH:MM:SSZ>"}
JSON
```

For `skip`: record at once with `"outcome": "skipped"`, `"must_haves_hit": []`, and the hints and follow-ups used so far; say it will come back later, and call `next`.
