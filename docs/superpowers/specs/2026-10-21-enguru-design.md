# Enguru: Design Spec

**Sub-project 1 of 2:** Comprehension interview and certificate
**Date:** 2026-10-21
**Status:** Awaiting review
**Plugin name:** `enguru`

---

## 1. Problem and intent

Experienced developers who use coding agents know what to review and what to correct. They spend a little time on exactly the right places and step in before things break. Junior developers who vibe-code usually have no senior to show them this, so they don't learn how to review. If this continues, many new developers may never build that skill.

`enguru` is a Claude Code plugin that does part of a senior's job. It interviews a developer about their own project or their own change. It checks that they understand the decisions made during vibe coding, the architecture, and the final outcome. When they get something wrong, it corrects them with context from their own code. It also tracks how their skills develop and summarizes them in a personal certificate.

**Primary user:** a junior developer working on their own, for self-improvement.

**What success looks like:** after using the plugin, the developer can explain the risky parts of their project, review their own changes the way a senior would, and catch real defects. Being able to pass the quiz isn't enough on its own.

## 2. Scope

### In scope (this spec)
- Project-mode interview: understanding of the whole project, organized into modules.
- Diff-mode interview: a single change set (branch, commit range, or uncommitted work), in quick or thorough form.
- Using transcripts, git history and code as evidence.
- Two interview flows: the **quiz flow** and the **guided-discovery flow** for real issues found in the code.
- A per-repo certificate with levels, evidence, strengths, gaps, next steps and trend.

### Out of scope
- **The token-efficiency review is sub-project 2,** with its own spec. This spec only makes sure the certificate format and the transcript adapter can be reused there (§13).
- The plugin never edits the developer's code.
- The certificate isn't a credential. There's no verification, signing, or protection against tampering.
- No certificates covering several repos, and no per-user store.
- No multiple-choice questions. Every answer is open-ended.
- No support for agents other than Claude Code.

## 3. Prior art and differentiation

| Existing tool | What it does | Gap we fill |
|---|---|---|
| DoI (`/doi`, `/paydebt`) | Multiple-choice quiz on a branch diff; skipped questions saved as "vibe debt" | Diff-only, multiple choice, no decision history, no review-skill training |
| cognitive-coverage | Teaching guide and quiz with a coverage dashboard | Not based on decision history; no correction loop |
| fable-method `merge-quiz` | Comprehension quiz before merging | Diff-only gate; no skill tracking |
| Token analyzers (ccwhy, burnie, token-efficiency-coach, etc.) | Token-waste analysis of transcripts | Relevant to sub-project 2 only |

**What sets enguru apart:**
1. It covers both whole-project and diff understanding, and uses **transcripts and git history to ask about decisions**.
2. It **trains review skill** (reviewing real diffs, spotting injected flaws, what-if reasoning), not just recall.
3. Open-ended answers, corrected using evidence from the developer's own code, and guided discovery of real issues.
4. A certificate with four skill levels that tracks progress over time.

**Taken from DoI:** diff mode, and the ability to skip a question now and revisit it later.

## 4. Commands

```
/interview                          Project mode: resume or start the next module
/interview --diff                   Diff mode: current branch vs. merge-base with the default branch
/interview --diff <base>..<head>    Diff mode on an explicit commit range
/interview --diff --uncommitted     Diff mode on uncommitted changes
          [--quick | --thorough]    Diff intensity (asked interactively if omitted)
/certificate                        Generate the certificate for this repo
```

During an interview the developer can type `hint`, `skip`, `stop`, or "I don't know."

**Default diff base:** the merge-base with the remote's default branch (`origin/HEAD`). If that can't be found, `main`, then `master`. If none of these exist, the developer is asked for a range.

## 5. Architecture

### 5.1 Components

