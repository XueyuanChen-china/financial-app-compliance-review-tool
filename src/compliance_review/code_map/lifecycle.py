from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from compliance_review.code_map.models import GraphifyInitResult
from compliance_review.repository.git import GitRepository


class GraphifyLifecycle:
    """Install-check and build the local Graphify map for one repository."""

    def __init__(
        self,
        command: Sequence[str] = ("graphify",),
        installer: Sequence[str] = ("uv", "tool", "install", "graphifyy"),
        timeout_seconds: float = 900.0,
    ) -> None:
        self.command = tuple(command)
        self.installer = tuple(installer)
        self.timeout_seconds = timeout_seconds

    def initialize(
        self,
        repo_path: Path,
        *,
        install_if_missing: bool = True,
        force: bool = False,
        code_only: bool = True,
    ) -> GraphifyInitResult:
        repo = repo_path.resolve()
        if not repo.is_dir():
            return GraphifyInitResult(
                repo_path=repo.as_posix(),
                status="unavailable",
                error_code="repository_not_found",
            )

        executable = self._find_executable()
        if executable is None and install_if_missing:
            install_error = self._install()
            if install_error is not None:
                return GraphifyInitResult(
                    repo_path=repo.as_posix(),
                    status="unavailable",
                    error_code=install_error,
                    message="Graphify CLI could not be installed automatically",
                )
            executable = self._find_executable()
        if executable is None:
            return GraphifyInitResult(
                repo_path=repo.as_posix(),
                status="unavailable",
                error_code="graphify_not_found",
                message="Install graphifyy or pass --install-graphify",
            )

        command = [*executable, "extract", "."]
        if code_only:
            command.append("--code-only")
        if force:
            command.append("--force")
        try:
            completed = subprocess.run(
                command,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return self._degraded(repo, command, "graphify_extract_timeout")
        except OSError:
            return self._degraded(repo, command, "graphify_extract_os_error")
        if completed.returncode != 0:
            return self._degraded(
                repo,
                command,
                "graphify_extract_failed",
                message=_tail(completed.stderr or completed.stdout),
            )

        graph_paths = [path.as_posix() for path in self.graph_paths(repo)]
        if not graph_paths:
            return self._degraded(
                repo,
                command,
                "graph_output_missing",
                message="Graphify completed without a recognized graphify-out or graph.json output",
            )
        self._write_index_state(repo)
        return GraphifyInitResult(
            repo_path=repo.as_posix(),
            status="initialized",
            graphify_command=executable[0],
            build_command=command,
            graph_paths=graph_paths,
            message="Graphify map is ready for query",
        )

    def _find_executable(self) -> tuple[str, ...] | None:
        executable = self.command[0] if self.command else ""
        if Path(executable).is_file() or shutil.which(executable):
            return self.command
        return None

    def _install(self) -> str | None:
        try:
            completed = subprocess.run(
                self.installer,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (FileNotFoundError, OSError):
            return "graphify_installer_not_found"
        except subprocess.TimeoutExpired:
            return "graphify_install_timeout"
        if completed.returncode != 0:
            return "graphify_install_failed"
        return None

    @staticmethod
    def graph_paths(repo: Path) -> list[Path]:
        candidates = (
            repo / "graphify-out" / "graph.json",
            repo / "graphify-out" / "manifest.json",
            repo / "graph.json",
        )
        return [path for path in candidates if path.is_file()]

    @staticmethod
    def index_is_fresh(repo: Path) -> bool:
        """Require an index to describe the exact current code state.

        A Graphify map is a navigation cache.  Once code changes, returning
        results from the old cache would be misleading, so callers must rebuild
        or fall back to bounded search/read tools.
        """
        state_path = repo / "graphify-out" / "index-state.json"
        if not state_path.is_file():
            return False
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        state_id = state.get("code_state_id")
        return isinstance(state_id, (str, type(None))) and state_id == GitRepository(
            repo
        ).code_state_id()

    @staticmethod
    def _write_index_state(repo: Path) -> None:
        target = repo / "graphify-out" / "index-state.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {"code_state_id": GitRepository(repo).code_state_id()},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _degraded(
        repo: Path,
        command: list[str],
        error_code: str,
        message: str | None = None,
    ) -> GraphifyInitResult:
        return GraphifyInitResult(
            repo_path=repo.as_posix(),
            status="degraded",
            build_command=command,
            error_code=error_code,
            message=message,
        )


def _tail(value: str, limit: int = 500) -> str:
    return value.strip()[-limit:]
