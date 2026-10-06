"""End-to-end API tests: zip upload, error reporting, metrics, author merges,
multi-repository isolation and deletion."""
from __future__ import annotations

import io
import time
import zipfile

import pytest

from server import app as app_module
from tests import fixture as fx


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RAT_DATA_DIR", str(tmp_path / "data"))
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as client:
        yield client


def _zip_fixture(tmp_path, name: str = "fixture") -> io.BytesIO:
    fixture_dir = fx.build_fixture_repo(tmp_path / name)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for item in sorted(fixture_dir.rglob("*")):
            archive.write(item, arcname=str(item.relative_to(fixture_dir)))
    buffer.seek(0)
    return buffer


def _upload(client, tmp_path, name: str = "fixture") -> dict:
    buffer = _zip_fixture(tmp_path, name)
    response = client.post("/api/repos",
                           data={"file": (buffer, f"{name}.zip")},
                           content_type="multipart/form-data")
    assert response.status_code == 202
    return response.get_json()


def _wait_for_job(client, job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/jobs/{job_id}").get_json()
        if payload["state"] in ("done", "error"):
            return payload
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


def test_zip_upload_full_flow(client, tmp_path):
    job = _wait_for_job(client, _upload(client, tmp_path)["id"])
    assert job["state"] == "done", job
    assert job["progress"] == 1.0
    repo_id = job["repo_id"]

    repos = client.get("/api/repos").get_json()["repos"]
    assert len(repos) == 1
    assert repos[0]["commit_count"] == 12
    assert repos[0]["kind"] == "zip"

    metrics = client.get(f"/api/repos/{repo_id}/metrics").get_json()
    assert metrics["object"]["label"] == "repository"
    assert metrics["metrics"]["churn"] == 28
    assert metrics["metrics"]["modifications"] == 8
    assert metrics["metrics"]["commits"] == 12
    assert {c["name"] for c in metrics["children"]} >= {"deep", "sub",
                                                        "foo.txt"}

    window = client.get(
        f"/api/repos/{repo_id}/metrics",
        query_string={"path": "deep", "kind": "dir",
                      "from": fx.TS[1], "to": fx.TS[2]}).get_json()
    assert window["metrics"]["added"] == 2
    assert window["metrics"]["removed"] == 1

    tree = client.get(f"/api/repos/{repo_id}/tree").get_json()["children"]
    assert any(c["name"] == "deep" and c["kind"] == "dir" for c in tree)
    sub = client.get(f"/api/repos/{repo_id}/tree",
                     query_string={"path": "sub"}).get_json()["children"]
    assert [c["name"] for c in sub] == ["x.txt"]

    commits = client.get(f"/api/repos/{repo_id}/commits",
                         query_string={"limit": 5}).get_json()
    assert commits["total"] == 12 and len(commits["commits"]) == 5
    found = client.get(f"/api/repos/{repo_id}/commits",
                       query_string={"q": "c11"}).get_json()
    assert found["total"] == 1

    authors = client.get(f"/api/repos/{repo_id}/authors").get_json()["authors"]
    assert len(authors) == 2  # mailmap-merged identities
    merged = client.post(f"/api/repos/{repo_id}/authors/merge",
                         json={"author_ids": [a["id"] for a in authors],
                               "name": "Merged Dev"})
    assert merged.status_code == 200
    assert merged.get_json()["updated"] == 2
    after = client.get(f"/api/repos/{repo_id}/metrics").get_json()["authors"]
    assert len(after) == 1 and after[0]["name"] == "Merged Dev"
    assert after[0]["ownership"] == pytest.approx(1.0)

    assert client.delete(f"/api/repos/{repo_id}").status_code == 200
    assert client.get("/api/repos").get_json()["repos"] == []


def test_invalid_clone_url_reports_error(client):
    response = client.post("/api/repos", json={"url": "not-a-url"})
    assert response.status_code == 202
    job = _wait_for_job(client, response.get_json()["id"])
    assert job["state"] == "error"
    assert "URL" in job["error"]


def test_bad_zip_reports_error(client):
    response = client.post("/api/repos",
                           data={"file": (io.BytesIO(b"hello"), "b.zip")},
                           content_type="multipart/form-data")
    assert response.status_code == 202
    job = _wait_for_job(client, response.get_json()["id"])
    assert job["state"] == "error"
    assert "zip" in job["error"].lower()


def test_validation_errors(client, tmp_path):
    job = _wait_for_job(client, _upload(client, tmp_path)["id"])
    repo_id = job["repo_id"]

    assert client.get(f"/api/repos/{repo_id}/metrics",
                      query_string={"kind": "bogus"}).status_code == 400
    assert client.get(f"/api/repos/{repo_id}/metrics",
                      query_string={"shas": "zzz"}).status_code == 400
    assert client.get(f"/api/repos/{repo_id}/metrics",
                      query_string={"authors": "not json"}).status_code == 400
    assert client.get("/api/repos/999/metrics").status_code == 404
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.post("/api/repos", json={}).status_code == 400


def test_multiple_repositories_are_isolated(client, tmp_path):
    for name in ("one", "two"):
        job = _wait_for_job(client, _upload(client, tmp_path, name)["id"])
        assert job["state"] == "done", job
    repos = client.get("/api/repos").get_json()["repos"]
    assert len(repos) == 2
    assert {r["name"] for r in repos} == {"one", "two"}
    for repo in repos:
        metrics = client.get(f"/api/repos/{repo['id']}/metrics").get_json()
        assert metrics["metrics"]["churn"] == 28
        assert metrics["metrics"]["commits"] == 12