| # | Component | Kind | Model | Responsibility |
|---|---|---|---|---|
| 1 | Digest script | Python (standard library only) | none | Locate sources; strip, redact and chunk transcripts; git summary; file map; diff hunks and blast radius; merge events; enforce the size cap |
| 2 | `decision-extractor` | Subagent | Haiku | Label transcript turns as request, choice or pushback |
| 3 | `project-analyst` | Subagent | Inherits the session's model | Build the topic map, questions, answer keys, flawed snippets, findings and discovery ladders |
| 4 | `finding-verifier` | Subagent | Inherits the session's model | Independently confirm or refute each finding |
| 5 | Interviewer | Skill (runs in the main session) | Session's model | Ask, grade, follow up, correct, record |
| 6 | Helper script | Python CLI | none | `next`, `record`, `status`, validation, staleness checks, lock file |
| 7 | Certificate step | Script for levels, model for narrative | Session's model | Calculate levels deterministically; write the narrative |

### 5.2 Data flow

```
/interview
  └─ digest prep (script) ─► chunks ─► decision-extractor (Haiku, per session, cached)
                                              │
     digest merge (script) ◄── labeled events ┘
        └─► digest.json
              └─► project-analyst ─► topic map, then questions per module (diff mode: all at once)
                     ├─► validate (script: schema, grounding, flaw check)
                     └─► findings ─► finding-verifier ─► confirmed / dropped / downgraded
                                          │
                                          ▼
                              question bank (banks/*.json)
                                          │
              interviewer skill ◄── next ─┤── record ─► progress.json
                                                          │
/certificate ─► level calculation (script) + narrative (model) ─► certificates/<date>.md
```

### 5.3 Plugin layout

Follows Claude Code plugin conventions. Exact manifest fields will be checked against current docs during planning.

```
enguru/
  .claude-plugin/plugin.json
  commands/interview.md
  commands/certificate.md
  skills/interviewer/SKILL.md
  skills/interviewer/references/grading.md
  skills/interviewer/references/discovery.md
  agents/decision-extractor.md      (model: haiku)
  agents/project-analyst.md
  agents/finding-verifier.md
  scripts/enguru.py                     (CLI entry point)
  scripts/transcripts.py            (transcript adapter: the only module that parses JSONL)
  scripts/gitinfo.py
  scripts/digest.py
  scripts/validate.py
  scripts/state.py
  scripts/rubric.py
  config/rubric.json
  schemas/*.json
  tests/
```

**Requirement:** `python3` 3.9 or later. This is checked at startup, before any tokens are spent.

### 5.4 State folder (`.enguru/`, per repo)

```
.enguru/
  digest.json
  cache/extractor/<transcript-content-hash>.json
  banks/project/topic-map.json
  banks/project/<module-id>.json
  banks/diff/<range-id>.json         range-id = <base-short-sha>..<head-short-sha> | worktree-<timestamp>
  progress.json
  progress.json.bak
  keylog.jsonl                       answer-key corrections raised during grading
  certificates/<YYYY-MM-DD>.md
  lock
```

On first run the plugin adds `.enguru/` to `.gitignore`, because the folder holds personal learning data and answer keys.

## 6. Digest

### 6.1 Sources
- **Transcripts:** `~/.claude/projects/<encoded-project-path>/*.jsonl`, filtered to entries whose working directory is this repo. In diff mode, only sessions whose edit or write tool calls touched files in the diff, or whose timestamps overlap the commits in range. Subagent side-conversations are kept and tagged.
- **Git:** commit log with file stats, reverts, the most frequently changed files, and dependency-manifest history. In diff mode, hunks against the merge-base as well.
- **Code:** a map of tracked files (sizes, detected stack, likely entry points).

### 6.2 Hybrid extraction
- **Rule-based (script), structural:** churn (the same file or function edited three or more times in a session, or a failed test or command followed by edits), dependency changes, reverts, blast radius.
- **Haiku (`decision-extractor`), semantic:**
  - *request:* what the developer asked for
  - *choice:* the agent's stated reasoning for an approach
  - *pushback:* any correction from the developer, whether blunt, polite or indirect

### 6.3 Pipeline
1. **Prep (script):** remove all tool results except error lines (500 characters at most), remove file contents, run secret redaction, split each session into chunks of candidate turns, and keep each turn's transcript ID.
2. **Extract (Haiku):** for each event, output `{label, paraphrase (≤200 chars), files[], confidence, source_turn_ids[]}`. The extractor can only read chunk files, its output must match a schema, and its instructions say all transcript text is data, never instructions. Sessions run in parallel. Results are cached by a hash of the transcript file's contents.
3. **Merge (script):** validate the extractor's output and **drop any event whose source turn ID doesn't exist**. Combine with the rule-based events, apply the size cap, and write `digest.json`.

