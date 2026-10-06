#!/usr/bin/env python3
"""Independent metric cross-check for the RAT ("second implementation").

Recomputes object metrics directly from git with a *separate* parser (plain
text numstat with arrow/brace rename notation - the opposite of the
NUL-separated parser in ``server/ingest.py``), so divergences between the two
implementations are detectable.

Usage:
    python3 tests/ground_truth.py REPO_DIR [REF] [FROM_TS] [TO_TS] [PATH]

Examples:
    python3 tests/ground_truth.py /path/to/src                # whole history
    python3 tests/ground_truth.py /path/to/src HEAD 1700000000 1710000000 src
    python3 tests/ground_truth.py /path/to/src HEAD - - foo/bar.c
"""
from __future__ import annotations

import re
import subprocess
import sys


def parse_numstat(repo_dir: str, ref: str) -> list[tuple[int, dict]]:
    """Return [(committer_ts, {path: (added, removed)})] for non-merge commits."""
    proc = subprocess.run(
        ["git", "-C", repo_dir, "log", "--no-merges", "-M50%", "--numstat",
         "--use-mailmap", "--format=\x01%ct", ref],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"git log failed: {proc.stderr.strip()}")

    commits: list[tuple[int, dict]] = []
    changes: dict = {}
    for line in proc.stdout.splitlines():
        if line.startswith("\x01"):
            changes = {}
            commits.append((int(line[1:]), changes))
            continue
        if not changes and not commits:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_s, removed_s, path = parts
        if added_s == "-" or removed_s == "-":
            continue  # binary - not measured
        if " => " in path:
            if "{" in path and "}" in path:
                path = re.sub(r"\{[^{}]* => ([^{}]*)\}", r"\1", path)
            else:
                path = path.split(" => ", 1)[1]
        if len(path) > 1 and path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        added, removed = int(added_s), int(removed_s)
        if added or removed:
            prev_a, prev_r = changes.get(path, (0, 0))
            changes[path] = (prev_a + added, prev_r + removed)
    return commits


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    repo_dir = sys.argv[1]
    ref = sys.argv[2] if len(sys.argv) > 2 else "HEAD"
    from_ts = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] != "-" else None
    to_ts = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] != "-" else None
    path = sys.argv[5] if len(sys.argv) > 5 else ""

    commits = parse_numstat(repo_dir, ref)
    selected = [
        entry for entry in commits
        if (from_ts is None or entry[0] >= from_ts)
        and (to_ts is None or entry[0] < to_ts)
    ]

    def in_object(candidate: str) -> bool:
        if not path:
            return True
        return candidate == path or candidate.startswith(path + "/")

    added = removed = modifications = 0
    files: dict[str, tuple[int, int]] = {}
    for _ts, changes in selected:
        touched = False
        for candidate, (a, r) in changes.items():
            if not in_object(candidate):
                continue
            if a or r:                 # positive churn only (0/0 renames excluded)
                touched = True
            added += a
            removed += r
            prev = files.get(candidate, (0, 0))
            files[candidate] = (prev[0] + a, prev[1] + r)
        if touched:
            modifications += 1

    print(f"object        : {path or '<repository root>'}")
    print(f"|H|           : {len(selected)}")
    print(f"added         : {added}")
    print(f"removed       : {removed}")
    print(f"growth        : {added - removed}")
    print(f"churn         : {added + removed}")
    print(f"modifications : {modifications}")
    if selected:
        print(f"mod frequency : {modifications / len(selected):.6f}")
        print(f"churn rate    : {(added + removed) / len(selected):.6f}")
    print("\nper-file (top by churn):")
    top = sorted(files.items(),
                 key=lambda item: -(item[1][0] + item[1][1]))[:20]
    for candidate, (a, r) in top:
        print(f"  {a:>8} {r:>8}  {candidate}")


if __name__ == "__main__":
    main()
