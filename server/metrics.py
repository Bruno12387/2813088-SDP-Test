"""Metric computation for the Repo Analysis Tool.

Every metric from the brief is computed with SQL aggregation over the
``changes`` table (one row per touched file per non-merge commit).

Notation mapping (brief -> implementation)
------------------------------------------
H            the selected commit set: time range (``from_ts <= ts < to_ts``),
             manual commit list, or the whole history - optionally narrowed
             to a set of author groups.
|H|          number of non-merge commits in H.
l+/l-        ``SUM(added)`` / ``SUM(removed)``.
delta        growth           = added - removed.
lambda       churn            = added + removed.
n(H,o)       modifications    = commits in H in which the object o had
                                positive churn (``SUM(added+removed) > 0``).
eta          modification frequency = n(H,o) / |H|.
rho          churn rate            = lambda(H,o) / |H|.
n(H,o,a)     author modifications  = same as n but split per author group.
lambda(H,o,a) author churn.
omega        ownership = lambda(H,o,a) / lambda(H,o).

Key algorithmic property: directory (and repository) metrics telescope - the
added/removed/churn of a directory is the sum over all descendant files.
Therefore directory and repository metrics are *descendant-prefix sums*
(``path LIKE d || '/%'``; the repository is every path) and no tree walk or
per-directory materialisation is ever needed; a single indexed SQL scan
answers each metric for any object.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timezone

from server import db


@dataclass
class Filters:
    """The commit-set selection driving every metric query."""

    from_ts: int | None = None          # H_t / H_i,j lower bound (inclusive)
    to_ts: int | None = None            # upper bound (exclusive)
    shas: list[str] | None = None       # manual commit selection
    groups: list[str] | None = None     # author group keys (None = all)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _path_pred(path: str, kind: str, column: str = "path") -> tuple[str, list]:
    """SQL predicate selecting an object's change rows.

    Files match their exact path. Directories match their descendants
    (``d/%``) only: a repository may contain both a file ``x`` and a
    directory ``x/`` at different points in history - they are distinct
    objects and must not be conflated (e.g. ``git-gui`` in git.git).
    """
    if not path:
        return "1 = 1", []
    if kind == "file":
        return f"{column} = ?", [path]
    return (f"{column} LIKE ? ESCAPE '\\'", [_escape_like(path) + "/%"])


class MetricQuery:
    """Runs the metric queries for one repository + filter combination."""

    def __init__(self, conn, repo_id: int, filters: Filters | None = None):
        self.conn = conn
        self.repo_id = repo_id
        self.filters = filters or Filters()
        self.author_ids: list[int] | None = None
        if self.filters.groups is not None:
            self.author_ids = db.author_ids_for_groups(
                conn, repo_id, self.filters.groups
            )

    # -- predicates ---------------------------------------------------------

    def _base_sql(self, prefix: str = "") -> tuple[str, list]:
        col = (lambda name: f"{prefix}{name}") if prefix else (lambda name: name)
        parts = [f"{col('repo_id')} = ?"]
        params: list = [self.repo_id]
        shas = self.filters.shas
        if shas is not None:
            if not shas:
                parts.append("1 = 0")
            else:
                marks = ",".join("?" for _ in shas)
                parts.append(f"{col('sha')} IN ({marks})")
                params.extend(shas)
        else:
            if self.filters.from_ts is not None:
                parts.append(f"{col('ts')} >= ?")
                params.append(self.filters.from_ts)
            if self.filters.to_ts is not None:
                parts.append(f"{col('ts')} < ?")
                params.append(self.filters.to_ts)
        if self.author_ids is not None:
            if not self.author_ids:
                parts.append("1 = 0")
            else:
                marks = ",".join("?" for _ in self.author_ids)
                parts.append(f"{col('author_id')} IN ({marks})")
                params.extend(self.author_ids)
        return " AND ".join(parts), params

    # -- atomic queries ------------------------------------------------------

    def commit_count(self) -> int:
        """|H| - the number of non-merge commits in the selected set."""
        where, params = self._base_sql()
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM commits WHERE {where}", params
        ).fetchone()
        return int(row["n"])

    def totals(self, path: str, kind: str) -> tuple[int, int]:
        where, params = self._base_sql()
        pred, pred_params = _path_pred(path, kind)
        row = self.conn.execute(
            f"SELECT COALESCE(SUM(added), 0) AS a, "
            f"       COALESCE(SUM(removed), 0) AS r "
            f"FROM changes WHERE {where} AND {pred}",
            params + pred_params,
        ).fetchone()
        return int(row["a"]), int(row["r"])

    def modifications(self, path: str, kind: str) -> int:
        """n(H,o): commits in H with positive churn on the object."""
        where, params = self._base_sql()
        pred, pred_params = _path_pred(path, kind)
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM ("
            f"  SELECT 1 FROM changes WHERE {where} AND {pred} "
            f"  GROUP BY sha HAVING SUM(added) + SUM(removed) > 0"
            f")",
            params + pred_params,
        ).fetchone()
        return int(row["n"])

    # -- composite metrics ---------------------------------------------------

    def object_metrics(self, path: str, kind: str) -> dict:
        """Complete metric panel for one file/directory ('' = repository)."""
        commits = self.commit_count()
        added, removed = self.totals(path, kind)
        mods = self.modifications(path, kind)
        churn = added + removed
        return {
            "commits": commits,
            "added": added,
            "removed": removed,
            "growth": added - removed,
            "churn": churn,
            "modifications": mods,
            "modification_frequency": mods / commits if commits else 0.0,
            "churn_rate": churn / commits if commits else 0.0,
        }

    def authors(self, path: str, kind: str) -> list[dict]:
        """Per-author metric breakdown for one object within H."""
        base, params = self._base_sql("c.")
        pred, pred_params = _path_pred(path, kind, "c.path")

        sums = self.conn.execute(
            f"SELECT a.group_key AS name, "
            f"       COALESCE(SUM(c.added), 0) AS added, "
            f"       COALESCE(SUM(c.removed), 0) AS removed, "
            f"       COALESCE(SUM(c.added + c.removed), 0) AS churn "
            f"FROM changes c JOIN authors a ON a.id = c.author_id "
            f"WHERE {base} AND {pred} "
            f"GROUP BY a.group_key",
            params + pred_params,
        ).fetchall()

        mods = self.conn.execute(
            f"SELECT name, COUNT(*) AS n FROM ("
            f"  SELECT a.group_key AS name, c.sha FROM changes c "
            f"  JOIN authors a ON a.id = c.author_id "
            f"  WHERE {base} AND {pred} "
            f"  GROUP BY a.group_key, c.sha "
            f"  HAVING SUM(c.added) + SUM(c.removed) > 0"
            f") GROUP BY name",
            params + pred_params,
        ).fetchall()
        mods_by_name = {row["name"]: int(row["n"]) for row in mods}

        total_churn = sum(int(row["churn"]) for row in sums)
        result = []
        for row in sums:
            churn = int(row["churn"])
            result.append({
                "name": row["name"],
                "added": int(row["added"]),
                "removed": int(row["removed"]),
                "churn": churn,
                "modifications": mods_by_name.get(row["name"], 0),
                "ownership": churn / total_churn if total_churn else 0.0,
            })
        result.sort(key=lambda item: (-item["churn"], item["name"]))
        return result

    def children(self, path: str) -> list[dict]:
        """Immediate children of a directory with their full metrics."""
        pred, pred_params = _path_pred(path, "dir")
        candidates: set[str] = set()

        base, params = self._base_sql()
        rows = self.conn.execute(
            f"SELECT DISTINCT path FROM changes WHERE {base} AND {pred}",
            params + pred_params,
        ).fetchall()
        candidates.update(row["path"] for row in rows)

        # files present at the reference state but untouched within H
        rows = self.conn.execute(
            f"SELECT path FROM repo_paths WHERE repo_id = ? AND {pred}",
            [self.repo_id, *pred_params],
        ).fetchall()
        candidates.update(row["path"] for row in rows)

        prefix = path + "/" if path else ""
        children: dict[str, tuple[str, str]] = {}
        for candidate in candidates:
            relative = candidate[len(prefix):]
            if not relative:
                continue
            if "/" in relative:
                segment = relative.split("/", 1)[0]
                children[segment] = ("dir", prefix + segment)
            else:
                # a directory wins over a historical file with the same name
                children.setdefault(relative, ("file", candidate))

        commits = self.commit_count()
        result = []
        for name, (kind, full_path) in children.items():
            added, removed = self.totals(full_path, kind)
            mods = self.modifications(full_path, kind)
            churn = added + removed
            result.append({
                "name": name,
                "path": full_path,
                "kind": kind,
                "added": added,
                "removed": removed,
                "growth": added - removed,
                "churn": churn,
                "modifications": mods,
                "modification_frequency": mods / commits if commits else 0.0,
                "churn_rate": churn / commits if commits else 0.0,
            })
        result.sort(key=lambda item: (-item["churn"], item["name"]))
        return result

    def timeseries(self, path: str, kind: str) -> list[dict]:
        """Added/removed lines per time bucket over H for the object.

        Bucket size adapts to the analysed span: day (<= 90 days), week
        (<= 2 years) or calendar month.
        """
        base, params = self._base_sql()
        row = self.conn.execute(
            f"SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM commits WHERE {base}",
            params,
        ).fetchone()
        if row["lo"] is None:
            return []
        lo, hi = int(row["lo"]), int(row["hi"])

        pred, pred_params = _path_pred(path, kind)
        span = hi - lo
        buckets: dict[int, tuple[int, int]] = {}

        if span <= 730 * 86400:
            step = 86400 if span <= 90 * 86400 else 7 * 86400
            anchor = (lo // step) * step
            rows = self.conn.execute(
                f"SELECT CAST((ts - ?) / ? AS INTEGER) * ? + ? AS bucket, "
                f"       SUM(added) AS a, SUM(removed) AS r "
                f"FROM changes WHERE {base} AND {pred} GROUP BY bucket",
                [anchor, step, step, anchor, *params, *pred_params],
            ).fetchall()
            for r in rows:
                buckets[int(r["bucket"])] = (int(r["a"]), int(r["r"]))
            first, last = anchor, ((hi - anchor) // step) * step + anchor
            points = []
            t = first
            while t <= last:
                added, removed = buckets.get(t, (0, 0))
                points.append({"t": t, "added": added, "removed": removed})
                t += step
            return points

        rows = self.conn.execute(
            f"SELECT strftime('%Y-%m-01', ts, 'unixepoch') AS bucket, "
            f"       SUM(added) AS a, SUM(removed) AS r "
            f"FROM changes WHERE {base} AND {pred} "
            f"GROUP BY bucket ORDER BY bucket",
            params + pred_params,
        ).fetchall()
        points = []
        for r in rows:
            stamp = calendar.timegm(
                datetime.strptime(r["bucket"], "%Y-%m-%d")
                .replace(tzinfo=timezone.utc).timetuple()
            )
            points.append({"t": stamp, "added": int(r["a"]),
                           "removed": int(r["r"])})
        return points


# ---------------------------------------------------------------------------
# Metric-free helpers (object browser)
# ---------------------------------------------------------------------------

def path_children(conn, repo_id: int, path: str) -> list[dict]:
    """Immediate children of ``path`` across the repository (no metrics)."""
    pred, pred_params = _path_pred(path, "dir")
    rows = conn.execute(
        f"SELECT DISTINCT path FROM changes WHERE repo_id = ? AND {pred} "
        f"UNION SELECT path FROM repo_paths WHERE repo_id = ? AND {pred}",
        [repo_id, *pred_params, repo_id, *pred_params],
    ).fetchall()
    prefix = path + "/" if path else ""
    children: dict[str, str] = {}
    for row in rows:
        relative = row["path"][len(prefix):]
        if not relative:
            continue
        if "/" in relative:
            segment = relative.split("/", 1)[0]
            children[segment] = "dir"
        else:
            children.setdefault(relative, "file")
    return [
        {"name": name, "path": (prefix + name), "kind": kind}
        for name, kind in sorted(children.items(),
                                 key=lambda item: (item[1] != "dir", item[0]))
    ]