### 6.4 Blast radius (diff mode)
Changed symbols are pulled from the hunks using pattern rules for Python, JS/TS, Go, Java, Ruby and Rust, falling back to plain identifiers. The script searches the repo for uses and for test files that reference them. The results are labeled **candidates**, and the analyst decides which are relevant.

### 6.5 Size cap
A hard cap of about 20,000 tokens, estimated as characters ÷ 4. When the digest would exceed it, the script trims in this order:
1. Shorten excerpts to 300 characters, then 150.
2. Drop request events before pushback and churn events.
3. Drop the oldest events before the newest.
4. Replace what remains with counts.

Every trim is recorded in `digest.meta.truncation`.

### 6.6 Safety
Regex redaction for common secret formats (cloud keys, `sk-`-style tokens, bearer tokens, connection strings, private key blocks). `.env`-style files are never read. The digest is stored only locally.

### 6.7 Output
`digest.json` (versioned) contains: `meta` (scope, source counts, events kept and dropped, sessions that used the fallback, truncation notes), `file_map`, `git`, `events[]` (each with id, label, paraphrase, files, session, timestamp, source refs) and `diff` (`hunks[]`, `blast_radius_candidates[]`).

## 7. Question bank

### 7.1 When it's built
- **Project mode:** first the topic map (4–8 modules suited to the project, each with 3–6 concepts), then questions **for each module when that module starts**.
- **Diff mode:** risk-ranked concepts covering the change and its blast radius, with their questions, built in one pass. Risk order: auth, data writes, money and external calls first; then changed contracts; then untested paths.
- **Revisits:** the analyst writes a **new question** on the same concept. An identical question is never repeated.

### 7.2 Concept record
`{id, module, title, why_it_matters, risk_tier, kind: quiz|finding, evidence_refs[], evidence_hashes{file: sha}}`

### 7.3 Question record
`{id, concept_id, style, skill, difficulty: foundational|intermediate|advanced, prompt, context, answer_key: {must_have[], nice_to_have[], misconceptions[]}}`
- Each `must_have` and `nice_to_have` item has `{point, explanation, evidence_ref}`.
- Each `misconception` has `{wrong_belief, correction, evidence_ref}`.

### 7.4 How each style is grounded
| Style | Skill | Source |
|---|---|---|
| Explain-it | Comprehension | Decision events and code structure |
| Review real diff | Review judgment | Project mode: commits that later needed a fix (the fix is the key). Diff mode: the developer's own hunks plus blast-radius files |
| Spot-the-flaw | Defect detection | A real snippet of 40 lines or fewer with one injected flaw from: missing await, removed auth check, unhandled null, off-by-one, swallowed exception, injection risk, race condition. Stores the original, the flawed version, the location and the category |
| What-if | Systems thinking | Components that actually exist in the project (external APIs, data stores, queues) |

### 7.5 Validation (script, after each analyst run)
- JSON schema check.
- **Grounding:** referenced files and line ranges exist, commits exist, and decision event IDs are in the digest.
- **Flaw check:** the flawed snippet differs from the original in exactly one region of 5 changed lines or fewer.
- A question that fails is regenerated once, and dropped if it fails again. A question that hasn't passed validation is never shown.

### 7.6 Staleness
At the start of each session the script compares evidence hashes with the current files. Concepts whose evidence has changed are marked stale and rebuilt when next used. Progress is tracked by concept ID and carries over. If a concept's code has been deleted, its history stays and it leaves the revisit queue.

### 7.7 Model choice
The analyst inherits the session's model and can be configured. It isn't Haiku, because answer keys, flaws and findings need the strongest reasoning in the plugin.

## 8. Findings and verification

### 8.1 What qualifies as a finding
A real issue in the code that falls into one of these: security exposure, data loss or corruption, money or billing errors, a crash on a main path, or a core feature that silently fails. Style and minor inefficiencies remain quiz material.

