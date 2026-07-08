"""Data models: pydantic for anything serialized, dataclasses for runtime.

Task carries living-doc state (``status``) so a crashed job can resume at the
first unfinished task (Chief's pattern).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    FAILED = "failed"


class ValidationStatus(str, Enum):
    """Outcome of one gate check.

    The FAIL/ERROR split is the whole point: FAIL means the gate ran and the
    code is wrong (feed it back to the model); ERROR means the gate itself
    could not execute (runner missing, timed out, no tests collected) — a
    broken harness, not a coding problem, so burning model turns on it is
    pointless.
    """

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"


class Task(BaseModel):
    """A unit of work. ``success_criteria`` entries are shell commands."""

    name: str
    description: str = ""
    success_criteria: List[str] = Field(default_factory=list)
    lint: bool = False  # run the built-in Python lint validator too
    judge: bool = False  # add LLM-as-judge quality gate after each iteration
    # Glob patterns (relative to repo root) the model must not change; the loop
    # snapshots matches at task start and restores them before every validation.
    protected_files: List[str] = Field(default_factory=list)
    # A task with no criteria, no lint, and no judge is unverifiable; loading
    # one fails unless this is set (see orchestrator.validate_tasks).
    allow_no_validators: bool = False
    max_iterations: int = 5
    depends_on: List[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING


class JobSpec(BaseModel):
    """Everything needed to launch a job."""

    project_name: str
    tasks: List[Task] = Field(default_factory=list)
    prd: str = ""
    backend: str = "mock"
    workspace_type: str = "git"
    runtime: str = ""  # "host" | "docker"; empty => Settings.runtime
    git_remote: Optional[str] = None
    project_path: Optional[str] = None  # work in this dir directly (no sub-repo)
    max_iterations_per_task: int = 5
    timeout_seconds: int = 7200


class JobState(BaseModel):
    """Mutable status of a running/finished job (persisted to metadata.json)."""

    job_id: str
    status: JobStatus = JobStatus.PENDING
    git_branch: str = ""
    project_name: str = ""
    current_task: Optional[str] = None
    completed_tasks: List[str] = Field(default_factory=list)
    commits: List[str] = Field(default_factory=list)
    error: Optional[str] = None


# --- runtime-only (not serialized) -----------------------------------------


@dataclass
class FileWrite:
    path: str
    content: str


@dataclass
class ValidationResult:
    name: str
    passed: bool
    output: str
    # PASS/FAIL/ERROR. Defaults derive from ``passed`` so legacy call sites and
    # tests that only pass a bool stay coherent; validators set it explicitly.
    status: ValidationStatus = ValidationStatus.FAIL

    def __post_init__(self) -> None:
        if self.passed:
            self.status = ValidationStatus.PASS
        elif self.status is ValidationStatus.PASS:
            self.status = ValidationStatus.FAIL

    @property
    def errored(self) -> bool:
        return self.status is ValidationStatus.ERROR


@dataclass
class IterationContext:
    repo_path: Path
    task: Task
    prompt: str
    iteration: int
    progress: str


@dataclass
class IterationOutput:
    done: bool
    summary: str
    log: str
    scratchpad: str = ""


@dataclass
class IterationResult:
    iteration: int
    passed: bool
    done: bool
    commit: str
    validations: List[ValidationResult] = field(default_factory=list)
    wrote_files: bool = True
    # Terminal reason the task stopped, set on the last result of a run:
    # "passed" | "done" | "max_iterations" | "no_progress" | "job_timeout"
    # | "env_not_ready" | "scope_empty". Empty on non-terminal iterations.
    stop_reason: str = ""
