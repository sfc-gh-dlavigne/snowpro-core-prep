"""One-time migration: shuffle option order in the existing question cache to
remove correct-answer position bias. Idempotent (skips already-shuffled
questions). Backs up the cache before writing.

Run once:  /opt/anaconda3/bin/python3 migrate_shuffle_options.py
"""
import json
import os
import random
import shutil
import sys

from question_utils import normalize_question

CACHE = os.path.join(os.path.dirname(__file__), "questions_cache.json")
BACKUP = CACHE + ".bak"


def main():
    if not os.path.exists(CACHE):
        print(f"No cache at {CACHE} — nothing to do.")
        return

    with open(CACHE) as f:
        data = json.load(f)

    if not os.path.exists(BACKUP):
        shutil.copy2(CACHE, BACKUP)
        print(f"Backup written: {BACKUP}")
    else:
        print(f"Backup already exists (kept): {BACKUP}")

    changed = skipped = 0
    for key, q in data.items():
        if not isinstance(q, dict) or "options" not in q or "answer" not in q:
            skipped += 1
            continue
        if q.get("_shuffled"):
            skipped += 1
            continue
        # Seed per-question by its stable cache key -> reproducible shuffle.
        normalize_question(q, rng=random.Random(key))
        changed += 1

    with open(CACHE, "w") as f:
        json.dump(data, f)

    print(f"Migrated {changed} question(s); skipped {skipped}.")


if __name__ == "__main__":
    main()