### 8.2 Finding record
`{concept_id, severity: critical|major, lines, consequence, fix, ladder: [{question, expected_realization}] (3–6 rungs, broad to narrow)}`

### 8.3 Verification
`finding-verifier` runs with a fresh context and receives only the finding and the code it cites.
- **Confirmed:** used in the guided-discovery flow.
- **Refuted:** dropped.
- **Uncertain, or the verifier failed:** becomes an ordinary quiz concept.

### 8.4 Resolution
The plugin doesn't fix anything. On a later run, if a finding's evidence hashes have changed, the verifier checks again. If the issue is resolved, the finding is marked `fixed` and shown on the certificate.

## 9. Interview

### 9.1 Session start
1. Startup checks (python version, lock file, staleness).
2. Order of work:
   - confirmed findings, critical first;
   - then up to 2 items from the revisit queue;
   - then the next unfinished concepts.
3. A short introduction: what's covered, the available commands, and that "I don't know" is better than guessing.
4. **Diff mode without `--quick` or `--thorough`:** the plugin asks once, and recommends **thorough** if the diff touches 10 or more files or any risk-tier-1 area.

### 9.2 Diff intensity
| Mode | Questions | Skips |
|---|---|---|
| Quick | 5 concepts by risk; findings in the diff are always included in addition | Allowed; the concept goes to the revisit queue |
| Thorough | Up to 2 questions per concept, at most 12 concepts. Concepts that don't fit are listed at the end | Allowed |

### 9.3 Context hygiene
The interviewer only gets questions through `enguru.py next` and saves results through `enguru.py record`. It never loads the whole bank.

### 9.4 Quiz flow
- **Starting difficulty:** intermediate, or foundational if this concept failed before.
- **Grading:** each must-have point is marked hit, partial or missed. A misconception the developer states outright counts against the answer more than an omission.
  - *Outcomes:* `correct | partial | incorrect | dont_know | skipped`
- **Grading rules:**
  1. Meaning over wording. Length and keywords earn nothing.
  2. A valid point the key doesn't include earns credit once the interviewer confirms it in the code, and the key is logged as wrong in `keylog.jsonl`.
  3. When the developer pushes back, the interviewer checks the evidence again. If the developer is right, it accepts and logs the key as wrong. If not, it holds the grade and cites file and line. Insisting never changes a grade.
- **After each outcome:**
  - *Correct:* confirm, plus one deeper point.
  - *Partial:* credit what was right, then a targeted follow-up that doesn't reveal the answer.
  - *Incorrect:* an explicit correction with the evidence shown, using the pre-written misconception correction where one applies, then a check question in a slightly different situation.
  - *Don't know:* the full explanation, then a check question.
- **Limits:**
  - Follow-ups: 3 per concept. After that, the full explanation is given and the concept is marked `needs_revisit`.
  - Hints: two steps (first the file, then the region).
- **Spot-the-flaw:** full credit requires the location, the reason and a fix. Finding the right area without the reason is partial. The reveal shows the flawed and original versions side by side, labeled: *"This flaw was introduced for the exercise; your code doesn't contain it."*
- **Review diff:** graded on flagging the issue that later needed fixing. Other valid concerns add credit.
- **What-if:** graded on reasoning.

### 9.5 Guided-discovery flow (findings)
- **Announced as real:** *"This next one is a real issue in your code, not an exercise."*
- **Moving down the ladder:** a wrong answer or "I don't know" moves to the next, narrower question. The answer is never revealed while steps remain. The three-follow-up limit doesn't apply.
- **Finishing has two parts:**
  1. *Name it:* the problem and its consequence, in the developer's own words.
  2. *Fix it:* the developer proposes a fix, which is checked against the evidence, including for new problems it might introduce.
- **Last resort:** if the developer is still stuck after the final step, the explanation is given, framed around the steps they worked through, and the finding goes back into the revisit queue.
- **Mandatory disclosure:** if a **critical** finding is skipped or stopped partway, the session summary still states that a real critical issue exists in a named area and file, without explaining it.

