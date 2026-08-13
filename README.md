# Financial App Compliance Review Tool

A Python-based, CI-oriented compliance review system for financial applications.

The project combines deterministic collectors and validators with a LangGraph parent graph and parallel AI reviewer subgraphs. Its coverage unit is `Control x Required Evidence Surface`; AI agents investigate and recommend, while ordinary program logic owns coverage, final resolution, snapshots, regression comparison, and CI gating.

Repository navigation uses a local Graphify CLI behind the project's `CodeMapProvider` boundary. Reviewers call `code_map_query` and `code_map_path` through the scoped Tool Runtime; they never receive direct shell access. Graphify helps locate related symbols and relationships; it is not a compliance scanner or a final evidence authority. `search_code` and `read_file` remain the verification and fallback tools, while `get_collector_facts` exposes precomputed deterministic facts.

## Status

The MVP pipeline now covers setup, source-to-control compilation, parallel Reviewer
execution, deterministic validation, targeted verification, final resolution, coverage
gating, snapshots, and Markdown reports. Reviewer results are confined to their assigned
Work Item and Collector Fact capabilities; file anchors use content revisions so dirty or
untracked changes invalidate stale evidence. Coverage distinguishes reviewed,
manual-required, blocked, and not-applicable rows. Trusted waiver input, diff/reuse,
anchor relocation, retry/resume, and CI process exit codes remain later-phase work. See
[the implementation plan](docs/implementation-plan.md) for the delivery roadmap.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
compliance-review --help
pytest
```

## Normal Workflow

先初始化 Compliance Workspace，登记代码仓库并生成带 provenance 的 App Profile draft：

```bash
compliance-review init ./my-review \
  --repository mobile=/path/to/mobile-repository \
  --repository backend=/path/to/backend-repository \
  --material /path/to/privacy-standard.md
```

如果 Profile 中存在无法从代码证明的业务字段，初始化会停在
`awaiting_confirmation`，不会伪造 jurisdiction、business type 或 self-lending。
确认接口目前由 Python service 提供，最终会写入 `setup/app_profile.json`。

初始化 Graphify 地图、生成 Review Manifest 和执行 Reviewer 仍可按下面的兼容流程运行：

```bash
# 方式一：初始化一个代码仓库
compliance-review init --repo /path/to/backend-repository

# 方式二：按照 App Profile 初始化 frontend_h5、android_native、backend_code
compliance-review init --profile examples/app-profile.yaml

# 生成 module x surface Review Work Items
compliance-review build-manifest \
  --profile examples/app-profile.yaml \
  --controls examples/mvp-controls.yaml \
  --run-id review-2026-01 \
  --output runs/review-2026-01/review-manifest.json

# 使用 LangGraph 运行并行 Reviewer，并保留 SQLite checkpoint
compliance-review run-review \
  --manifest runs/review-2026-01/review-manifest.json \
  --output-root runs/review-2026-01/work-items \
  --model <model-name> \
  --checkpoint-db runs/review-2026-01/review-checkpoints.sqlite \
  --thread-id review-2026-01

# 执行完整审查，产出 Snapshot、Coverage Manifest 和最终报告
compliance-review full-review ./my-review \
  --model <model-name> \
  --max-concurrency 3

# 在已有 baseline Snapshot 的基础上执行增量审查
compliance-review diff-review ./my-review \
  --baseline-run-id <completed-run-id> \
  --model <model-name> \
  --max-concurrency 3

# 查询已经初始化的代码地图
compliance-review code-map-query \
  --repo /path/to/backend-repository \
  --query "account deletion workflow" \
  --surface backend_code
```

旧的 `init --repo/--profile` 用法仍用于 Graphify 初始化：缺少 Graphify CLI 时执行 `uv tool install graphifyy`，然后在目标仓库中执行 `graphify extract . --code-only`。API 文档不走 Graphify，而由 `backend_api_doc` 的 API Document Collector 解析；源码路由由 Graphify 定位并由 Reviewer 精确核验。

## Core Principles

- Controls define what must be reviewed.
- Evidence surfaces define where proof may exist.
- Generic collectors extract repeatable technical facts.
- Parallel reviewers investigate isolated work items.
- A single verifier performs targeted QA only for suspicious results.
- Deterministic code owns coverage and final CI decisions.
- Every run produces an auditable compliance snapshot.

## License

MIT
