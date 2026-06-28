from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from lou_op import api
from lou_op.config import Settings
from lou_op.orchestrator import JobManager


def _client(tmp_path: Path) -> TestClient:
    api._manager = JobManager(Settings(jobs_dir=tmp_path / "jobs"))
    return TestClient(api.app)


def test_generate_status_results_flow(tmp_path: Path):
    client = _client(tmp_path)
    resp = client.post(
        "/generate",
        json={
            "project_name": "demo",
            "backend": "mock",
            "tasks": [
                {
                    "name": "Calc",
                    "success_criteria": ["python -m pytest -q"],
                }
            ],
        },
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert resp.json()["git_branch"] == f"lou-op/job-{job_id}"

    deadline = time.time() + 30
    status = "pending"
    while time.time() < deadline:
        status = client.get(f"/status/{job_id}").json()["status"]
        if status in ("completed", "failed"):
            break
        time.sleep(0.2)
    assert status == "completed"

    results = client.get(f"/results/{job_id}").json()
    assert results["commit_log"]
    assert "Calc" in results["completed_tasks"]


def test_status_404(tmp_path: Path):
    client = _client(tmp_path)
    assert client.get("/status/nope").status_code == 404