### 9.6 Tangents
If the developer asks a question mid-interview, the interviewer answers briefly and then returns to the current question.

### 9.7 Tone
Direct, specific, warm. No filler praise. A wrong answer is never softened into "partly right."

### 9.8 Recording
Each graded answer is saved immediately to `progress.json` as `{concept_id, question_id, style, skill, difficulty, outcome, must_haves_hit, misconception_stated, hints_used, followups_used, ladder_rung_reached, ladder_length, fix_valid, scope, range_id, intensity, timestamp}`. An unfinished ladder records its current step so the next session can resume there.

### 9.9 Session end
A summary: solid concepts, concepts to revisit, the single most important takeaway, any undisclosed critical findings (per §9.5), and a suggestion to run `/certificate` when enough new evidence has accumulated.

## 10. Certificate

### 10.1 Principle
**Levels are calculated by `rubric.py` using fixed rules.** The model only writes the narrative, based on the same evidence. The certificate covers **this repo only**.

### 10.2 Skills
| Skill | Fed by |
|---|---|
| Comprehension | Explain-it |
| Review judgment | Review real diff, findings |
| Defect detection | Spot-the-flaw, findings |
| Systems thinking | What-if |

### 10.3 Answer score
- **Base score by outcome:**
  - correct 1.0
  - partial 0.5
  - partial with a stated misconception 0.25
  - incorrect 0
  - don't know 0
  - skipped: not scored
- **Hint multiplier:** 1.0 (no hints), 0.8 (one), 0.6 (two).
- **Follow-up multiplier:** 1.0, 0.85, 0.7 or 0.55, for 0, 1, 2 or 3 follow-ups.
- **Findings:** score = 1 − (rung − 1) / ladder_length. Multiplied by 0.75 if the proposed fix was invalid. 0 if the last-resort explanation was needed.
- **Difficulty weight:** foundational ×1, intermediate ×1.5, advanced ×2.

**Weighted score for a skill** = Σ(score × weight) / Σ(weight), calculated over that skill's **most recent 20 scored answers**.

### 10.4 Rubric (`config/rubric.json`, initial values)
| Level | Minimum evidence | Performance |
|---|---|---|
| Not yet rated | fewer than 6 scored answers | none |
| Novice | 6+ | below 50% |
| Developing | 6+ | 50% or higher |
| Proficient | 10+, at least 3 intermediate or above, at least 1 advanced attempted | 70% or higher, and no misconception stated in 2 or more answers without a later correct answer on the same concept |
| Senior-ready | 15+, at least 3 advanced, project-mode evidence **plus 2 or more distinct diff `range_id`s** | 80% or higher on advanced; average finding rung ≤ ladder_length / 2 (if findings exist) |

Levels can go down as well as up, under the same rules. A drop is stated plainly.

### 10.5 `certificates/<YYYY-MM-DD>.md`
1. **Header:** developer name (`git config user.name`), repo, date, scopes covered, and a one-line note: AI-produced self-assessment for personal growth, not a credential.
2. **Each skill:** level, evidence summary (count, difficulty mix, scopes), one strength, one gap (each citing concepts or sessions), and one concrete next step.
3. **Findings:** discovered (at which step), and fixed or open.
4. **Open items:** size of the revisit queue, and critical findings not yet worked through.
5. **Trend:** each skill's level compared with the previous certificate.

Sections are self-contained so sub-project 2 can add a token-efficiency section (§13).

## 11. Error handling

| Situation | Behavior |
|---|---|
| Not a git repo | Project mode uses the file map and transcripts. Diff mode explains it needs git |
| No transcripts | Continue without them. Tell the developer that decision questions will be limited |
| Transcript format changed | The adapter skips unreadable lines and reports a count. If nothing can be read, treated as no transcripts |
| `python3` missing or below 3.9 | Fail at startup with an install instruction |
| Extractor fails for a session | That session uses keyword rules. Noted in `digest.meta` |
| Analyst fails or keeps producing invalid output | Retry once, then stop that module with a message |
| Verifier fails | The finding becomes a quiz concept |
| Empty diff | Say there's nothing to quiz on and suggest project mode |
| Diff larger than the thorough limit | List the concepts that weren't covered |
| Progress file corrupted | Writes go to a temporary file and then replace the original. Restore from `.bak` |
| A second session running at the same time | The lock file blocks it, with an explanation |
| Developer stops mid-ladder | The current step is saved, and the next session resumes there |
| Answer key found wrong | Logged to `keylog.jsonl` and corrected the next time that module is rebuilt |

