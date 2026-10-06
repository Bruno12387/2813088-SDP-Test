"""Metric correctness tests against a hand-computed fixture repository.

Every expected number in this file is derived by hand from the commit script
in :mod:`tests.fixture` - see the module docstring there for the ledger.
"""
from __future__ import annotations

import zipfile

import pytest

from server import db, ingest, metrics
from tests import fixture as fx


@pytest.fixture()
def env(tmp_path):
    conn = db.connect(tmp_path / "rat.db")
    repo_dir = fx.build_fixture_repo(tmp_path / "fixture")
    repo_id = ingest.import_local(conn, "fixture", str(repo_dir), "local",
                                  repo_dir)
    yield conn, repo_id
    conn.close()


def mq(conn, repo_id, **kwargs) -> metrics.MetricQuery:
    return metrics.MetricQuery(conn, repo_id, metrics.Filters(**kwargs))


def approx(value):
    return pytest.approx(value, abs=1e-9)


# ---------------------------------------------------------------------------
# Repository / file / directory metrics
# ---------------------------------------------------------------------------

def test_repository_metrics_full_history(env):
    conn, repo_id = env
    m = mq(conn, repo_id).object_metrics("", "dir")
    assert m == {
        "commits": 12,
        "added": 24,
        "removed": 4,
        "growth": 20,
        "churn": 28,
        "modifications": 8,
        "modification_frequency": approx(8 / 12),
        "churn_rate": approx(28 / 12),
    }


@pytest.mark.parametrize("path,kind,added,removed,churn,mods", [
    ("foo.txt", "file", 5, 1, 6, 2),          # c1 +3, c2 +2/-1
    ("bar.txt", "file", 1, 0, 1, 1),          # c4 (c3 pure rename adds nothing)
    ("big.txt", "file", 10, 0, 10, 1),        # c7
    ("big2.txt", "file", 1, 1, 2, 1),         # c8 shows only the content delta
    (".mailmap", "file", 1, 0, 1, 1),         # c10
    ("mm.txt", "file", 1, 0, 1, 1),           # c11
    ("sub/x.txt", "file", 1, 1, 2, 2),        # c1 add, c6 delete
    ("deep", "dir", 4, 1, 5, 2),              # nest of deep/a/nested.txt
    ("deep/a", "dir", 4, 1, 5, 2),
    ("deep/a/nested.txt", "file", 4, 1, 5, 2),
    ("sub", "dir", 1, 1, 2, 2),
])
def test_file_and_directory_metrics(env, path, kind, added, removed, churn,
                                    mods):
    conn, repo_id = env
    m = mq(conn, repo_id).object_metrics(path, kind)
    assert (m["added"], m["removed"], m["growth"], m["churn"],
            m["modifications"]) == (added, removed, added - removed, churn,
                                    mods)
    assert m["modification_frequency"] == approx(mods / 12)
    assert m["churn_rate"] == approx(churn / 12)


def test_children_of_root(env):
    conn, repo_id = env
    children = {c["name"]: c for c in mq(conn, repo_id).children("")}
    assert set(children) == {"foo.txt", "bar.txt", "big.txt", "big2.txt",
                             "deep", "sub", ".mailmap", "mm.txt"}
    assert children["deep"]["kind"] == "dir"
    assert children["deep"]["churn"] == 5
    assert children["sub"]["kind"] == "dir"
    assert children["sub"]["churn"] == 2
    assert children["foo.txt"]["kind"] == "file"
    assert children["foo.txt"]["added"] == 5
    # binary files never surface (not measured by definition)
    assert "bin.dat" not in children and "bin2.dat" not in children


def test_children_of_nested_directory(env):
    conn, repo_id = env
    deep = mq(conn, repo_id).children("deep")
    assert [c["name"] for c in deep] == ["a"]
    assert deep[0]["kind"] == "dir"
    assert deep[0]["churn"] == 5
    inner = mq(conn, repo_id).children("deep/a")
    assert [c["name"] for c in inner] == ["nested.txt"]
    assert inner[0]["path"] == "deep/a/nested.txt"


