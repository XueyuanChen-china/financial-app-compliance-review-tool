import json
import os
from pathlib import Path
from typing import Annotated, Optional

import typer
from dotenv import load_dotenv
from pydantic import TypeAdapter, ValidationError

from compliance_review import __version__
from compliance_review.code_map import (
    CodeMapQuery,
    GraphifyCodeMapProvider,
    GraphifyLifecycle,
)
from compliance_review.code_map.provider import command_from_string
from compliance_review.collectors import (
    ApiDocumentCollector,
    DependencyCollector,
    ManifestCollector,
)
from compliance_review.config.loader import ConfigLoadError, load_controls, load_profile
from compliance_review.domain.models import ControlSet, Snapshot, Surface
from compliance_review.repository import GitRepository, ReadOnlyRepositoryTools, RepositorySandbox
from compliance_review.review import (
    OpenAICompatibleProvider,
    ReviewManifestBuilder,
)
from compliance_review.review.diff_review import DiffReviewService
from compliance_review.review.full_review import FullReviewService
from compliance_review.review.langgraph_runtime import LangGraphReviewRuntime
from compliance_review.review.models import ReviewManifest
from compliance_review.review.redaction import redact_sensitive_text
from compliance_review.setup.models import WorkspaceMaterial, WorkspaceRepository
from compliance_review.setup.service import ReviewSetupError, ReviewSetupService

load_dotenv()

DEFAULT_MODEL = os.environ.get("COMPLIANCE_REVIEW_MODEL", "gpt-4o-mini")
DEFAULT_BASE_URL = os.environ.get(
    "COMPLIANCE_REVIEW_BASE_URL",
    "https://api.openai.com/v1/chat/completions",
)

app = typer.Typer(
    name="compliance-review",
    help="Evidence-aware compliance review for financial applications.",
    no_args_is_help=True,
)


def ci_exit_code(ci_status: str) -> int:
    """Map the final CoverageGate status to the stable CI contract."""
    if ci_status == "block":
        return 1
    if ci_status in {"pass", "warn"}:
        return 0
    raise ValueError(f"unknown CI status: {ci_status}")


@app.command()
def version() -> None:
    """Print the installed project version."""
    typer.echo(__version__)


@app.command()
def review(
    target: Annotated[str, typer.Argument(help="Repository or release package to review")],
    mode: Annotated[
        str,
        typer.Option(help="Review mode: full or diff"),
    ] = "full",
) -> None:
    """Reserve the public review entry point while the engine is implemented."""
    raise typer.BadParameter(
        f"Review engine is not implemented yet (target={target!r}, mode={mode!r})."
    )


@app.command()
def validate(
    profile: Annotated[Path, typer.Option(help="Applicability profile YAML")],
    controls: Annotated[Path, typer.Option(help="Control set YAML")],
) -> None:
    """Validate the Day 1 profile and control contracts."""
    try:
        loaded_profile = load_profile(profile)
        loaded_controls = load_controls(controls)
    except ConfigLoadError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"valid: profile={loaded_profile.app_name} controls={len(loaded_controls.controls)}")


@app.command("code-map-query")
def code_map_query(
    repo: Annotated[Path, typer.Option(help="Repository to query")],
    query: Annotated[str, typer.Option(help="Business or Control-oriented query")],
    surface: Annotated[Optional[str], typer.Option(help="Optional evidence surface")] = None,
    graphify_command: Annotated[
        str, typer.Option(help="Graphify executable command; no shell is used")
    ] = "graphify",
) -> None:
    """Query Graphify through the stable local Code Map provider boundary."""
    try:
        command = command_from_string(graphify_command)
        request = CodeMapQuery.model_validate({"query": query, "surface": surface})
        result = GraphifyCodeMapProvider(repo, command=command).query(request)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(result.model_dump_json(indent=2))


