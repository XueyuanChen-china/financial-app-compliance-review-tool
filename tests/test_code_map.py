import sys
from pathlib import Path

from compliance_review.code_map import (
    CodeMapExplain,
    CodeMapImpact,
    CodeMapNeighbors,
    CodeMapPath,
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


def test_graphify_query_rejects_stale_index_state(tmp_path: Path) -> None:
    graph_dir = tmp_path / "graphify-out"
    graph_dir.mkdir()
    (graph_dir / "graph.json").write_text("{}", encoding="utf-8")
    (graph_dir / "index-state.json").write_text(
        '{"code_state_id":"stale"}', encoding="utf-8"
    )
    provider = GraphifyCodeMapProvider(tmp_path, command=("echo",))

    result = provider.query(CodeMapQuery(query="account deletion"))

    assert result.status == "unavailable"
    assert result.error_code == "graph_index_stale"


def test_graphify_path_is_normalized_and_bounded(tmp_path: Path) -> None:
    fake_graphify = tmp_path / "fake_graphify.py"
    fake_graphify.write_text(
        """
import sys
print('C.delete → S.delete → R.delete')
""",
        encoding="utf-8",
    )
    result = GraphifyCodeMapProvider(
        tmp_path,
        command=(sys.executable, str(fake_graphify)),
        require_index=False,
    ).path(CodeMapPath(source="C.delete", target="R.delete", max_hops=1))

    assert result.status == "available"
    assert result.truncated is True
    assert [node.symbol for node in result.nodes] == ["C.delete", "S.delete"]
    assert len(result.relations) == 1


def test_graphify_explain_callers_callees_and_impact_are_bounded(tmp_path: Path) -> None:
    fake_graphify = tmp_path / "fake_graphify.py"
    fake_graphify.write_text(
        """
import sys
command = sys.argv[1]
if command == 'explain':
    print('Node: AccountService.delete')
    print('Source: service.py L42')
    print('Connections (2):')
    print('  --> UserRepository.delete [calls] [EXTRACTED]')
    print('  <-- AccountController.delete [calls] [EXTRACTED]')
elif command == 'affected':
    print('NODE AccountController.delete [src=controller.py loc=L20]')
    print('NODE AccountRoute [src=routes.py loc=L8]')
    print('EDGE AccountRoute --calls [EXTRACTED]--> AccountController.delete at=routes.py:L8')
""",
        encoding="utf-8",
    )
    provider = GraphifyCodeMapProvider(
        tmp_path,
        command=(sys.executable, str(fake_graphify)),
        require_index=False,
    )

    explained = provider.explain(CodeMapExplain(symbol="AccountService.delete"))
    assert explained.status == "available"
    assert explained.node is not None
    assert explained.node.start_line == 42
    assert len(explained.relations) == 2

    callers = provider.neighbors(
        CodeMapNeighbors(symbol="AccountService.delete", direction="callers")
    )
    callees = provider.neighbors(
        CodeMapNeighbors(symbol="AccountService.delete", direction="callees")
    )
    assert [node.symbol for node in callers.nodes] == ["AccountController.delete"]
    assert [node.symbol for node in callees.nodes] == ["UserRepository.delete"]

    impact = provider.impact(CodeMapImpact(symbol="AccountService.delete"))
    assert impact.status == "available"
    assert [node.symbol for node in impact.nodes] == ["AccountController.delete", "AccountRoute"]


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
