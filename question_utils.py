"""Pure, stdlib-only helpers for question data.

Kept free of any Streamlit import so both the app and the one-time migration
script can use it. The key function, ``normalize_question``, removes
correct-answer position bias by shuffling option order and remapping the
answer indices AND any option-letter references in the explanation.
"""
import random
import re

# Match an option/choice/answer keyword followed by a run of A-E letters joined
# by connectors ("A", "A and B", "A, B, and C"). Only uppercase A-E qualify, so
# lowercase connective words ("and", "or") never get mistaken for a letter.
_LETTER_RUN = re.compile(
    r"\b([Oo]ptions?|[Cc]hoices?|[Aa]nswers?)(\s+)"
    r"([A-E](?:(?:\s*(?:,|and|or|&)\s*)+[A-E])*)"
)


def _remap_explanation_letters(text: str, old_to_new: dict) -> str:
    """Rewrite 'Option A'-style letter references through the permutation.

    Letters map to ORIGINAL option indices (A=0, B=1, ...). Each letter is
    replaced with the letter for its NEW position, so a content-anchored
    sentence like 'Option A describes X' stays correct after X is moved.
    """
    if not text:
        return text

    def _sub_letter(m):
        idx = ord(m.group(0)) - 65  # 'A' -> 0
        if idx in old_to_new:
            return chr(65 + old_to_new[idx])
        return m.group(0)  # letter beyond option count — leave untouched

    def _sub_run(m):
        keyword, gap, run = m.group(1), m.group(2), m.group(3)
        return keyword + gap + re.sub(r"[A-E]", _sub_letter, run)

    return _LETTER_RUN.sub(_sub_run, text)


def normalize_question(q, rng=random):
    """Shuffle a question's options in place, remapping answer + explanation.

    Idempotent: a question already marked ``_shuffled`` is returned unchanged.
    Malformed questions (missing/short options or answer) are returned as-is.
    """
    if not isinstance(q, dict) or q.get("_shuffled"):
        return q

    options = q.get("options")
    answer = q.get("answer")
    if not isinstance(options, list) or len(options) < 2:
        return q
    if not isinstance(answer, list) or not answer:
        return q

    n = len(options)
    perm = list(range(n))
    rng.shuffle(perm)  # perm[new_pos] = old_pos
    old_to_new = {old: new for new, old in enumerate(perm)}

    q["options"] = [options[perm[new]] for new in range(n)]
    q["answer"] = sorted(old_to_new[a] for a in answer if a in old_to_new)

    if isinstance(q.get("_original_answer"), list):
        q["_original_answer"] = sorted(
            old_to_new[a] for a in q["_original_answer"] if a in old_to_new
        )

    if isinstance(q.get("explanation"), str):
        q["explanation"] = _remap_explanation_letters(q["explanation"], old_to_new)

    q["_shuffled"] = True
    return q
