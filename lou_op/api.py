"""FastAPI surface matching PRD §1 (local now; Modal wrapper later)."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .git_ops import log
from .logutil import configure_logging
from .models import JobSpec, JobState, Task
from .orchestrator import JobManager

configure_logging()
app = FastAPI(title="lou-op", version="0.1.0")
_manager = JobManager()


class GenerateRequest(BaseModel):
    project_name: str
    tasks: List[Task] = []
    prd: str = ""
    backend: str = "mock"
    workspace_type: str = "git"
    git_remote: Optional[str] = None
    max_iterations_per_task: int = 5
    timeout_seconds: int = 7200


class GenerateResponse(BaseModel):
    job_id: str
    status_url: str
    git_branch: str


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest) -> GenerateResponse:
    spec = JobSpec(**req.model_dump())
    state = _manager.create(spec, run_async=True)
    return GenerateResponse(
        job_id=state.job_id,
        status_url=f"/status/{state.job_id}",
        git_branch=state.git_branch,
    )


@app.get("/status/{job_id}", response_model=JobState)
def status(job_id: str) -> JobState:
    state = _manager.get_status(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="job not found")
    return state


@app.get("/results/{job_id}")
def results(job_id: str) -> dict:
    state = _manager.get_results(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="job not found")
    repo_path = _manager.settings.jobs_dir / job_id
    commit_log = log(repo_path, count=100) if repo_path.exists() else []
    return {
        "job_id": state.job_id,
        "status": state.status,
        "git_branch": state.git_branch,
        "commit_log": commit_log,
        "completed_tasks": state.completed_tasks,
        "error": state.error,
    }


@app.get("/logs/{job_id}")
async def stream_logs(job_id: str) -> StreamingResponse:
    """SSE stream of agent output lines while the job runs."""
    q = _manager.get_log_queue(job_id)
    if q is None:
        raise HTTPException(status_code=404, detail="job not found")

    async def _generate() -> AsyncIterator[str]:
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, q.get)
            if line is None:
                break
            yield f"data: {line}\n\n"

    return StreamingResponse(_generate(), media_type="text/event-stream")
