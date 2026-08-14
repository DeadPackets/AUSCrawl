"""Compare a freshly crawled term against the shipped database.

Usage: uv run --project . python scripts/crosscheck.py <new.db> <old.db> <term_id>
"""

import sqlite3
import sys


def crns(path, term):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT crn FROM courses WHERE term_id = ?", (term,))}
    finally:
        conn.close()


def main():
    new_db, old_db, term = sys.argv[1], sys.argv[2], sys.argv[3]
    a, b = crns(new_db, term), crns(old_db, term)
    only_new, only_old = sorted(a - b), sorted(b - a)
    print(f"term {term}: new={len(a)} old={len(b)} shared={len(a & b)}")
    print(f"  only in new ({len(only_new)}): {only_new[:20]}")
    print(f"  only in old ({len(only_old)}): {only_old[:20]}")
    return 0 if not only_old else 1


if __name__ == "__main__":
    raise SystemExit(main())
