# Task 11 controller rulings (binding; resolve gaps in the brief)

## Files
- Banks: `.enguru/banks/project/topic-map.json`, `.enguru/banks/project/<module_id>.json`, `.enguru/banks/diff/<range_id>.json`.
- progress: `.enguru/progress.json` (atomic_write_json / read_json from paths). Missing → initialize the documented empty structure.

## Session
- `session start` takes the same scope flags as `digest prep` (`--project | --diff [--range A..B | --uncommitted]`) plus `[--quick|--thorough]`. Reuse digest's scope-resolution code (refactor a shared helper if needed; keep Task 7 tests green). Errors: not_git / no_base / empty_diff as in digest prep; lock_held → exit 2 `lock_held`.
- For `--uncommitted`, range_id is a worktree id computed once at session start and stored in the session; `next` uses the session's stored range_id. (Digest prep may compute its own; the skill runs `session start` first and passes nothing else — so ALSO make `digest prep` reuse the current session's range_id for --uncommitted if a current session exists with scope diff and a worktree range id.)
- `start_session` returns `{session_id, scope, range_id, intensity, recommend_intensity?, open_findings, revisit_count, module_id}`. `recommend_intensity` only present in diff mode when no intensity flag was given; session intensity then stays null until the skill calls `session set-intensity quick|thorough` (add this small subcommand). In project mode intensity is null.
- module_id at start (project mode): first module in topic-map order with any concept not solid and not retired; null if no topic map yet. If the topic map is built during the session, `next` sets the session's module_id lazily the same way.
- An existing unended session is simply superseded (its `ended` stays null); lock stale-reclaim already handled by acquire_lock.
- `end_session` releases the lock, sets `ended`, clears `current`.

## next_item details
- Every build action carries paths the skill passes to the analyst: `{"action":"build_topic_map","out_path":...,"digest_path":".enguru/digest.json","keylog_path":".enguru/keylog.jsonl"}`, `build_module` adds `module_id`, `build_diff_bank` adds `range_id`. `rebuild_concept` adds `concept_id, reason, bank_path, out_path` (`out_path` = `.enguru/work/revisit-<concept_id>.json`) and `asked_question_ids` (list of prompts' ids already asked, so the analyst avoids repeats). Paths are repo-relative strings.
- Add CLI `bank merge <bank_path> <partial_path>` (in bank.py `merge_partial(bank_path, partial_path)`): partial is a bank-shaped file; replace concepts by id, add/replace questions by id, replace findings by concept_id; atomic write. The skill runs validate on the partial → drop failures → merge → seal.
- Staleness in `next`: before serving a concept, check it live with `bank.stale_concepts(repo,[concept])`; stale → `rebuild_concept` reason "stale". (After merge+seal it is fresh, so next call proceeds.) Session-start marking still happens as the brief says.
- Exhausted concept (every question asked) → `rebuild_concept` reason "revisit" (for revisit items AND any re-served concept).
- verify_fix: `{"action":"verify_fix","concept_id","finding":<finding obj>,"out_path":".enguru/work/verdicts/fixcheck-<cid>.json"}`. Result recorded with a new record kind `fix_check`: extend record schema `kind` enum with `fix_check` and add optional `fix_verdict ∈ fixed|still_present|uncertain`. `fixed` → finding status `fixed`; otherwise restore the status it had before `recheck` (store `prev_status` when marking recheck). The skill then runs `bank seal` so the finding is no longer stale. A fix_check record is not scored (Task 12 must ignore it — it only scores kind answer).
- Ladder: only findings with verdict "confirmed" whose finding status is open|in_progress. Priority: in_progress before open, then critical before major, then bank order. `rung` = saved rung (default 1); `step` = ladder[rung-1]; `announce` = rung == 1 and status open. Findings are drawn from all built banks in scope (project: all module banks; diff: the range bank).
- Serving/once-per-session: a concept/question is "served" when `next` returns it; record it in `current.served_concepts` / `current.served_questions[cid]` and the question id in `concepts[cid].asked_question_ids`. If `next` is called while a served ask has no answer record yet in this session, return that same ask again (resume-safe). Same for a ladder (it naturally repeats until ladder_progress/answer arrives).
- Revisit: up to 2 revisit concepts served per session (count served ones from `revisit`), taken in queue order, skipping retired.
- Project mode: serve each not-solid, not-retired concept of the session's module at most once per session, in topic-map concept order. When all are served → done (one module per session). 
- Quick: up to 5 non-finding concepts (risk_tier asc, then bank order), 1 question each. Thorough: up to 12 non-finding concepts, up to 2 questions each (the 2nd question for a concept is served right after the 1st is answered, only if the 1st outcome was not `correct`... NO — simpler: serve 2nd question only if the concept is not solid after the first answer). Findings are always in addition. Revisit items count toward neither limit.
- done: `{"action":"done","uncovered":[cid...]}` — diff mode (either intensity): non-finding concepts beyond the limit, in order; project mode: [].
- A "non-finding concept" = concept kind quiz. Concepts with kind finding are served only via their ladder (their fallback quiz questions are used only if the verifier downgraded them, which turns kind into quiz).

## record
- Validate against record schema → exit 2 `state_corrupt` with the error list on invalid input.
- `answer` for question_kind quiz: append; `correct` → solid (and remove from revisit); `skipped`, or `followups_used==3` and outcome≠correct → needs_revisit + append to revisit (no duplicates); otherwise status stays `open`.
- `answer` for question_kind finding: append; if `revealed` → finding status `open`, rung reset to 1, concept into revisit; else → status `completed`. 
- `ladder_progress`: append (unscored), finding status `in_progress`, `rung = ladder_rung_reached`.
- Returns `{"ok":true,"concept_status": <concept status or finding status for finding records>}`.

## end_session
- `scored_since_last_certificate`: count answer records (outcome ≠ skipped) with timestamp after the latest `.enguru/certificates/*.json` file's date (filename `YYYY-MM-DD.json`; compare record date > that date; none → all).
- `undisclosed_critical` items: `{concept_id, title, file}` where file = finding.lines.file.

## status
- `{sessions:n, answers:n, concepts:{status:count}, revisit:[...], findings:{cid:{status,rung}}, current_session: id|null}`.