@app.command("init")
def init_graphify(
    workspace: Annotated[
        Optional[Path], typer.Argument(help="Workspace root for Phase 1 setup")
    ] = None,
    repo: Annotated[Optional[Path], typer.Option(help="One code repository to index")] = None,
    profile: Annotated[
        Optional[Path], typer.Option(help="Applicability profile; indexes code surfaces")
    ] = None,
    install_graphify: Annotated[
        bool, typer.Option(help="Install graphifyy with uv when graphify is missing")
    ] = True,
    force: Annotated[bool, typer.Option(help="Force a full graph rebuild")] = False,
    repository: Annotated[
        Optional[list[str]],
        typer.Option(
            "--repository",
            help="Workspace repository, repeat as repo_id=path; enables setup mode",
        ),
    ] = None,
    material: Annotated[
        Optional[list[Path]],
        typer.Option(
            "--material",
            help="Compliance material path to register in workspace setup",
        ),
    ] = None,
    repository_surface: Annotated[
        Optional[list[str]],
        typer.Option(
            "--repository-surface",
            help="Declared surface, repeat as repo_id=surface",
        ),
    ] = None,
    profile_model: Annotated[
        Optional[str],
        typer.Option(help="Optional model for workspace Profile Agent inference"),
    ] = None,
    profile_base_url: Annotated[
        str,
        typer.Option(help="OpenAI-compatible Profile Agent endpoint"),
    ] = "https://api.openai.com/v1/chat/completions",
) -> None:
    """Initialize a Workspace, or use legacy Graphify repository initialization."""
    repository = repository or []
    material = material or []
    repository_surface = repository_surface or []
    if workspace is not None or repository or material or repository_surface or profile_model:
        if workspace is None:
            raise typer.BadParameter("workspace path is required in setup mode")
        try:
            repositories = [_parse_workspace_repository(value) for value in repository]
            surface_overrides = dict(
                _parse_repository_surface(value) for value in repository_surface
            )
            repositories = [
                item.model_copy(update={"declared_surface": surface_overrides[item.repo_id]})
                if item.repo_id in surface_overrides
                else item
                for item in repositories
            ]
            materials = [
                WorkspaceMaterial(path=path.expanduser().resolve().as_posix()) for path in material
            ]
            provider = (
                OpenAICompatibleProvider(profile_model, base_url=profile_base_url)
                if profile_model
                else None
            )
            result = ReviewSetupService(workspace, profile_provider=provider).initialize(
                repositories, materials
            )
        except (OSError, ValueError) as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo(
            json.dumps(
                {
                    "workspace": result.workspace.model_dump(mode="json"),
                    "repositories": len(result.inventories),
                    "facts": len(result.app_facts.facts),
                    "profile_status": result.profile.status,
                    "confirmation_status": result.confirmation.status,
                    "required_fields": result.confirmation.required_fields,
                },
                indent=2,
            )
        )
        return
    if (repo is None) == (profile is None):
        raise typer.BadParameter("provide exactly one of --repo or --profile")
    lifecycle = GraphifyLifecycle()
    targets: list[Path]
    if repo is not None:
        targets = [repo]
    else:
        try:
            loaded_profile = load_profile(profile or Path())
        except ConfigLoadError as exc:
            raise typer.BadParameter(str(exc)) from exc
        profile_root = (profile or Path()).parent
        targets = [
            (profile_root / root).resolve() if not Path(root).is_absolute() else Path(root)
            for surface, root in loaded_profile.roots.items()
            if surface in {"frontend_h5", "android_native", "backend_code"}
        ]
    results = [
        lifecycle.initialize(
            target,
            install_if_missing=install_graphify,
            force=force,
        )
        for target in targets
    ]
    typer.echo(json.dumps([result.model_dump() for result in results], indent=2))


def _parse_workspace_repository(value: str) -> WorkspaceRepository:
    if "=" in value:
        repo_id, raw_path = value.split("=", 1)
        if not repo_id or not raw_path:
            raise ValueError("workspace repository must use repo_id=path")
    else:
        raw_path = value
        repo_id = Path(raw_path).expanduser().name
    return WorkspaceRepository(
        repo_id=repo_id,
        path=Path(raw_path).expanduser().resolve().as_posix(),
    )


