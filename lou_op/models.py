"""Data models: pydantic for anything serialized, dataclasses for runtime.

Task carries living-doc state (``status``) so a crashed job can resume at the
first unfinished task (Chief's pattern).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional

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


class Task(BaseModel):
    """A unit of work. ``success_criteria`` entries are shell commands."""

    name: str
    description: str = ""
    success_criteria: List[str] = Field(default_factory=list)
    lint: bool = False  # run the built-in Python lint validator too
    judge: bool = False  # add LLM-as-judge quality gate after each iteration
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
    git_remote: Optional[str] = None
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


@dataclass
class IterationContext:
    repo_path: Path
    task: Task
    prompt: str
    iteration: int
    progress: str
    on_line: Optional[Callable[[str], None]] = field(default=None, repr=False)


@dataclass
class IterationOutput:
    done: bool
    summary: str
    log: str


@dataclass
class IterationResult:
    iteration: int
    passed: bool
    done: bool
    commit: str
    validations: List[ValidationResult] = field(default_factory=list)
