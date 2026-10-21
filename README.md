# enguru

A Claude Code plugin that interviews you about your own vibe-coded project, so you
understand the code your agent wrote and can review the next change it makes.

Enguru reads three things: your code, your git history, and your past Claude Code session
transcripts for the repo. From them it builds questions grounded in your real code and real
decisions, then asks them one at a time:

- **Explain it**: why the code is the way it is (comprehension).
- **Review a real diff**: a change of yours that later needed a fix (review judgment).
- **Spot the flaw**: a real snippet with one injected bug (defect detection).
- **What if**: what your components do when something fails (systems thinking).

It grades answers on meaning, not keywords, and holds its grade unless the code proves you
right. If it finds a **real issue** in your code (a security hole, data loss, money errors,
a crash on a main path), an independent verifier checks it first. Then, instead of telling
you, it walks you down a ladder of narrower questions until you find the issue yourself and
propose a fix. Enguru never edits your code.

A certificate summarizes your level in the four skills, from fixed rules over the recorded
answers, with written strengths, gaps and next steps.

This is **sub-project 1 of 2**. Sub-project 2 will add a token-efficiency review of how you
work with the agent, as a new section of the same certificate.

## Requirements

- Claude Code with plugin support.
- Python **3.9 or newer** as `python3` (standard library only). Enguru checks this on start
  and tells you where to get it if it is missing.
- git, for diff mode and history-based questions (project mode works without it, with fewer
  question types).

## Install

From the marketplace in this repository:

```
/plugin marketplace add AkshayRajaramanSubramanian/enguru
/plugin install enguru@enguru
```

Or load a local clone for one session:

```
claude --plugin-dir /path/to/enguru
```

## Commands

Run these from inside the repository you want to be interviewed about.

```
/enguru:interview                          Project mode: resume or start the next module
/enguru:interview --diff                   Diff mode: current branch vs. merge-base with the default branch
/enguru:interview --diff <base>..<head>    Diff mode on an explicit commit range
/enguru:interview --diff --uncommitted     Diff mode on uncommitted changes
                  [--quick | --thorough]   Diff intensity (asked if omitted)
/enguru:certificate                        Write the certificate for this repo
```

- **Project mode** splits the project into 4 to 8 modules and covers one module per session.
  Questions for a module are built when that module starts.
- **Diff mode** covers a change and the code it affects, riskiest first. **Quick** asks about
  5 concepts plus any real issues; **thorough** asks up to 2 questions on each of up to 12
  concepts. The default base is the merge-base with `origin/HEAD`, then `main`, then `master`.
- During an interview, type `hint` (first the file, then the region), `skip` (it comes back
  later), `stop` (end with a summary), or "I don't know", which beats guessing: you get the
  full explanation at no extra cost.

The first session in a repo takes a few minutes while enguru reads your transcripts and code.

## What is stored in `.enguru/`

Everything lives in a `.enguru/` folder at the repository root, which enguru adds to
`.gitignore` on first run. Enguru writes nothing else in your repository.

| Path | What it holds |
|---|---|
| `digest.json` | Decisions, requests and pushback extracted from your transcripts, plus git facts |
| `cache/extractor/` | Extractor results, keyed by a hash of each transcript |
| `banks/project/`, `banks/diff/` | Topic map, questions with answer keys, and verified findings |
| `progress.json` (+ `.bak`) | Every graded answer, the revisit queue, and where each real issue's ladder stands |
| `keylog.jsonl` | Answer-key corrections made during grading |
| `certificates/<date>.md`, `.json` | Certificates and the levels behind them (for the trend) |
| `work/`, `lock` | Scratch files for the current session and its lock |

## Privacy

- Enguru has no server and sends nothing anywhere itself. Its state stays in `.enguru/` in
  your repository, and transcripts are read in place from `~/.claude/projects/`.
- The text it works with is processed by the same Claude model your Claude Code session
  already uses, as with any other Claude Code work.
- Before transcript text goes into the digest, common secret formats are redacted (cloud
  keys, `sk-` style tokens, GitHub and Slack tokens, bearer tokens, credentials in URLs,
  private key blocks, `password=`/`token=` assignments). `.env`-style files are never read.
- The certificate is an AI-produced self-assessment for personal growth, not a credential.

## Development

```
python3 -m pytest -q                 # unit tests (deterministic, no model calls)
claude plugin validate .             # manifest checks
python3 evals/extractor/run.py       # model evals: extractor, analyst, verifier,
python3 evals/run_analyst.py         #   grader and hold-firm (each calls `claude -p`;
python3 evals/run_verifier.py        #   results go to evals/results/)
python3 evals/run_grader.py
python3 evals/run_holdfirm.py
python3 e2e/run_e2e.py               # end-to-end run with a scripted student (15 to 30 minutes)
```

`e2e/run_e2e.py` builds the sample app in `e2e/sample-app/` as a git repo, installs
`e2e/sample-transcripts/` as its session history (the CLI reads them through
`ENGURU_TRANSCRIPTS_ROOT`, which overrides `~/.claude/projects`), and runs project mode, quick
and thorough diff mode and the certificate through `claude -p`, with a second `claude -p`
playing the student in `e2e/student_persona.md`. It checks the follow-up limit, that a ladder
never reveals early, that stopping mid-ladder discloses the critical issue's file, that the
next session resumes at the saved step, that certificate levels match a recomputation, and
that nothing outside `.enguru/` and `.gitignore` changed. Results go to
`e2e/results/last_run.json`.

## License

MIT
