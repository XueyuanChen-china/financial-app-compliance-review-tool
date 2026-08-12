# Financial App Compliance Review Tool

A Python-based, CI-oriented compliance review system for financial applications.

The project combines deterministic collectors and validators with parallel AI reviewers. Its coverage unit is `Control x Required Evidence Surface`; AI agents investigate and recommend, while ordinary program logic owns coverage, final resolution, snapshots, regression comparison, and CI gating.

Repository navigation uses a local Graphify CLI behind the project's `CodeMapProvider` boundary. Graphify helps locate related symbols and relationships; it is not a compliance scanner or a final evidence authority. `search_code` and `read_file` remain the verification and fallback tools.

## Status

The repository is in architecture and foundation setup. See [the implementation plan](docs/implementation-plan.md) for the full design and delivery roadmap.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
compliance-review --help
pytest
```

## Normal Workflow

先初始化目标代码仓库的 Graphify 地图，再生成 Review Manifest 和执行 Reviewer：

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

# 查询已经初始化的代码地图
compliance-review code-map-query \
  --repo /path/to/backend-repository \
  --query "account deletion workflow" \
  --surface backend_code
```

`init` 默认会在缺少 Graphify CLI 时执行 `uv tool install graphifyy`，然后在目标仓库中执行 `graphify extract . --code-only`。API 文档不走 Graphify，而由 `backend_api_doc` 的 API Document Collector 解析；源码路由由 Graphify 定位并由 Reviewer 精确核验。

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
