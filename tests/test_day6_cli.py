import pytest

from compliance_review.cli import ci_exit_code


@pytest.mark.parametrize("status", ["pass", "warn"])
def test_ci_pass_and_warn_exit_zero(status: str) -> None:
    assert ci_exit_code(status) == 0


def test_ci_block_exit_one() -> None:
    assert ci_exit_code("block") == 1


def test_unknown_ci_status_is_runtime_error() -> None:
    with pytest.raises(ValueError, match="unknown CI status"):
        ci_exit_code("unknown")
