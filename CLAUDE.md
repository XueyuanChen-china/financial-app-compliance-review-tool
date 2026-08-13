# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development commands

This is a Python 3.9+ package using a `src/` layout and Hatchling as its build backend.

```bash
# Initial setup
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# Quality checks
ruff check .
ruff format --check .
mypy
pytest

# Apply formatting
ruff format .

# Run one test file or one test
pytest tests/test_full_review.py
pytest tests/test_full_review.py::test_full_review_writes_snapshot_and_blocks_missing_backend_evidence

# Build a wheel without requiring an additional build frontend
python -m pip wheel . --no-deps -w dist/

# Inspect or run the CLI
compliance-review --help
python -m compliance_review --help
```

Pytest already uses `-q` through `pyproject.toml`. Ruff enforces `E`, `F`, `I`, and `B` with a 100-character line length; mypy runs in strict mode. Tests use deterministic `StaticModelProvider` response factories and temporary workspaces rather than live model calls.

## End-to-end workflow

The current workspace-based flow is:

1. `compliance-review init <workspace> --repository <id>=<path> --material <path>` inventories repositories, collects deterministic facts, and writes an App Profile draft.
2. `compliance-review confirm-profile <workspace> ...` supplies unresolved business fields and writes `setup/app_profile.json`. Do not infer jurisdiction, business type, self-lending, or other unprovable business facts from source code.
3. `compliance-review compile-rules <workspace> --model <model> ...` turns registered policy sources into obligations and validated controls.
4. `compliance-review prepare-review <workspace>` deterministically creates `Control x Required Evidence Surface` coverage units and module-by-surface work items.
5. `compliance-review full-review <workspace> --model <model>` runs reviewers, validation, targeted verification, resolution, coverage gating, snapshot generation, and the Markdown report.

Commands that invoke a model use `OpenAICompatibleProvider`. Set `OPENAI_API_KEY`; use `--base-url` for another OpenAI-compatible Chat Completions endpoint. `compliance-review review` is still a reserved placeholder—use `full-review` for the implemented pipeline.

The legacy `init --repo` / `init --profile`, `build-manifest`, and `run-review` commands remain for direct Graphify and runtime workflows. Graphify is optional navigation infrastructure: when absent, initialization can install it with `uv tool install graphifyy` and runs `graphify extract . --code-only` in the target repository. `backend_api_doc` evidence is parsed by the API document collector rather than Graphify.

## Architecture

The system deliberately separates probabilistic investigation from deterministic compliance decisions:

- **Contracts and configuration:** `domain/models.py` contains the shared Pydantic contracts. `config/loader.py` loads YAML safely; `examples/app-profile.yaml` and `examples/mvp-controls.yaml` are canonical fixtures.
- **Workspace setup and planning:** `setup/service.py` orchestrates repository inventory, deterministic app facts, conservative profile drafting/confirmation, applicability evaluation, coverage construction, and work-item planning. `setup/planning.py` owns the finite applicability expression evaluator and the coverage denominator. A runtime handoff is valid only after the profile and controls pass deterministic validation.
- **Rule compilation:** `compilation/service.py` registers policy sources, makes two structured model calls (obligation extraction and control compilation), then validates the result deterministically. Recompilation invalidates the previously generated validated control artifact before replacing it.
- **Repository evidence:** `collectors/` emits repeatable manifest, dependency, and API facts. `code_map/` wraps Graphify behind `CodeMapProvider`; it finds candidate symbols and relationships but is never final evidence. Reviewer conclusions must be grounded with bounded `search_code`, `read_file`, or collector facts.
- **Review runtime:** `review/langgraph_runtime.py` builds a parent LangGraph that fans out one isolated reviewer subgraph per `WorkItem`, gathers executions with a reducer, and emits a deferred summary after all branches return. Each reviewer loops through model calls and scoped read-only tools before writing a structured `ReviewResult`. `review/context.py` maintains immutable context, an evidence ledger, active/retired rounds, and bounded compression. `review/scheduler.py` is a compatibility facade; the LangGraph runtime is authoritative.
- **Finalization:** `review/full_review.py` runs `ResultValidator -> SuspiciousRouter -> optional TargetedVerifier -> ComplianceResolver -> CoverageGate`, then creates the snapshot and report. Agent prose and confidence do not decide final status or CI gating. A control passes only when every required coverage row is valid, complete, and passing; otherwise deterministic logic produces fail or indeterminate outcomes.
- **Persistence:** `persistence/artifact_store.py` confines atomic writes to the compliance workspace. Setup state lives under `setup/`; each run lives under `runs/<run_id>/` with the manifest, append-only `worker-events.jsonl`, SQLite checkpoint, reviewer results, validation/verifier outputs, `coverage_manifest.json`, `snapshot.json`, and `report.md`.

`docs/langgraph-architecture.md` predates the current finalization implementation and still describes some Day 4 stages as unimplemented. For current behavior, prefer the code, tests, README, and `docs/day4-learning-notes.md`.

## Safety and evidence boundaries

Reviewer-facing tools are read-only and scoped to each work item's allowed roots and budgets. `RepositorySandbox` rejects path traversal and symlink escapes, limits reads, and blocks common secret/signing files such as `.env*`, service-account files, private keys, certificates, and keystores. Preserve these boundaries when adding tools: reviewers do not receive a shell, mutate source repositories, or call business-state APIs.

Evidence coverage is based on `Control x Required Evidence Surface`, not merely control IDs. Changes to controls, surfaces, work-item grouping, result rows, or evidence anchors must remain consistent across setup planning, runtime result validation, finalization, snapshots, and their tests.
