"""Cross-check RAT metrics against the reference CSVs shipped with the brief.

Each reference CSV pins a repository at a reference commit (``ref_sha``) and
lists, for every object (repository / directory / file): the aggregate metrics
(``author=ALL`` row) and the per-author breakdown including churn ownership.

This script verifies the whole pipeline end to end:

1. clones the repository into ``data/refcheck/clones/<name>`` (once),
2. imports it into a scratch RAT database pinned at ``ref_sha``
   (``data/refcheck/db/<name>``, re-used while the ref is unchanged),
3. replays every object row through the public
   ``GET /api/repos/<id>/metrics`` endpoint,
4. compares added / removed / growth / churn / modifications /
   modification_frequency / churn_rate / |H| and the per-author rows
   (added / removed / churn / modifications / ownership).

Exit code is 0 only when every row matches the reference.

Usage::

    python3 tests/compare_reference.py tests/references/cJSON.csv.gz \
        --url https://github.com/DaveGamble/cJSON.git
"""
from __future__ import annotations

import argparse
import csv
import gzip
import math
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECK_DIR = ROOT / "data" / "refcheck"


# ---------------------------------------------------------------------------
# Reference loading
# ---------------------------------------------------------------------------

def _open_reference(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return open(path, "r", encoding="utf-8", newline="")


def load_reference(path: Path) -> dict:
    """{repo, ref_sha, objects: {(type, path): {all: row, authors: {name: row}}}}"""
    objects: dict[tuple[str, str], dict] = {}
    repo_name = ref_sha = None
    with _open_reference(path) as handle:
        for row in csv.DictReader(handle):
            repo_name = repo_name or row["repo"]
            ref_sha = ref_sha or row["ref_sha"]
            bucket = objects.setdefault(
                (row["object_type"], row["path"]), {"all": None, "authors": {}}
            )
            if row["author"] == "ALL":
                bucket["all"] = row
            else:
                bucket["authors"][row["author"]] = row
    if not objects or ref_sha is None:
        raise SystemExit(f"{path}: no rows found")
    return {"repo": repo_name, "ref_sha": ref_sha, "objects": objects}


# ---------------------------------------------------------------------------
# Clone + import
# ---------------------------------------------------------------------------

def git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd),
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: "
                           f"{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def ensure_clone(name: str, url: str) -> Path:
    dest = CHECK_DIR / "clones" / name
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"cloning {url} -> {dest} ...")
        result = subprocess.run(["git", "clone", "--quiet", url, str(dest)])
        if result.returncode != 0:
            raise RuntimeError(f"clone of {url} failed")
    return dest


def ensure_import(name: str, url: str, clone_dir: Path, ref_sha: str):
    data_dir = CHECK_DIR / "db" / name
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["RAT_DATA_DIR"] = str(data_dir)

    from server import app as app_module, db, ingest

    git(["cat-file", "-e", f"{ref_sha}^{{commit}}"], clone_dir)  # ref exists

    marker = data_dir / "imported.sha"
    repo_id = None
    if marker.exists() and marker.read_text().strip() == ref_sha:
        conn = db.connect()
        row = conn.execute("SELECT id FROM repos WHERE source = ?",
                           (url,)).fetchone()
        conn.close()
        if row is not None:
            repo_id = int(row["id"])
            print(f"reusing existing import of {name} @ {ref_sha[:12]} "
                  f"(repo {repo_id})")

    if repo_id is None:
        conn = db.connect()
        for row in conn.execute("SELECT id FROM repos WHERE source = ?",
                                (url,)).fetchall():
            db.delete_repo(conn, int(row["id"]))
        started = time.time()
        repo_id = ingest.import_local(conn, name=name, source=url,
                                      kind="local", repo_dir=str(clone_dir),
                                      ref=ref_sha)
        conn.close()
        marker.write_text(ref_sha)
        print(f"imported {name} @ {ref_sha[:12]} in {time.time() - started:.1f}s "
              f"(repo {repo_id})")
    return app_module, repo_id


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def approx(a, b) -> bool:
    return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)


