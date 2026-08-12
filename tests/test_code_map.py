import sys
from pathlib import Path

from compliance_review.code_map import (
    CodeMapQuery,
    GraphifyCodeMapProvider,
    GraphifyLifecycle,
)


def test_graphify_query_is_compact_and_bounded(tmp_path: Path) -> None:
    fake_graphify = tmp_path / "fake_graphify.py"
    fake_graphify.write_text(
        """import sys
print('Traversal: BFS | Start: [\"C.delete\"] | 2 nodes found')
print('')
print('NODE C.delete [src=account.py loc=L82 community=1]')
print('NODE S.delete [src=service.py loc=L114-L148 community=1]')
print('EDGE C.delete --calls [EXTRACTED]--> S.delete at=account.py:L90')
""",
        encoding="utf-8",
    )
    result = GraphifyCodeMapProvider(
        tmp_path,
        command=(sys.executable, str(fake_graphify)),
        require_index=False,
    ).query(CodeMapQuery(query="account deletion workflow", max_candidates=1))

    assert result.status == "available"
    assert result.truncated is True
    assert len(result.candidates) == 1
    assert result.candidates[0].symbol == "C.delete"
    assert result.candidates[0].start_line == 82
    assert result.relations == []


def test_graphify_missing_is_degraded_without_exception(tmp_path: Path) -> None:
    result = GraphifyCodeMapProvider(tmp_path, command=("missing-graphify",)).query(
        CodeMapQuery(query="account deletion")
    )

    assert result.status == "unavailable"
    assert result.error_code == "graphify_not_found"


def test_graphify_unparseable_output_is_degraded(tmp_path: Path) -> None:
    fake_graphify = tmp_path / "fake_graphify.py"
    fake_graphify.write_text("print('unexpected graph output')\n", encoding="utf-8")

    result = GraphifyCodeMapProvider(
        tmp_path, command=(sys.executable, str(fake_graphify)), require_index=False
    ).query(CodeMapQuery(query="account deletion"))

    assert result.status == "degraded"
    assert result.error_code == "graphify_output_unparseable"


def test_graphify_nonexistent_repository_is_unavailable(tmp_path: Path) -> None:
    result = GraphifyCodeMapProvider(tmp_path / "missing", command=("graphify",)).query(
        CodeMapQuery(query="account deletion")
    )

    assert result.status == "unavailable"
    assert result.error_code == "repository_not_found"


def test_graphify_query_requires_initialized_map(tmp_path: Path) -> None:
    result = GraphifyCodeMapProvider(
        tmp_path, command=(sys.executable, "-c", "print('unused')")
    ).query(CodeMapQuery(query="account deletion"))

    assert result.status == "unavailable"
    assert result.error_code == "graph_not_initialized"


def test_graphify_lifecycle_builds_code_only_map_with_fake_cli(tmp_path: Path) -> None:
    fake_graphify = tmp_path / "fake_graphify.py"
    fake_graphify.write_text(
        """
from pathlib import Path
import sys
Path('graphify-out').mkdir()
Path('graphify-out/graph.json').write_text('{}')
print('built', ' '.join(sys.argv[1:]))
""",
        encoding="utf-8",
    )
    result = GraphifyLifecycle(
        command=(sys.executable, str(fake_graphify)),
        installer=("missing-installer",),
    ).initialize(tmp_path, install_if_missing=False)

    assert result.status == "initialized"
    assert result.graph_paths == [(tmp_path / "graphify-out" / "graph.json").as_posix()]
    assert result.build_command[-1] == "--code-only"
