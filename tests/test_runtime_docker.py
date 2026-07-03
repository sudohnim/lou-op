"""Spec (P0.1): sandbox runtime — host (unchanged) | docker (locked down).

The docker runtime runs model-influenced commands in a per-job container:
--cap-drop ALL, --security-opt no-new-privileges, non-root user, repo
bind-mounted at /work, optional --network none. Argv construction is tested
without docker; live container tests skip when docker is unavailable.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from lou_op.runtime import DockerRuntime, HostRuntime, get_runtime


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    probe = subprocess.run(["docker", "info"], capture_output=True, timeout=20)
    return probe.returncode == 0


needs_docker = pytest.mark.skipif(
    not _docker_available(), reason="docker not available"
)


class TestFactory:
    def test_default_is_host(self) -> None:
        assert isinstance(get_runtime("host"), HostRuntime)

    def test_docker_selected(self) -> None:
        assert isinstance(get_runtime("docker"), DockerRuntime)

    def test_unknown_rejected(self) -> None:
        with pytest.raises(ValueError):
            get_runtime("kubernetes")


class TestHostRuntime:
    def test_shell_runs_scrubbed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Host runtime delegates to exec.run_shell — secrets stay scrubbed."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-leak-test")
        rt = HostRuntime()
        rt.setup("jobx", tmp_path)
        result = rt.shell('echo "k=$OPENROUTER_API_KEY"', tmp_path)
        rt.teardown()
        assert result.passed
        assert "sk-leak-test" not in result.stdout

    def test_shell_reports_failure(self, tmp_path: Path) -> None:
        assert not HostRuntime().shell("exit 3", tmp_path).passed


class TestDockerArgvHardening:
    """The security flags must be in the container create argv — verifiable
    with no docker daemon at all."""

    def test_create_argv_flags(self, tmp_path: Path) -> None:
        rt = DockerRuntime()
        argv = rt.create_argv("job1", tmp_path)
        joined = " ".join(argv)
        assert argv[0:2] == ["docker", "run"]
        assert "--cap-drop" in argv and "ALL" in argv
        assert "no-new-privileges" in joined
        assert "--user" in argv  # non-root
        uid_idx = argv.index("--user") + 1
        assert argv[uid_idx].split(":")[0] not in ("0", "root")
        assert f"{tmp_path.resolve()}:/work" in joined  # repo bind-mounted
        # default-deny: no egress unless explicitly opted in
        net_idx = argv.index("--network")
        assert argv[net_idx + 1] == "none"

    def test_network_optin_removes_none(self, tmp_path: Path) -> None:
        rt = DockerRuntime(network=True)
        argv = rt.create_argv("job1", tmp_path)
        assert "--network" not in argv

    def test_exec_argv_runs_in_work(self) -> None:
        rt = DockerRuntime()
        argv = rt.exec_argv("job1", "echo hi")
        assert argv[0:2] == ["docker", "exec"]
        assert "/work" in " ".join(argv)
        assert argv[-1] == "echo hi" and argv[-2] == "-c"


@needs_docker
class TestDockerLive:
    @pytest.fixture()
    def rt(self, tmp_path: Path):
        runtime = DockerRuntime()
        job_id = f"test-{uuid.uuid4().hex[:8]}"
        runtime.setup(job_id, tmp_path)
        yield runtime, tmp_path
        runtime.teardown()

    def test_shell_inside_container(self, rt) -> None:
        runtime, repo = rt
        result = runtime.shell("echo from-container", repo)
        assert result.passed
        assert "from-container" in result.stdout

    def test_repo_is_bind_mounted_rw(self, rt) -> None:
        runtime, repo = rt
        (repo / "host_file.txt").write_text("host wrote this")
        result = runtime.shell("cat host_file.txt", repo)
        assert "host wrote this" in result.stdout
        runtime.shell("echo container-wrote > container_file.txt", repo)
        assert (repo / "container_file.txt").read_text().strip() == "container-wrote"

    def test_runs_as_non_root(self, rt) -> None:
        runtime, repo = rt
        result = runtime.shell("id -u", repo)
        assert result.stdout.strip() != "0"

    def test_teardown_removes_container(self, tmp_path: Path) -> None:
        runtime = DockerRuntime()
        job_id = f"test-{uuid.uuid4().hex[:8]}"
        runtime.setup(job_id, tmp_path)
        runtime.teardown()
        probe = subprocess.run(
            ["docker", "inspect", runtime.container_name(job_id)],
            capture_output=True,
        )
        assert probe.returncode != 0  # gone
