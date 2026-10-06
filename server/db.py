"""SQLite storage layer for the Repo Analysis Tool.

A single database file holds every imported repository; all tables are keyed
by ``repo_id`` so multiple repositories can be analysed side by side.  The
intended usage pattern is one connection per request / importer thread
(SQLite handles cross-connection concurrency in WAL mode).

Data model
----------
repos        one row per imported repository
authors      raw commit identities (name + email) of a repository; ``group_key``
             is the canonical display identity (seeded from ``.mailmap``,
             rewritable by manual merges)
commits      non-merge commits reachable from the reference commit
changes      one row per measured file touched by a commit.  ``added`` /
             ``removed`` are the numstat line counts with rename detection at
             50%.  Binary files are never inserted here (they are not
             measured).  ``ts`` and ``author_id`` are denormalised from the
             commit row so every metric query is a single-table scan.
repo_paths   every file present at the reference state (used to complete the
             object tree with untouched files)
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

_DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    kind         TEXT    NOT NULL,              -- 'clone' | 'zip' | 'local'
    dir          TEXT    NOT NULL,
    ref          TEXT    NOT NULL DEFAULT 'HEAD',
    status       TEXT    NOT NULL DEFAULT 'ready', -- 'ready' | 'error'
    error        TEXT    NOT NULL DEFAULT '',
    commit_count INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS authors (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id   INTEGER NOT NULL,
    name      TEXT    NOT NULL,
    email     TEXT    NOT NULL,
    group_key TEXT    NOT NULL,
    UNIQUE (repo_id, name, email)
);

CREATE TABLE IF NOT EXISTS commits (
    repo_id   INTEGER NOT NULL,
    sha       TEXT    NOT NULL,
    ts        INTEGER NOT NULL,               -- committer date, UNIX seconds
    author_id INTEGER NOT NULL,
    subject   TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (repo_id, sha)
);

CREATE TABLE IF NOT EXISTS changes (
    repo_id   INTEGER NOT NULL,
    sha       TEXT    NOT NULL,
    ts        INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    path      TEXT    NOT NULL,
    added     INTEGER NOT NULL,
    removed   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS repo_paths (
    repo_id INTEGER NOT NULL,
    path    TEXT    NOT NULL,
    PRIMARY KEY (repo_id, path)
);

CREATE INDEX IF NOT EXISTS idx_changes_repo_path   ON changes  (repo_id, path);
CREATE INDEX IF NOT EXISTS idx_changes_repo_ts     ON changes  (repo_id, ts);
CREATE INDEX IF NOT EXISTS idx_changes_repo_sha    ON changes  (repo_id, sha);
CREATE INDEX IF NOT EXISTS idx_changes_repo_author ON changes  (repo_id, author_id);
CREATE INDEX IF NOT EXISTS idx_commits_repo_ts     ON commits  (repo_id, ts);
CREATE INDEX IF NOT EXISTS idx_commits_repo_author ON commits  (repo_id, author_id);
"""


def data_dir() -> Path:
    """Root directory for RAT runtime data (DB + materialised repositories)."""
    path = Path(os.environ.get("RAT_DATA_DIR", str(_DEFAULT_DATA_DIR)))
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "rat.db"


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open (and initialise, if needed) the RAT database."""
    conn = sqlite3.connect(str(path or db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Repository helpers
# ---------------------------------------------------------------------------

def create_repo(conn: sqlite3.Connection, name: str, source: str, kind: str,
                dir_: str, ref: str = "HEAD", status: str = "ready") -> int:
    cur = conn.execute(
        "INSERT INTO repos (name, source, kind, dir, ref, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, source, kind, str(dir_), ref, status, int(time.time())),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_repo(conn: sqlite3.Connection, repo_id: int, **fields) -> None:
    allowed = {"name", "source", "kind", "dir", "ref", "status", "error",
               "commit_count"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"cannot update repos.{key}")
        sets.append(f"{key} = ?")
        params.append(value)
    if not sets:
        return
    params.append(repo_id)
    conn.execute(f"UPDATE repos SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()


def list_repos(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT r.*, (SELECT COUNT(*) FROM commits c WHERE c.repo_id = r.id) "
        "             AS commit_count_live "
        "FROM repos r ORDER BY r.created_at"
    ).fetchall()
    return [dict(row) for row in rows]


def get_repo(conn: sqlite3.Connection, repo_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    return dict(row) if row else None


def delete_repo(conn: sqlite3.Connection, repo_id: int) -> None:
    with conn:
        for table in ("authors", "commits", "changes", "repo_paths", "repos"):
            conn.execute(f"DELETE FROM {table} WHERE " +
                         ("id = ?" if table == "repos" else "repo_id = ?"),
                         (repo_id,))


# ---------------------------------------------------------------------------
# Author helpers
# ---------------------------------------------------------------------------

def author_ids_for_groups(conn: sqlite3.Connection, repo_id: int,
                          groups: list[str]) -> list[int]:
    """Resolve author group keys to the raw author ids that belong to them."""
    if not groups:
        return []
    marks = ",".join("?" for _ in groups)
    rows = conn.execute(
        f"SELECT id FROM authors WHERE repo_id = ? AND group_key IN ({marks})",
        [repo_id, *groups],
    ).fetchall()
    return [int(row["id"]) for row in rows]


def merge_authors(conn: sqlite3.Connection, repo_id: int,
                  author_ids: list[int], group_name: str) -> int:
    """Merge (or rename) author identities into a single display identity.

    ``group_key`` is what metrics group by, so a single UPDATE is enough no
    matter how much history the authors own.
    """
    author_ids = [int(a) for a in author_ids]
    if not author_ids:
        raise ValueError("author_ids must not be empty")
    if not group_name.strip():
        raise ValueError("group name must not be empty")
    marks = ",".join("?" for _ in author_ids)
    with conn:
        cur = conn.execute(
            f"UPDATE authors SET group_key = ? "
            f"WHERE repo_id = ? AND id IN ({marks})",
            [group_name.strip(), repo_id, *author_ids],
        )
    return cur.rowcount


def list_authors(conn: sqlite3.Connection, repo_id: int) -> list[dict]:
    """Raw author identities with their overall history totals."""
    rows = conn.execute(
        """
        SELECT a.id, a.name, a.email, a.group_key,
               (SELECT COUNT(*) FROM commits c
                 WHERE c.repo_id = a.repo_id AND c.author_id = a.id) AS commits,
               (SELECT COALESCE(SUM(ch.added + ch.removed), 0) FROM changes ch
                 WHERE ch.repo_id = a.repo_id AND ch.author_id = a.id) AS churn
        FROM authors a
        WHERE a.repo_id = ?
        ORDER BY churn DESC, a.name
        """,
        (repo_id,),
    ).fetchall()
    return [dict(row) for row in rows]
