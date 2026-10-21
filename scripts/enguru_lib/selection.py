"""Progressive question order: explain it -> what if -> spot the flaw, then real findings.

Pure functions over a concept's progress entry, its bank questions and the answer log;
progress.py decides what to serve with them.
"""

DIFFICULTIES = ["foundational", "intermediate", "advanced"]
# Quiz phases by question style. Real findings (guided-discovery ladders) come after phase 3.
PHASES = {"explain_it": 1, "what_if": 2, "spot_the_flaw": 3, "review_diff": 3}
QUIZ_PHASES = (1, 2, 3)
PASSED = ("correct", "partial")


def phase_of(style):
    return PHASES.get(style, 1)


def deepest_phase(questions):
    """The deepest phase a concept has questions for (asked or not), or None."""
    return max((phase_of(q.get("style")) for q in questions), default=None)


def _difficulty(q):
    d = q.get("difficulty")
    return DIFFICULTIES.index(d) if d in DIFFICULTIES else 1


def pick(concept_state, questions, only_phase=None):
    """(phase, question) for a concept's next question, or None.

    The next phase is the lowest one, above the deepest phase already passed (answered
    correct or partial), that still has an unasked question; a concept that has passed every
    phase but is not solid stays on its deepest phase. A phase with nothing unasked is
    skipped. Within the phase the easiest unasked question comes first (foundational ->
    intermediate -> advanced, then bank order). `only_phase` restricts the pick to one phase
    (a concept that failed this session gets no deeper question). None means nothing is left
    to ask in the concept's remaining phases."""
    if not questions:
        return None
    asked = set(concept_state.get("asked_question_ids", []))
    floor = min(int(concept_state.get("phase_passed") or 0) + 1, deepest_phase(questions))
    best = None
    for i, q in enumerate(questions):
        if q["id"] in asked:
            continue
        p = phase_of(q.get("style"))
        if (only_phase is not None and p != only_phase) or (only_phase is None and p < floor):
            continue
        key = (p, _difficulty(q), i)
        if best is None or key < best[0]:
            best = (key, p, q)
    return None if best is None else (best[1], best[2])


def last_quiz_answer(answers, cid, sid):
    """The last quiz answer on a concept in this session, or None."""
    for a in reversed(answers):
        if (a.get("session_id") == sid and a.get("kind") == "answer"
                and a.get("question_kind") == "quiz" and a.get("concept_id") == cid):
            return a
    return None
