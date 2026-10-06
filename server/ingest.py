"""Repository ingestion and git-history parsing.

Two ingestion paths are supported:

* ``clone``  - a remote repository URL is deeply cloned (full history clone)
* ``zip``    - a zip archive of a repository that contains its ``.git``
               directory (or a ``.git`` file pointing inside the archive)

Both paths materialise the repository on disk and then run the same streaming
parser which feeds the SQLite tables (see :mod:`server.db`).

Parser design
-------------
A single ``git log`` invocation does all the heavy lifting:

    git log --no-merges -M50% --numstat -z --use-mailmap \
        --format='%x1e%H%x1f%an%x1f%ae%x1f%aN%x1f%aE%x1f%ct%x1f%s'

Rename detection is native to git (50% threshold as required by the brief);
binary files are reported by numstat as ``-\\t-`` and skipped because the
brief states binary files are not measured.  The ``-z`` mode makes every
record NUL-terminated, which is parsed as a flat token stream:

    \\x1e<sha>\\x1f<an>\\x1f<ae>\\x1f<aN>\\x1f<aE>\\x1f<ct>\\x1f<s>\\x00 \\n
    <added>\\t<removed>\\t<path>\\x00                       (normal change)
    <added>\\t<removed>\\t\\x00<old path>\\x00<new path>\\x00  (rename)
    -\\t-\\t<path>\\x00                                      (binary, skipped)

Rename records are attributed to their *new* path, so renaming a file does
not change its metrics while a rename with content changes only registers the
content delta - exactly the behaviour the brief requires.  Non-merge commits
reachable from the reference commit (default HEAD) are stored with the raw
identity (``%an/%ae``) plus the mailmap-canonical identity (``%aN/%aE``)
which seeds automatic author merging.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import zipfile
from collections import deque
from pathlib import Path

from server import db

# Token separators agreed with the --format string above.
RS = b"\x1e"   # record separator: starts a commit header token
FS = b"\x1f"   # field separator inside the commit header
NUL = b"\x00"

LOG_FORMAT = "%x1e%H%x1f%an%x1f%ae%x1f%aN%x1f%aE%x1f%ct%x1f%s"

_URL_RE = re.compile(r"^(https?://|ssh://|git://|git@)[^\s]+$")
_PROGRESS_RE = re.compile(
    r"(Counting objects|Compressing objects|Receiving objects|Resolving deltas)"
    r":\s+(\d+)%"
)


class IngestError(RuntimeError):
    """Raised when a repository cannot be ingested."""


class Job:
    """Tracks the progress of one background ingestion."""

    def __init__(self, name: str = "") -> None:
        self.id: str = ""
        self.name = name
        self.state = "pending"   # pending|cloning|extracting|parsing|done|error
        self.progress = 0.0
        self.message = ""
        self.error: str | None = None
        self.repo_id: int | None = None
        self.created = time.time()

    def update(self, *, state: str | None = None, progress: float | None = None,
               message: str | None = None) -> None:
        if state is not None:
            self.state = state
        if progress is not None:
            self.progress = max(0.0, min(1.0, progress))
        if message is not None:
            self.message = message

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state,
            "progress": round(self.progress, 4),
            "message": self.message,
            "error": self.error,
            "repo_id": self.repo_id,
        }


class NullJob(Job):
    """Job stand-in for tests / synchronous callers."""


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------

def run_git(args: list[str], cwd: str | Path | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise IngestError(
            f"git {' '.join(args[:2])} failed: "
            f"{detail[-1] if detail else 'exit code %d' % proc.returncode}"
        )
    return proc


def repo_name_from_url(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or "repository"


def clone(url: str, dest: str | Path, job: Job) -> None:
    """Deep (full history) clone of ``url`` into ``dest``."""
    if not _URL_RE.match(url.strip()):
        raise IngestError(
            "invalid repository URL - expected https://, ssh://, git:// or git@ "
            "form"
        )
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        ["git", "clone", "--progress", url.strip(), str(dest)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    tail: deque[str] = deque(maxlen=20)
    buf = ""
    assert proc.stderr is not None
    while True:
        chunk = proc.stderr.read(4096)
        if not chunk:
            break
        buf += chunk
        while True:
            match = re.search(r"[\r\n]", buf)
            if match is None:
                break
            line, buf = buf[: match.start()], buf[match.end():]
            line = line.strip()
            if not line:
                continue
            tail.append(line)
            progress = _PROGRESS_RE.search(line)
            if progress:
                # Clone occupies the first 45% of the overall job.
                job.update(progress=0.02 + 0.43 * int(progress.group(2)) / 100,
                           message=line)
            elif line.startswith("Cloning into"):
                job.update(message=line)
    if proc.wait() != 0:
        raise IngestError(
            "git clone failed: " + (tail[-1] if tail else "unknown error")
        )


def _find_repo_root(root: Path) -> Path | None:
    """Locate the shallowest directory that looks like a git repository."""
    candidates: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        depth = len(current.relative_to(root).parts)
        if depth > 3:
            dirnames[:] = []
            continue
        if ".git" in dirnames or ".git" in filenames:
            candidates.append(current)
            dirnames[:] = []
            continue
        if ((current / "HEAD").is_file() and (current / "objects").is_dir()
                and (current / "refs").is_dir()):
            candidates.append(current)
            dirnames[:] = []
    if not candidates:
        return None
    candidates.sort(key=lambda c: len(c.relative_to(root).parts))
    return candidates[0]


def extract_zip(zip_path: str | Path, dest: str | Path, job: Job) -> Path:
    """Extract ``zip_path`` into ``dest`` and return the repository root."""
    zip_path = Path(zip_path)
    dest = Path(dest)
    job.update(state="extracting", message="Extracting archive")
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                target = (dest / info.filename).resolve()
                if dest_resolved not in target.parents and target != dest_resolved:
                    raise IngestError(
                        "archive contains an entry outside the extraction root "
                        f"({info.filename!r})"
                    )
            archive.extractall(dest)
    except zipfile.BadZipFile as exc:
        raise IngestError(f"not a valid zip archive: {exc}") from exc

    root = _find_repo_root(dest)
    if root is None:
        raise IngestError(
            "the archive does not contain a git repository "
            "(no .git directory or file found)"
        )
    git_entry = root / ".git"
    if git_entry.is_file():
        content = git_entry.read_text(errors="replace").strip()
        if not content.startswith("gitdir:"):
            raise IngestError("the .git file in the archive is malformed")
        target = content.split(":", 1)[1].strip()
        resolved = (root / target).resolve()
        if not resolved.exists():
            raise IngestError(
                "the .git file references a git directory that is not part of "
                "the archive"
            )
    return root


# ---------------------------------------------------------------------------
# History parser
# ---------------------------------------------------------------------------

def _iter_nul_tokens(stream, chunk_size: int = 1 << 20):
    """Yield NUL-terminated byte tokens from a binary stream."""
    buf = b""
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            if buf:
                yield buf
            return
        buf += chunk
        if NUL in buf:
            parts = buf.split(NUL)
            buf = parts.pop()
            yield from parts


def parse_repo(conn, repo_id: int, repo_dir: str | Path, ref: str = "HEAD",
               job: Job | None = None) -> int:
    """Parse the full history of ``repo_dir`` into the database.

    Returns the number of non-merge commits reachable from ``ref``.
    """
    job = job if job is not None else NullJob()
    repo_dir = str(repo_dir)

    verify = run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                     cwd=repo_dir, check=False)
    if verify.returncode != 0:
        raise IngestError(f"reference {ref!r} does not resolve to a commit")

    total = int(run_git(["rev-list", "--count", "--no-merges", ref],
                        cwd=repo_dir).stdout.strip() or 0)
    if total == 0:
        raise IngestError("repository contains no non-merge commits")

    # Fresh parse: drop history-derived rows but keep authors so that manual
    # identity merges survive a re-parse.
    with conn:
        conn.execute("DELETE FROM changes WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM commits WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM repo_paths WHERE repo_id = ?", (repo_id,))

    job.update(state="parsing", progress=0.45,
               message=f"Parsing {total} commits")

    args = [
        "git", "-C", repo_dir, "log", "--no-merges", "-M50%", "--numstat",
        "-z", "--use-mailmap", f"--format={LOG_FORMAT}", ref,
    ]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    assert proc.stdout is not None

    author_cache: dict[tuple[str, str], int] = {}
    commit_rows: list[tuple] = []
    change_rows: list[tuple] = []
    binary_paths: set[str] = set()   # excluded from the object tree

    def resolve_author(raw_name: str, raw_email: str,
                       group_key: str) -> int:
        key = (raw_name, raw_email)
        cached = author_cache.get(key)
        if cached is not None:
            return cached
        row = conn.execute(
            "SELECT id FROM authors WHERE repo_id = ? AND name = ? AND email = ?",
            (repo_id, raw_name, raw_email),
        ).fetchone()
        if row is not None:
            author_id = int(row["id"])
        else:
            cur = conn.execute(
                "INSERT INTO authors (repo_id, name, email, group_key) "
                "VALUES (?, ?, ?, ?)",
                (repo_id, raw_name, raw_email, group_key),
            )
            author_id = int(cur.lastrowid)
        author_cache[key] = author_id
        return author_id

    def flush() -> None:
        if commit_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO commits "
                "(repo_id, sha, ts, author_id, subject) VALUES (?, ?, ?, ?, ?)",
                commit_rows,
            )
            commit_rows.clear()
        if change_rows:
            conn.executemany(
                "INSERT INTO changes "
                "(repo_id, sha, ts, author_id, path, added, removed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                change_rows,
            )
            change_rows.clear()
        conn.commit()

    # Parser state -----------------------------------------------------------
    cur_sha: str | None = None
    cur_ts = 0
    cur_subject = ""
    cur_author_id = 0
    cur_rows: list[tuple[str, int, int]] = []
    processed = 0
    expect_lf = False       # the LF git emits right after a commit header
    rename_stage = 0        # 0 none, 1 expecting old path, 2 expecting new path
    pend_added = 0
    pend_removed = 0
    pend_binary = False

    def finish_commit() -> None:
        nonlocal cur_sha
        if cur_sha is None:
            return
        commit_rows.append(
            (repo_id, cur_sha, cur_ts, cur_author_id, cur_subject)
        )
        for path, added, removed in cur_rows:
            change_rows.append(
                (repo_id, cur_sha, cur_ts, cur_author_id, path, added, removed)
            )
        cur_sha = None

    def maybe_flush() -> None:
        """Flush completed commits to SQLite and report progress."""
        if processed and processed % 2000 == 0 and (commit_rows or change_rows):
            flush()
            job.update(
                progress=0.45 + 0.53 * processed / max(total, 1),
                message=f"Parsing commits: {processed}/{total}",
            )

    try:
        for token in _iter_nul_tokens(proc.stdout):
            if expect_lf and token.startswith(b"\n"):
                token = token[1:]
                expect_lf = False
            if token.startswith(RS):
                finish_commit()
                maybe_flush()
                fields = token[1:].split(FS, 6)
                if len(fields) < 6:
                    continue  # malformed header, skip defensively
                sha = fields[0].decode("ascii", "replace")
                raw_name = fields[1].decode("utf-8", "replace")
                raw_email = fields[2].decode("utf-8", "replace")
                mapped_name = fields[3].decode("utf-8", "replace")
                mapped_email = fields[4].decode("utf-8", "replace")
                try:
                    ts = int(fields[5])
                except ValueError:
                    continue
                subject = (
                    fields[6].decode("utf-8", "replace")[:250]
                    if len(fields) > 6 else ""
                )
                expect_lf = True
                cur_sha = sha
                cur_ts = ts
                cur_subject = subject
                cur_rows = []
                group_name = mapped_name or raw_name
                group_email = mapped_email or raw_email
                group_key = (
                    f"{group_name} <{group_email}>".strip()
                    if (group_name or group_email) else raw_email
                )
                cur_author_id = resolve_author(raw_name, raw_email, group_key)
                processed += 1
                continue
            if not token:
                continue
            if rename_stage == 1:
                # old path of a rename - metrics are attributed to the new path
                rename_stage = 2
                continue
            if rename_stage == 2:
                rename_stage = 0
                if pend_binary:
                    binary_paths.add(token.decode("utf-8", "replace"))
                elif pend_added or pend_removed:
                    new_path = token.decode("utf-8", "replace")
                    cur_rows.append((new_path, pend_added, pend_removed))
                continue
            parts = token.split(b"\t", 2)
            if len(parts) != 3:
                continue  # malformed numstat record
            added_s, removed_s, path_s = parts
            binary = added_s == b"-" or removed_s == b"-"
            try:
                added = 0 if binary else int(added_s)
                removed = 0 if binary else int(removed_s)
            except ValueError:
                continue
            if path_s == b"":
                # rename marker; old and new paths follow as separate tokens
                pend_added, pend_removed, pend_binary = added, removed, binary
                rename_stage = 1
                continue
            if binary:
                binary_paths.add(path_s.decode("utf-8", "replace"))
                continue
            if not (added or removed):
                continue
            cur_rows.append((path_s.decode("utf-8", "replace"), added, removed))
    finally:
        if proc.stdout:
            proc.stdout.close()
        stderr = proc.stderr.read() if proc.stderr else b""
        if proc.stderr:
            proc.stderr.close()
        rc = proc.wait()

    finish_commit()
    flush()

    if rc != 0:
        raise IngestError(
            "git log failed: "
            + (stderr.decode("utf-8", "replace").strip().splitlines() or
               ["unknown error"])[-1]
        )

    # Record every file present at the reference state so the object tree can
    # list untouched files as well.  Binary files stay hidden - they are not
    # measured by definition.
    ls_tree = run_git(["ls-tree", "-r", "-z", "--name-only", ref],
                      cwd=repo_dir)
    paths = [p for p in ls_tree.stdout.split("\x00")
             if p and p not in binary_paths]
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO repo_paths (repo_id, path) VALUES (?, ?)",
            [(repo_id, p) for p in paths],
        )

    job.update(state="parsing", progress=0.98,
               message=f"Parsed {processed} commits")
    return processed


# ---------------------------------------------------------------------------
# Top-level import orchestration
# ---------------------------------------------------------------------------

def import_local(conn, name: str, source: str, kind: str,
                 repo_dir: str | Path, ref: str = "HEAD",
                 job: Job | None = None) -> int:
    """Register and parse an already-materialised repository."""
    job = job if job is not None else NullJob()
    repo_id = db.create_repo(conn, name=name, source=source, kind=kind,
                             dir_=str(repo_dir), ref=ref)
    job.repo_id = repo_id
    try:
        count = parse_repo(conn, repo_id, repo_dir, ref=ref, job=job)
        db.update_repo(conn, repo_id, commit_count=count)
    except Exception:
        db.delete_repo(conn, repo_id)
        shutil.rmtree(repo_dir, ignore_errors=True)
        raise
    return repo_id


def ingest_clone(conn, url: str, dest: str | Path, job: Job,
                 name: str | None = None) -> int:
    """Clone ``url`` and import it. Returns the new repo id."""
    job.update(state="cloning", message=f"Cloning {url}")
    clone(url, dest, job)
    return import_local(conn, name=name or repo_name_from_url(url), source=url,
                        kind="clone", repo_dir=dest, job=job)


def ingest_zip(conn, zip_path: str | Path, dest: str | Path, job: Job,
               name: str | None = None, source: str | None = None) -> int:
    """Extract a repository zip and import it. Returns the new repo id."""
    root = extract_zip(zip_path, dest, job)
    return import_local(conn, name=name or Path(zip_path).stem,
                        source=source or Path(zip_path).name, kind="zip",
                        repo_dir=root, job=job)