## 12. Testing

### 12.1 Unit tests (deterministic)
- **Digest:** fixture repos built in tests with real git commands, plus fixture JSONL transcripts. Covers session matching, redaction, churn rules, blast radius, and the trimming order.
- **Validation:** schema, grounding and flaw checks, each with inputs that should pass and inputs that should fail.
- **`enguru.py`:** `next`, `record`, `status`, crash-safe writes, restoring from backup, and the lock file.
- **`rubric.py`:** table-driven tests at every boundary: 5 vs. 6 answers, 69.9% vs. 70%, the misconception rule, distinct `range_id`s, the 20-answer window, and level drops.

### 12.2 Evaluation sets (model-driven components)
| Component | Set | Target |
|---|---|---|
| decision-extractor | Hand-labeled transcript excerpts, including polite, sarcastic and indirect pushback | Pushback recall ≥ 0.85 and precision ≥ 0.85; recall at least 0.20 higher than the keyword rules on the same set |
| project-analyst | Seeded repo with known real bugs planted | ≥ 80% of planted bugs reported as findings |
| finding-verifier | The same repo with deliberately false findings added | **0 false findings reach guided discovery** |
| Grader | Question and answer pairs with expected grades: keyword-stuffed wrong answers, long wrong answers, terse right answers, valid answers missing from the key | ≥ 90% agreement |
| Holding firm | Scripted pushback against a correct correction, and against a wrong key | Holds 100% / accepts 100% |

### 12.3 End to end
A small sample vibe-coded app with real transcripts, run through project mode, quick and thorough diff modes, and `/certificate`, with a scripted "student" whose knowledge has defined gaps. The checks:
- follow-up and hint limits are enforced;
- the ladder never reveals the answer early;
- a critical finding is disclosed when the student stops partway;
- a resumed session continues at the right step;
- certificate levels match a hand-calculated result.

## 13. Hooks for sub-project 2 (token-efficiency review)
- `scripts/transcripts.py` is the only module that parses transcript JSONL, and it will be reused.
- The certificate format is a set of self-contained sections, so sub-project 2 can add a "Token efficiency" section without changing existing ones.
- Plan for sub-project 2: evaluate wrapping or recommending an existing token analyzer for the numbers, and focus enguru's own work on **behavior-level coaching** (prompt clarity, repeated context, CLAUDE.md usage).

## 14. Decision log
| Decision | Reason |
|---|---|
| Two sub-projects; interview first | The interview is central to the stated goal; the certificate depends on scoring |
| Certificate for self-improvement | Removes the need for verification or tamper-proofing; honest explanations matter more |
| Code + git + transcripts as sources | Decisions often aren't visible in code alone |
| Modular project mode with capped adaptivity | Covers real project sizes without fatigue or unbounded cost |
| All four question styles | Each maps to a distinct skill on the certificate |
| Levels plus written feedback | A bare level doesn't tell the developer what to do next |
| Approach B (prep subagent + main-session interviewer) | Keeps the interview's context small; the prep cost is paid once |
| Diff mode with quick/thorough chosen each time | Most real work is on existing codebases; fits both pre-merge checks and study sessions |
| Haiku for semantic event extraction | Keyword rules miss polite or indirect pushback |
| Per-module question generation | Unvisited modules cost nothing; answer keys match current code |
| Guided discovery for real findings | Working out a real mistake yourself is remembered better than being told |
| Verifier for findings | A false finding taught through guided discovery would be a lasting false lesson |
| Mandatory disclosure of critical findings | The teaching method must never hide a real critical issue |
| Levels calculated by a script | Consistent levels; meaningful trend |
| Per-repo certificate | Simplicity; anti-memorization handled by requiring 2+ distinct diff ranges |
