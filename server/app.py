"""Flask application: REST API and dashboard for the Repo Analysis Tool.

Run with ``python3 -m server.app`` (or ``./run.sh``) and open
http://127.0.0.1:5000.

The API is intentionally thin - validation + wiring around :mod:`server.db`,
:mod:`server.ingest` and :mod:`server.metrics`.  Long-running imports (clone
and parse) run in background threads and expose progress through polled job
objects.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from flask import Flask, g, jsonify, request, send_from_directory

from server import db, ingest, metrics

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"

app = Flask("rat", static_folder=str(STATIC_DIR), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 ** 3  # 2 GiB archives allowed
app.config["JSON_SORT_KEYS"] = False

_JOBS: dict[str, ingest.Job] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 3600  # seconds a finished job stays pollable
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Connections / jobs
# ---------------------------------------------------------------------------

def get_conn():
    if "conn" not in g:
        g.conn = db.connect()
    return g.conn


@app.teardown_appcontext
def close_conn(_exc):
    conn = g.pop("conn", None)
    if conn is not None:
        conn.close()


def _evict_old_jobs() -> None:
    cutoff = time.time() - _JOB_TTL
    for job_id, job in list(_JOBS.items()):
        if job.state in ("done", "error") and job.created < cutoff:
            _JOBS.pop(job_id, None)


def _start_job(work, name: str = "", cleanup=None) -> ingest.Job:
    """Run ``work(conn, job)`` on a background thread, tracking it as a job."""
    job = ingest.Job(name=name)
    job.id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _evict_old_jobs()
        _JOBS[job.id] = job

    def runner() -> None:
        conn = None
        try:
            conn = db.connect()
            job.repo_id = work(conn, job)
            job.update(state="done", progress=1.0, message="Import complete")
        except Exception as exc:  # surface the real reason to the UI
            traceback.print_exc()
            job.error = str(exc) or exc.__class__.__name__
            job.update(state="error", message="Import failed")
        finally:
            if conn is not None:
                conn.close()
            if cleanup is not None:
                cleanup()

    threading.Thread(target=runner, daemon=True, name="rat-import").start()
    return job


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

@app.errorhandler(ApiError)
def _handle_api_error(exc: ApiError):
    return jsonify({"error": str(exc)}), exc.status


@app.errorhandler(404)
def _handle_404(_exc):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(STATIC_DIR, "index.html")


@app.errorhandler(413)
def _handle_413(_exc):
    return jsonify({"error": "upload exceeds the 2 GiB limit"}), 413


@app.errorhandler(Exception)
def _handle_unexpected(exc: Exception):
    traceback.print_exc()
    return jsonify({"error": f"internal error: {exc}"}), 500


# ---------------------------------------------------------------------------
# Static dashboard
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------

@app.get("/api/repos")
def api_list_repos():
    repos = db.list_repos(get_conn())
    with _JOBS_LOCK:
        _evict_old_jobs()
    for repo in repos:
        repo.pop("commit_count_live", None)
    return jsonify({"repos": repos})


@app.post("/api/repos")
def api_add_repo():
    """Add a repository from a zip upload (multipart ``file``) or a remote
    clone URL (JSON ``{"url": ...}``).  Returns a job to poll."""
    data_root = db.data_dir()

    if "file" in request.files:
        upload = request.files["file"]
        if not upload.filename:
            raise ApiError("no file uploaded")
        if not upload.filename.lower().endswith(".zip"):
            raise ApiError("only .zip archives with a .git directory are "
                           "supported")
        tmp_zip = data_root / f"upload-{uuid.uuid4().hex}.zip"
        upload.save(tmp_zip)
        name = (request.form.get("name")
                or Path(upload.filename).stem).strip() or "repository"

        def work(conn, job, tmp_zip=tmp_zip, name=name,
                 filename=upload.filename):
            return ingest.ingest_zip(
                conn, tmp_zip, db.data_dir() / "repos" / job.id, job,
                name=name, source=filename,
            )

        job = _start_job(work, name=name,
                         cleanup=lambda: tmp_zip.unlink(missing_ok=True))
        return jsonify(job.to_dict()), 202

    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url:
        raise ApiError("provide either a zip file or a repository URL")
    name = (payload.get("name")
            or ingest.repo_name_from_url(url)).strip() or "repository"

    def work(conn, job, url=url, name=name):
        return ingest.ingest_clone(conn, url,
                                   db.data_dir() / "repos" / job.id, job,
                                   name=name)

    job = _start_job(work, name=name)
    return jsonify(job.to_dict()), 202


@app.get("/api/jobs/<job_id>")
def api_job(job_id: str):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise ApiError("unknown job", 404)
    return jsonify(job.to_dict())


def _repo_container(repo_dir: str) -> Path:
    """The top directory owned by a repository import (safe to delete)."""
    base = (db.data_dir() / "repos").resolve()
    path = Path(repo_dir).resolve()
    while path != base and base in path.parents:
        parent = path.parent
        if parent == base:
            break
        path = parent
    return path


@app.delete("/api/repos/<int:repo_id>")
def api_delete_repo(repo_id: int):
    conn = get_conn()
    repo = db.get_repo(conn, repo_id)
    if repo is None:
        raise ApiError("unknown repository", 404)
    with _JOBS_LOCK:
        running = any(
            job.repo_id == repo_id and job.state not in ("done", "error")
            for job in _JOBS.values()
        )
    if running:
        raise ApiError("an import for this repository is still running", 409)
    db.delete_repo(conn, repo_id)
    shutil.rmtree(_repo_container(repo["dir"]), ignore_errors=True)
    return jsonify({"deleted": repo_id})


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

@app.get("/api/repos/<int:repo_id>/authors")
def api_authors(repo_id: int):
    conn = get_conn()
    if db.get_repo(conn, repo_id) is None:
        raise ApiError("unknown repository", 404)
    return jsonify({"authors": db.list_authors(conn, repo_id)})


@app.post("/api/repos/<int:repo_id>/authors/merge")
def api_merge_authors(repo_id: int):
    conn = get_conn()
    if db.get_repo(conn, repo_id) is None:
        raise ApiError("unknown repository", 404)
    payload = request.get_json(silent=True) or {}
    author_ids = payload.get("author_ids")
    name = (payload.get("name") or "").strip()
    if not isinstance(author_ids, list) or not author_ids:
        raise ApiError("author_ids must be a non-empty list")
    if not name:
        raise ApiError("a display name for the merged identity is required")
    try:
        updated = db.merge_authors(conn, repo_id, author_ids, name)
    except ValueError as exc:
        raise ApiError(str(exc)) from exc
    if updated == 0:
        raise ApiError("no matching author identities for this repository")
    return jsonify({"updated": updated,
                    "authors": db.list_authors(conn, repo_id)})


# ---------------------------------------------------------------------------
# Commit picker / object tree
# ---------------------------------------------------------------------------

@app.get("/api/repos/<int:repo_id>/commits")
def api_commits(repo_id: int):
    conn = get_conn()
    if db.get_repo(conn, repo_id) is None:
        raise ApiError("unknown repository", 404)
    query = (request.args.get("q") or "").strip()
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 500)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError as exc:
        raise ApiError("limit/offset must be integers") from exc

    where = ["c.repo_id = ?"]
    params: list = [repo_id]
    if query:
        where.append("(c.sha LIKE ? OR c.subject LIKE ?)")
        params.extend([query + "%", f"%{query}%"])
    clause = " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM commits c WHERE {clause}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"SELECT c.sha, c.ts, c.subject, a.group_key AS author "
        f"FROM commits c JOIN authors a ON a.id = c.author_id "
        f"WHERE {clause} ORDER BY c.ts DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return jsonify({"total": int(total), "commits": [dict(r) for r in rows]})


@app.get("/api/repos/<int:repo_id>/tree")
def api_tree(repo_id: int):
    conn = get_conn()
    if db.get_repo(conn, repo_id) is None:
        raise ApiError("unknown repository", 404)
    path = (request.args.get("path") or "").strip("/")
    return jsonify({"path": path,
                    "children": metrics.path_children(conn, repo_id, path)})


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _parse_shas(raw: str) -> list[str]:
    shas = [s.strip().lower() for s in raw.split(",") if s.strip()]
    if len(shas) > 5000:
        raise ApiError("too many commits selected (max 5000)")
    for sha in shas:
        if not _SHA_RE.match(sha):
            raise ApiError(f"invalid commit hash: {sha!r}")
    return shas


def _parse_filters() -> metrics.Filters:
    filters = metrics.Filters()

    raw_from = request.args.get("from")
    if raw_from not in (None, "", "null"):
        try:
            filters.from_ts = int(raw_from)
        except ValueError as exc:
            raise ApiError("`from` must be a UNIX timestamp") from exc

    raw_to = request.args.get("to")
    if raw_to not in (None, "", "null"):
        try:
            filters.to_ts = int(raw_to)
        except ValueError as exc:
            raise ApiError("`to` must be a UNIX timestamp") from exc

    raw_shas = request.args.get("shas")
    if raw_shas not in (None, "", "null"):
        filters.shas = _parse_shas(raw_shas)

    raw_authors = request.args.get("authors")
    if raw_authors not in (None, "", "null"):
        try:
            groups = json.loads(raw_authors)
        except json.JSONDecodeError as exc:
            raise ApiError("`authors` must be a JSON array") from exc
        if not isinstance(groups, list) or not all(
                isinstance(g, str) for g in groups):
            raise ApiError("`authors` must be a JSON array of group names")
        filters.groups = groups

    return filters


@app.get("/api/repos/<int:repo_id>/metrics")
def api_metrics(repo_id: int):
    conn = get_conn()
    repo = db.get_repo(conn, repo_id)
    if repo is None:
        raise ApiError("unknown repository", 404)

    path = (request.args.get("path") or "").strip("/")
    kind = request.args.get("kind") or "dir"
    if kind not in ("dir", "file"):
        raise ApiError("`kind` must be 'dir' or 'file'")

    # ``light=1`` skips the (heavier) children/timeseries aggregates; used by
    # the bulk ground-truth comparison script.
    light = request.args.get("light") in ("1", "true", "yes")

    filters = _parse_filters()
    mq = metrics.MetricQuery(conn, repo_id, filters)

    payload = {
        "repo": {"id": repo["id"], "name": repo["name"],
                 "kind": repo["kind"], "source": repo["source"]},
        "object": {"path": path, "kind": kind,
                   "label": path or "repository"},
        "metrics": mq.object_metrics(path, kind),
        "authors": mq.authors(path, kind),
        "children": [] if light or kind != "dir" else mq.children(path),
        "timeseries": [] if light else mq.timeseries(path, kind),
    }
    return jsonify(payload)


def main() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
