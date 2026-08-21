from __future__ import annotations

from itertools import islice
from pathlib import Path

SENSITIVE_NAMES = {
    ".env",
    "google-services.json",
    "service-account.json",
}
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".jks", ".keystore")
MAX_READABLE_FILE_BYTES = 10_000_000


class SandboxViolation(ValueError):
    """Raised when a read attempts to leave the configured repository root."""


class RepositorySandbox:
    """Resolve read paths inside one repository root, including symlink checks."""

    def __init__(self, root: Path) -> None:
        if not root.exists() or not root.is_dir():
            raise SandboxViolation(f"repository root is not a directory: {root}")
        self.root = root.resolve()

    def resolve(self, relative_path: str | Path) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise SandboxViolation(f"path leaves repository root: {relative_path}") from exc
        return resolved

    def read_bytes(
        self, relative_path: str | Path, max_bytes: int = MAX_READABLE_FILE_BYTES
    ) -> bytes:
        path = self.resolve(relative_path)
        if is_sensitive_path(path.relative_to(self.root).as_posix()):
            raise SandboxViolation(f"sensitive file is not readable: {relative_path}")
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.stat().st_size > max_bytes:
            raise SandboxViolation(f"file exceeds read limit: {relative_path}")
        return path.read_bytes()

    def read_text(
        self, relative_path: str | Path, max_bytes: int = MAX_READABLE_FILE_BYTES
    ) -> str:
        return self.read_bytes(relative_path, max_bytes=max_bytes).decode("utf-8", errors="replace")

    def read_text_range(
        self,
        relative_path: str | Path,
        *,
        start_line: int,
        line_count: int,
        max_bytes: int = MAX_READABLE_FILE_BYTES,
    ) -> str:
        """Read only a bounded line range without materializing the whole file."""
        if start_line < 1 or line_count < 1:
            raise ValueError("start_line and line_count must be positive")
        path = self.resolve(relative_path)
        relative = path.relative_to(self.root).as_posix()
        if is_sensitive_path(relative):
            raise SandboxViolation(f"sensitive file is not readable: {relative_path}")
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.stat().st_size > max_bytes:
            raise SandboxViolation(f"file exceeds read limit: {relative_path}")
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return "".join(islice(handle, start_line - 1, start_line - 1 + line_count))

    def count_lines(
        self,
        relative_path: str | Path,
        max_bytes: int = MAX_READABLE_FILE_BYTES,
    ) -> int:
        """Count lines through a bounded stream, without loading file contents."""
        path = self.resolve(relative_path)
        relative = path.relative_to(self.root).as_posix()
        if is_sensitive_path(relative):
            raise SandboxViolation(f"sensitive file is not readable: {relative_path}")
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.stat().st_size > max_bytes:
            raise SandboxViolation(f"file exceeds read limit: {relative_path}")
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)

    def list_files(self, pattern: str = "*", limit: int = 500) -> list[str]:
        if limit < 1:
            raise ValueError("limit must be positive")
        pattern_path = Path(pattern)
        if pattern_path.is_absolute() or ".." in pattern_path.parts:
            raise SandboxViolation(f"file pattern leaves repository root: {pattern}")
        matches = []
        for path in sorted(self.root.glob(pattern)):
            if path.is_file() and not is_sensitive_path(path.relative_to(self.root).as_posix()):
                self.resolve(path)
                matches.append(path.relative_to(self.root).as_posix())
                if len(matches) >= limit:
                    break
        return matches


def is_sensitive_path(relative_path: str) -> bool:
    """Keep common secrets and signing material out of reviewer-facing tools."""
    path = Path(relative_path)
    name = path.name.lower()
    if name in SENSITIVE_NAMES:
        return True
    if name.startswith(".env") or name.startswith("private_key"):
        return True
    return name.endswith(SENSITIVE_SUFFIXES) or "service-account" in name