EXACT_FIELDS = ("added", "removed", "growth", "churn", "modifications")
FLOAT_FIELDS = ("modification_frequency", "churn_rate")


def compare_all_metrics(actual: dict, expected: dict, label: str) -> list[str]:
    diffs = []
    for field in EXACT_FIELDS:
        if int(actual[field]) != int(expected[field]):
            diffs.append(f"{label}: {field} expected {expected[field]} "
                         f"got {actual[field]}")
    for field in FLOAT_FIELDS:
        if not approx(actual[field], expected[field]):
            diffs.append(f"{label}: {field} expected {expected[field]} "
                         f"got {actual[field]}")
    return diffs


def compare_authors(actual_authors: list[dict], expected: dict[str, dict],
                    label: str, per_author_report: int = 4) -> list[str]:
    diffs = []
    actual = {a["name"]: a for a in actual_authors}
    for author in sorted(set(expected) - set(actual))[:per_author_report]:
        diffs.append(f"{label}: missing author row for {author}")
    for author in sorted(set(actual) - set(expected))[:per_author_report]:
        diffs.append(f"{label}: unexpected author row for {author}")
    for author, want in expected.items():
        got = actual.get(author)
        if got is None:
            continue
        for field in ("added", "removed", "churn", "modifications"):
            if int(got[field]) != int(want[field]):
                diffs.append(f"{label}: author {author} {field} expected "
                             f"{want[field]} got {got[field]}")
        if not approx(got["ownership"], want["ownership"]):
            diffs.append(f"{label}: author {author} ownership expected "
                         f"{want['ownership']} got {got['ownership']}")
    return diffs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="reference csv (optionally .gz)")
    parser.add_argument("--url", required=True, help="repository clone URL")
    parser.add_argument("--name", help="short name (default: repo column)")
    parser.add_argument("--max-report", type=int, default=40)
    args = parser.parse_args()

    reference = load_reference(args.csv)
    name = args.name or reference["repo"]
    ref_sha = reference["ref_sha"]

    clone_dir = ensure_clone(name, args.url)
    app_module, repo_id = ensure_import(name, args.url, clone_dir, ref_sha)

    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()

    objects = reference["objects"]
    expected_commits = int(objects[("repository", "/")]["all"]["commit_count"])
    diffs: list[str] = []
    checked = 0
    started = time.time()

    for object_type, path in sorted(objects):
        bucket = objects[(object_type, path)]
        api_kind = "file" if object_type == "file" else "dir"
        api_path = "" if path == "/" else path
        response = client.get(
            f"/api/repos/{repo_id}/metrics",
            query_string={"path": api_path, "kind": api_kind, "light": "1"},
        )
        if response.status_code != 200:
            diffs.append(f"{object_type} {path}: HTTP "
                         f"{response.status_code} {response.get_json()}")
            continue
        payload = response.get_json()
        label = f"{object_type} '{path or '/'}'"

        commits = payload["metrics"]["commits"]
        if commits != expected_commits:
            diffs.append(f"{label}: |H| expected {expected_commits} "
                         f"got {commits}")
        if bucket["all"] is not None:
            diffs.extend(compare_all_metrics(payload["metrics"],
                                             bucket["all"], label))
        diffs.extend(compare_authors(payload["authors"], bucket["authors"],
                                     label))
        checked += 1

    elapsed = time.time() - started
    print(f"\nchecked {checked}/{len(objects)} objects for {name} "
          f"@ {ref_sha[:12]} in {elapsed:.1f}s")
    if diffs:
        print(f"{len(diffs)} mismatches:")
        for line in diffs[:args.max_report]:
            print("  " + line)
        if len(diffs) > args.max_report:
            print(f"  ... and {len(diffs) - args.max_report} more")
        return 1
    print("ALL VALUES MATCH THE REFERENCE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