# ---------------------------------------------------------------------------
# Commit set metrics
# ---------------------------------------------------------------------------

def test_commit_set_time_window(env):
    conn, repo_id = env
    q = mq(conn, repo_id, from_ts=fx.TS[1], to_ts=fx.TS[2])  # c2 only
    m = q.object_metrics("", "dir")
    assert m["commits"] == 1
    assert (m["added"], m["removed"], m["churn"]) == (4, 2, 6)
    assert m["modifications"] == 1
    assert m["modification_frequency"] == approx(1.0)
    assert m["churn_rate"] == approx(6.0)
    foo = q.object_metrics("foo.txt", "file")
    assert (foo["added"], foo["removed"]) == (2, 1)


def test_commit_set_manual_commit_list(env):
    conn, repo_id = env
    shas = [row["sha"] for row in conn.execute(
        "SELECT sha FROM commits WHERE repo_id = ? ORDER BY ts", (repo_id,)
    )]
    q = mq(conn, repo_id, shas=[shas[0], shas[10]])  # c1 + c11
    m = q.object_metrics("", "dir")
    assert m["commits"] == 2
    assert (m["added"], m["removed"], m["churn"]) == (7, 0, 7)
    assert m["modifications"] == 2
    assert m["churn_rate"] == approx(3.5)


def test_binary_commits_contribute_nothing(env):
    conn, repo_id = env
    q = mq(conn, repo_id, from_ts=fx.TS[4], to_ts=fx.TS[5])  # c5: binary add
    m = q.object_metrics("", "dir")
    assert m["commits"] == 1
    assert (m["added"], m["removed"], m["churn"]) == (0, 0, 0)
    assert m["modifications"] == 0
    assert m["modification_frequency"] == 0.0
    assert m["churn_rate"] == 0.0


def test_pure_rename_does_not_change_metrics(env):
    conn, repo_id = env
    q = mq(conn, repo_id, from_ts=fx.TS[2], to_ts=fx.TS[3])  # c3 only
    bar = q.object_metrics("bar.txt", "file")
    assert (bar["added"], bar["removed"], bar["churn"]) == (0, 0, 0)
    assert bar["modifications"] == 0


def test_rename_with_change_attributed_to_new_path(env):
    conn, repo_id = env
    q = mq(conn, repo_id, from_ts=fx.TS[7], to_ts=fx.TS[8])  # c8 only
    new = q.object_metrics("big2.txt", "file")
    assert (new["added"], new["removed"]) == (1, 1)
    old = q.object_metrics("big.txt", "file")
    assert (old["added"], old["removed"]) == (0, 0)


def test_deletion_recorded_as_removed_lines(env):
    conn, repo_id = env
    q = mq(conn, repo_id, from_ts=fx.TS[5], to_ts=fx.TS[6])  # c6 only
    deleted = q.object_metrics("sub/x.txt", "file")
    assert (deleted["added"], deleted["removed"]) == (0, 1)
    assert deleted["modifications"] == 1
    directory = q.object_metrics("sub", "dir")
    assert (directory["added"], directory["removed"]) == (0, 1)


def test_empty_commit_counts_but_has_no_changes(env):
    conn, repo_id = env
    q = mq(conn, repo_id, from_ts=fx.TS[11], to_ts=fx.TS[11] + 1)
    m = q.object_metrics("", "dir")
    assert m["commits"] == 1
    assert m["churn"] == 0
    assert m["modifications"] == 0


def test_unknown_object_and_empty_selection_edge_cases(env):
    conn, repo_id = env
    q = mq(conn, repo_id)
    ghost = q.object_metrics("does/not/exist.txt", "file")
    assert ghost["churn"] == 0 and ghost["modifications"] == 0
    empty = mq(conn, repo_id, shas=[])
    m = empty.object_metrics("", "dir")
    assert m["commits"] == 0
    assert m["modification_frequency"] == 0.0
    assert m["churn_rate"] == 0.0


# ---------------------------------------------------------------------------
# Author metrics
# ---------------------------------------------------------------------------