def _parse_repository_surface(value: str) -> tuple[str, Surface]:
    if "=" not in value:
        raise ValueError("repository surface must use repo_id=surface")
    repo_id, raw_surface = value.split("=", 1)
    if not repo_id or not raw_surface:
        raise ValueError("repository surface must use repo_id=surface")
    return repo_id, TypeAdapter(Surface).validate_python(raw_surface)


@app.command("confirm-profile")
def confirm_profile(
    workspace: Annotated[Path, typer.Argument(help="Workspace root")],
    value: Annotated[
        Optional[list[str]],
        typer.Option("--value", help="Profile value, repeat as field=json-or-text"),
    ] = None,
    repository_surface: Annotated[
        Optional[list[str]],
        typer.Option("--repository-surface", help="Resolve repo surface as repo_id=surface"),
    ] = None,
) -> None:
    """Confirm profile fields and repository surface conflicts after re-validation."""
    values: dict[str, object] = {}
    for item in value or []:
        if "=" not in item:
            raise typer.BadParameter("--value must use field=json-or-text")
        field, raw = item.split("=", 1)
        try:
            values[field] = json.loads(raw)
        except json.JSONDecodeError:
            values[field] = raw
    try:
        surfaces: dict[str, str] = dict(
            _parse_repository_surface(item) for item in (repository_surface or [])
        )
        result = ReviewSetupService(workspace).confirm_profile(values, surfaces)
    except (OSError, ValueError, ValidationError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(result.model_dump_json(indent=2))


@app.command("compile-rules")
def compile_rules(
    workspace: Annotated[Path, typer.Argument(help="Workspace root")],
    source: Annotated[
        Optional[list[Path]],
        typer.Option("--source", help="Policy source file or directory; repeatable"),
    ] = None,
    source_family: Annotated[
        Optional[list[str]],
        typer.Option(
            "--source-family",
            help="Source family mapping as path=country_regulator/google_play/other",
        ),
    ] = None,
    model: Annotated[str, typer.Option(help="OpenAI-compatible model name")] = DEFAULT_MODEL,
    base_url: Annotated[
        str,
        typer.Option(help="OpenAI-compatible chat completions endpoint"),
    ] = DEFAULT_BASE_URL,
) -> None:
    """Compile source materials into obligations and validated controls."""
    from compliance_review.compilation.service import (
        Phase2CompilationError,
        Phase2CompilationService,
    )

    try:
        paths = [item.expanduser().resolve() for item in (source or [])]
        families: dict[str, str] = {}
        for item in source_family or []:
            if "=" not in item:
                raise ValueError("--source-family must use path=family")
            raw_path, family = item.split("=", 1)
            path = Path(raw_path).expanduser().resolve()
            if not family:
                raise ValueError("source family cannot be empty")
            families[path.as_posix()] = family
            if path not in paths:
                paths.append(path)
        if not paths:
            workspace_data = json.loads(
                (workspace.expanduser().resolve() / "workspace.json").read_text(encoding="utf-8")
            )
            paths = [Path(item["path"]) for item in workspace_data.get("materials", [])]
        if not paths:
            raise ValueError("provide --source or initialize workspace materials first")
        result = Phase2CompilationService(
            workspace,
            OpenAICompatibleProvider(model, base_url=base_url),
        ).compile(paths, source_families=families)
    except (OSError, ValueError, Phase2CompilationError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        json.dumps(
            {
                "sources": len(result.source_registry.sources),
                "obligations": len(result.obligations.obligations),
                "controls": len(result.controls_draft.controls),
                "validated": result.control_validation.valid,
                "artifacts": [
                    "setup/sources.json",
                    "setup/obligation_extraction_batches.json",
                    "setup/obligations.json",
                    "setup/controls_draft.json",
                    "setup/control_validation.json",
                    "setup/controls.json",
                ],
            },
            indent=2,
        )
    )


@app.command("prepare-review")
def prepare_review(
    workspace: Annotated[Path, typer.Argument(help="Workspace root")],
    run_id: Annotated[
        Optional[str], typer.Option(help="Stable run ID; generated when omitted")
    ] = None,
    max_concurrency: Annotated[int, typer.Option(help="Maximum parallel Reviewer work items")] = 3,
) -> None:
    """Compile confirmed setup state into a Runtime-ready review handoff."""
    try:
        result = ReviewSetupService(workspace).compile(
            run_id=run_id,
            max_concurrency=max_concurrency,
        )
    except (OSError, ValueError, ReviewSetupError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        json.dumps(
            {
                "run_id": result.run_id,
                "work_items": len(result.work_items),
                "coverage_units": len(result.coverage.units if result.coverage else []),
                "excluded_controls": (
                    result.coverage.excluded_control_ids if result.coverage else []
                ),
                "unknown_controls": (
                    result.coverage.unknown_control_ids if result.coverage else []
                ),
                "missing_surfaces": (result.coverage.missing_surfaces if result.coverage else []),
                "manifest": f"runs/{result.run_id}/manifest.json",
            },
            indent=2,
        )
    )


@app.command("repository-info")
def repository_info(
    repo: Annotated[Path, typer.Option(help="Repository to inspect")],
) -> None:
    """Print bounded, read-only repository and Git metadata."""
    try:
        sandbox = RepositorySandbox(repo)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    metadata = GitRepository(sandbox.root).metadata()
    typer.echo(json.dumps(metadata.__dict__, indent=2, sort_keys=True))


@app.command("search-code")
def search_code(
    repo: Annotated[Path, typer.Option(help="Repository to search")],
    query: Annotated[str, typer.Option(help="Exact text to search")],
    root: Annotated[str, typer.Option(help="Relative search root")] = ".",
    limit: Annotated[int, typer.Option(help="Maximum matches")] = 100,
) -> None:
    """Run a bounded read-only search with Git grep fallback."""
    try:
        tools = ReadOnlyRepositoryTools(RepositorySandbox(repo))
        matches = tools.search_code(query, roots=(root,), limit=limit)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo("\n".join(str(match.__dict__) for match in matches))


@app.command("collect")
def collect(
    repo: Annotated[Path, typer.Option(help="Repository to collect facts from")],
    collector: Annotated[str, typer.Option(help="manifest, dependencies, or api-doc")],
    surface: Annotated[Optional[str], typer.Option(help="Evidence surface override")] = None,
    root: Annotated[str, typer.Option(help="Relative API document scan root")] = "src",
) -> None:
    """Run one deterministic Collector and print its structured result."""
    try:
        sandbox = RepositorySandbox(repo)
        parsed_surface = TypeAdapter(Surface).validate_python(surface) if surface else None
        if collector == "manifest":
            result = ManifestCollector().collect(sandbox)
        elif collector == "dependencies":
            result = DependencyCollector().collect(
                sandbox,
                source_surface=parsed_surface or "android_native",
            )
        elif collector == "api-doc":
            if parsed_surface is not None and parsed_surface != "backend_api_doc":
                raise ValueError("api-doc collector only supports backend_api_doc")
            result = ApiDocumentCollector().collect(sandbox, roots=(root,))
        else:
            raise ValueError("collector must be manifest, dependencies, or api-doc")
    except (OSError, ValueError, ValidationError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(result.model_dump_json(indent=2))


@app.command("build-manifest")
def build_manifest(
    profile: Annotated[Path, typer.Option(help="Applicability profile YAML")],
    controls: Annotated[Path, typer.Option(help="Control set YAML")],
    run_id: Annotated[str, typer.Option(help="Stable review run identifier")],
    output: Annotated[Path, typer.Option(help="Manifest JSON output path")],
    max_concurrency: Annotated[int, typer.Option(help="Default worker concurrency")] = 3,
) -> None:
    """Build a deterministic module-by-surface Review Manifest."""
    try:
        loaded_profile = load_profile(profile)
        loaded_controls = load_controls(controls)
        manifest = ReviewManifestBuilder().build(
            loaded_profile,
            loaded_controls,
            run_id=run_id,
            max_concurrency=max_concurrency,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    except (ConfigLoadError, OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(output.as_posix())


@app.command("run-review")
def run_review(
    manifest: Annotated[Path, typer.Option(help="Review Manifest JSON")],
    output_root: Annotated[Path, typer.Option(help="Per-work-item output directory")],
    model: Annotated[str, typer.Option(help="OpenAI-compatible model name")] = DEFAULT_MODEL,
    base_url: Annotated[
        str, typer.Option(help="OpenAI-compatible chat completions URL")
    ] = DEFAULT_BASE_URL,
    max_concurrency: Annotated[int, typer.Option(help="Maximum parallel workers")] = 3,
    token_budget: Annotated[
        int, typer.Option(help="Per-work-item Reviewer token budget")
    ] = 32000,
    checkpoint_db: Annotated[
        Optional[Path], typer.Option(help="Optional SQLite checkpoint database")
    ] = None,
    thread_id: Annotated[
        Optional[str], typer.Option(help="LangGraph thread id for resume/debug")
    ] = None,
) -> None:
    """Run a manifest with the LangGraph parent graph and read-only tools."""
    try:
        loaded = json.loads(manifest.read_text(encoding="utf-8"))
        review_manifest = ReviewManifest.model_validate(loaded)
        sandboxes = {
            surface: RepositorySandbox(Path(root))
            for surface, root in review_manifest.surface_roots.items()
        }
        runtime = LangGraphReviewRuntime(
            provider=OpenAICompatibleProvider(model=model, base_url=base_url),
            max_concurrency=max_concurrency,
            token_budget=token_budget,
        )
        if checkpoint_db is None:
            summary = runtime.run(
                manifest_run_id=review_manifest.run_id,
                work_items=review_manifest.work_items,
                sandboxes=sandboxes,
                output_root=output_root,
                thread_id=thread_id,
            )
        else:
            from langgraph.checkpoint.sqlite import SqliteSaver

            checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
            with SqliteSaver.from_conn_string(checkpoint_db.as_posix()) as saver:
                runtime = LangGraphReviewRuntime(
                    provider=OpenAICompatibleProvider(model=model, base_url=base_url),
                    max_concurrency=max_concurrency,
                    token_budget=token_budget,
                    checkpointer=saver,
                )
                summary = runtime.run(
                    manifest_run_id=review_manifest.run_id,
                    work_items=review_manifest.work_items,
                    sandboxes=sandboxes,
                    output_root=output_root,
                    thread_id=thread_id,
                )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    except Exception as exc:
        typer.echo(f"RUNTIME_ERROR={redact_sensitive_text(str(exc))}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(summary.model_dump_json(indent=2))


@app.command("full-review")
def full_review(
    workspace: Annotated[Path, typer.Argument(help="Workspace root")],
    model: Annotated[str, typer.Option(help="OpenAI-compatible model name")] = DEFAULT_MODEL,
    base_url: Annotated[
        str, typer.Option(help="OpenAI-compatible chat completions URL")
    ] = DEFAULT_BASE_URL,
    run_id: Annotated[Optional[str], typer.Option(help="Stable run identifier")] = None,
    max_concurrency: Annotated[int, typer.Option(help="Maximum parallel Reviewer work items")] = 3,
    token_budget: Annotated[
        int, typer.Option(help="Per-work-item Reviewer token budget")
    ] = 32000,
) -> None:
    """Run setup handoff, parallel review, deterministic resolution, and report."""
    try:
        workspace = workspace.expanduser().resolve()
        setup = ReviewSetupService(workspace).compile(
            run_id=run_id, max_concurrency=max_concurrency
        )
        controls = ControlSet.model_validate(
            json.loads((workspace / "setup" / "controls.json").read_text(encoding="utf-8"))
        )
        provider = OpenAICompatibleProvider(model=model, base_url=base_url)
        result = FullReviewService(
            workspace,
            LangGraphReviewRuntime(
                provider=provider,
                max_concurrency=max_concurrency,
                token_budget=token_budget,
            ),
        ).run(setup, controls)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    except Exception as exc:
        typer.echo(f"RUNTIME_ERROR={redact_sensitive_text(str(exc))}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"CI_STATUS={result.snapshot.ci_status.upper()} "
        f"RUN_ID={result.snapshot.run_id} REPORT={result.report_path}"
    )
    typer.echo(
        json.dumps(
            {
                "run_id": result.snapshot.run_id,
                "ci_status": result.snapshot.ci_status,
                "coverage_complete": result.coverage_gate.complete,
                "report": result.report_path,
            },
            indent=2,
        )
    )
    if result.snapshot.ci_status == "block":
        for reason in result.coverage_gate.blocking_reasons:
            typer.echo(f"BLOCK_REASON={reason}")
        raise typer.Exit(code=ci_exit_code(result.snapshot.ci_status))
    for reason in result.coverage_gate.warning_reasons:
        typer.echo(f"WARN_REASON={reason}")
    raise typer.Exit(code=ci_exit_code(result.snapshot.ci_status))


@app.command("diff-review")
def diff_review(
    workspace: Annotated[Path, typer.Argument(help="Workspace root")],
    baseline_run_id: Annotated[str, typer.Option(help="Completed baseline run ID")],
    model: Annotated[str, typer.Option(help="OpenAI-compatible model name")] = DEFAULT_MODEL,
    base_url: Annotated[
        str, typer.Option(help="OpenAI-compatible chat completions URL")
    ] = DEFAULT_BASE_URL,
    run_id: Annotated[Optional[str], typer.Option(help="Stable run identifier")] = None,
    max_concurrency: Annotated[int, typer.Option(help="Maximum parallel Reviewer work items")] = 3,
) -> None:
    """Run deterministic Git impact planning, safe reuse, and incremental review."""
    try:
        workspace = workspace.expanduser().resolve()
        setup = ReviewSetupService(workspace).compile(
            run_id=run_id,
            mode="diff",
            max_concurrency=max_concurrency,
        )
        controls = ControlSet.model_validate(
            json.loads((workspace / "setup" / "controls.json").read_text(encoding="utf-8"))
        )
        snapshot = Snapshot.model_validate(
            json.loads(
                (workspace / "runs" / baseline_run_id / "snapshot.json").read_text(encoding="utf-8")
            )
        )
        provider = OpenAICompatibleProvider(model=model, base_url=base_url)
        result = DiffReviewService(
            workspace,
            LangGraphReviewRuntime(provider=provider, max_concurrency=max_concurrency),
        ).run(setup, controls, snapshot)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    except Exception as exc:
        typer.echo(f"RUNTIME_ERROR={redact_sensitive_text(str(exc))}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"CI_STATUS={result.snapshot.ci_status.upper()} "
        f"RUN_ID={result.snapshot.run_id} REPORT={result.report_path}"
    )
    typer.echo(
        json.dumps(
            {
                "run_id": result.snapshot.run_id,
                "baseline_run_id": result.snapshot.baseline_run_id,
                "ci_status": result.snapshot.ci_status,
                "reviewed": len(result.snapshot.reviewed_rows),
                "reused": len(result.snapshot.reused_rows),
                "report": result.report_path,
            },
            indent=2,
        )
    )
    if result.snapshot.ci_status == "block":
        for reason in result.coverage_gate.blocking_reasons:
            typer.echo(f"BLOCK_REASON={reason}")
        raise typer.Exit(code=ci_exit_code(result.snapshot.ci_status))
    for reason in result.coverage_gate.warning_reasons:
        typer.echo(f"WARN_REASON={reason}")
    raise typer.Exit(code=ci_exit_code(result.snapshot.ci_status))
