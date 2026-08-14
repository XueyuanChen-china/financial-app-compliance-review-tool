from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")


class ReliabilityError(RuntimeError):
    """An execution error with deterministic retry semantics."""

    error_code = "runtime_error"
    retryable = False


class RetryableWorkerError(ReliabilityError):
    retryable = True


class NonRetryableWorkerError(ReliabilityError):
    retryable = False


class ModelTimeoutError(RetryableWorkerError):
    error_code = "model_timeout"


class ToolTimeoutError(RetryableWorkerError):
    error_code = "tool_timeout"


class WorkItemTimeoutError(RetryableWorkerError):
    error_code = "work_item_timeout"


class PathEscapeError(NonRetryableWorkerError):
    error_code = "path_escape"


class ContractError(NonRetryableWorkerError):
    error_code = "invalid_work_item_contract"


class ClassifiedWorkerError(ReliabilityError):
    """Carry a deterministic error code from a bounded tool/runtime boundary."""

    def __init__(self, error_code: str, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


@dataclass(frozen=True)
class ErrorClassification:
    error_code: str
    retryable: bool


def classify_error(error: BaseException | str) -> ErrorClassification:
    """Classify known failures conservatively; security and contract errors never retry."""
    if isinstance(error, ReliabilityError):
        return ErrorClassification(error.error_code, error.retryable)
    if isinstance(error, FileNotFoundError):
        return ErrorClassification("missing_path", False)
    if isinstance(error, PermissionError):
        return ErrorClassification("permission_denied", False)
    message = str(error).lower()
    non_retryable_markers = (
        "path leaves",
        "path escape",
        "outside work item roots",
        "sensitive file",
        "security policy",
        "invalid work item",
        "unknown control",
        "unknown surface",
        "contract",
        "token budget",
        "context_budget_exhausted",
        "max_tool_rounds",
        "max_tool_calls",
        "max_files_read",
        "file exceeds read limit",
    )
    if any(marker in message for marker in non_retryable_markers):
        if "path " in message or "outside work item roots" in message:
            return ErrorClassification("path_escape", False)
        return ErrorClassification("non_retryable_error", False)
    if isinstance(error, (TimeoutError, FutureTimeoutError)):
        return ErrorClassification("timeout", True)
    if "invalid structured review result" in message or "returned neither" in message:
        return ErrorClassification("invalid_model_response", True)
    if isinstance(error, (OSError, ConnectionError)):
        return ErrorClassification("transient_provider_error", True)
    return ErrorClassification("worker_error", True)


def call_with_timeout(
    function: Callable[[], T], timeout_seconds: float, timeout_error: ReliabilityError
) -> T:
    """Run a potentially blocking call with a bounded wait.

    The worker thread is not force-killed. This is deliberate: Python cannot safely
    terminate arbitrary provider/tool threads. The caller still receives a terminal
    timeout state and can schedule a fresh attempt.
    """
    if timeout_seconds <= 0:
        raise timeout_error
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(function)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError as exc:
        future.cancel()
        raise timeout_error from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