def test_mailmap_merges_authors_automatically(env):
    conn, repo_id = env
    authors = mq(conn, repo_id).authors("", "dir")
    by_name = {a["name"]: a for a in authors}
    assert set(by_name) == {
        f"{fx.TEST_NAME} <{fx.TEST_EMAIL}>",
        f"{fx.CANON_NAME} <{fx.CANON_EMAIL}>",
    }
    test = by_name[f"{fx.TEST_NAME} <{fx.TEST_EMAIL}>"]
    assert test["churn"] == 27
    assert test["modifications"] == 7
    assert test["ownership"] == approx(27 / 28)
    canon = by_name[f"{fx.CANON_NAME} <{fx.CANON_EMAIL}>"]
    assert canon["churn"] == 1
    assert canon["modifications"] == 1
    assert canon["ownership"] == approx(1 / 28)


def test_author_filter_restricts_commit_set(env):
    conn, repo_id = env
    canon_group = f"{fx.CANON_NAME} <{fx.CANON_EMAIL}>"
    q = mq(conn, repo_id, groups=[canon_group])
    m = q.object_metrics("", "dir")
    assert m["commits"] == 1
    assert (m["added"], m["removed"], m["churn"]) == (1, 0, 1)
    assert m["modifications"] == 1
    assert m["modification_frequency"] == approx(1.0)
    authors = q.authors("", "dir")
    assert len(authors) == 1
    assert authors[0]["name"] == canon_group
    assert authors[0]["ownership"] == approx(1.0)

    none = mq(conn, repo_id, groups=["nobody <nobody@example.com>"])
    assert none.object_metrics("", "dir")["commits"] == 0


def test_manual_author_merge(env):
    conn, repo_id = env
    identities = db.list_authors(conn, repo_id)
    assert len(identities) == 2
    updated = db.merge_authors(conn, repo_id, [a["id"] for a in identities],
                               "Merged Dev")
    assert updated == 2
    authors = mq(conn, repo_id).authors("", "dir")
    assert len(authors) == 1
    assert authors[0]["name"] == "Merged Dev"
    assert authors[0]["churn"] == 28
    assert authors[0]["modifications"] == 8
    assert authors[0]["ownership"] == approx(1.0)


def test_author_ownership_on_single_file(env):
    conn, repo_id = env
    authors = {a["name"]: a for a in
               mq(conn, repo_id).authors("mm.txt", "file")}
    canon = authors[f"{fx.CANON_NAME} <{fx.CANON_EMAIL}>"]
    assert canon["ownership"] == approx(1.0)
    assert canon["modifications"] == 1


# ---------------------------------------------------------------------------
# Time series
# ---------------------------------------------------------------------------

def test_timeseries_sums_match_totals(env):
    conn, repo_id = env
    points = mq(conn, repo_id).timeseries("", "dir")
    assert points
    assert sum(p["added"] for p in points) == 24
    assert sum(p["removed"] for p in points) == 4
    stamps = [p["t"] for p in points]
    assert stamps == sorted(stamps)


# ---------------------------------------------------------------------------
# Zip ingestion produces identical results
# ---------------------------------------------------------------------------

def test_zip_ingestion_matches_direct_import(tmp_path):
    fixture_dir = fx.build_fixture_repo(tmp_path / "fixture")
    zip_path = tmp_path / "fixture.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for item in sorted(fixture_dir.rglob("*")):
            archive.write(item, arcname=str(item.relative_to(fixture_dir)))

    conn = db.connect(tmp_path / "zip.db")
    try:
        repo_id = ingest.ingest_zip(conn, zip_path, tmp_path / "extract",
                                    ingest.Job(), name="zipfixture")
        m = mq(conn, repo_id).object_metrics("", "dir")
        assert m["commits"] == 12
        assert (m["added"], m["removed"], m["churn"]) == (24, 4, 28)
        assert m["modifications"] == 8
        authors = mq(conn, repo_id).authors("", "dir")
        assert len(authors) == 2  # mailmap still applied after extraction
    finally:
        conn.close()
