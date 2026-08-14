# Financial App Compliance Review Tool

一个面向金融类 App 的、可接入 CI 的增量合规审查工具。系统把法规控制项、App 适用性画像和多个技术证据面编译成结构化审查任务，再由 LangGraph 并行调度 Reviewer，最后由普通程序完成证据校验、控制项裁决、覆盖门禁、快照和报告生成。

核心原则是：**Agent 负责调查和提出建议，确定性代码负责验证、裁决和 CI 决定。**

## 当前状态

当前主链路已经覆盖：

- Phase 1：Workspace、App Profile、Repository Inventory 和 Collector Facts
- Phase 2：政策材料切分、Batch 编排、Obligation 提取和 Control 编译
- Phase 3：Control x Evidence Surface 覆盖规划
- Phase 4：LangGraph 并行 Reviewer、受控代码工具和 Graphify Code Map
- Phase 5：结果校验、Coverage Gate、Full Review、Diff Review、Snapshot 和 CI 状态
- 运行可靠性：attempt artifacts、失败重试、stale running 恢复和敏感信息脱敏
- 报告：固定中文 Markdown 模板，机器字段填充，不使用 Agent 自由文本决定最终状态

`SuspiciousRouter`、`TargetedVerifier` 和 `VerifierResult` 仍保留为旧产物/调用方的兼容模型，但不参与当前 Full Review 或 Diff Review 的权威判定。

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
compliance-review --help
pytest
ruff check .
mypy
```

## 正常流程

### 1. 初始化 Workspace

登记代码仓库和政策材料，生成带 provenance 的 App Profile draft：

```bash
compliance-review init ./my-review \
  --repository mobile=/path/to/mobile-repository \
  --repository backend=/path/to/backend-repository \
  --material /path/to/privacy-standard.md
```

如果 jurisdiction、business type 或 self-lending 等字段无法从材料确定，初始化会停在 `awaiting_confirmation`，不会自行编造结论。

### 2. 生成并执行 Reviewer

```bash
compliance-review build-manifest \
  --profile examples/app-profile.yaml \
  --controls examples/mvp-controls.yaml \
  --run-id review-2026-01 \
  --output runs/review-2026-01/review-manifest.json

compliance-review run-review \
  --manifest runs/review-2026-01/review-manifest.json \
  --output-root runs/review-2026-01/work-items \
  --model <model-name> \
  --checkpoint-db runs/review-2026-01/review-checkpoints.sqlite \
  --thread-id review-2026-01
```

### 3. 运行完整审查或增量审查

```bash
compliance-review full-review ./my-review \
  --model <model-name> \
  --max-concurrency 3

compliance-review diff-review ./my-review \
  --baseline-run-id <completed-run-id> \
  --model <model-name> \
  --max-concurrency 3
```

命令会根据 Coverage Gate 返回 CI 状态：`PASS` 返回 0，`WARN` 返回 0，`BLOCK` 返回 1。

每次运行的核心产物包括：

```text
runs/<run_id>/result_validation.json
runs/<run_id>/coverage_manifest.json
runs/<run_id>/snapshot.json
runs/<run_id>/report.md
```

报告使用中文固定结构，包含控制项结论、证据覆盖台账、人工复核变化、自动化证据退化、校验标记、阻断原因和机器产物路径。

## 架构摘要

```text
Controls + Applicability Profile
              |
              v
        Parent LangGraph
              |
        Build Work Items
              |
     +--------+--------+
     v        v        v
 Reviewer  Reviewer  Reviewer
     +--------+--------+
              v
   Result Validator
              v
   Compliance Resolver
              v
       Coverage Gate
              v
    Snapshot + Report
```

覆盖单位固定为：

```text
Control x Required Evidence Surface
```

当前支持的证据面包括 `frontend_h5`、`android_native`、`backend_api_doc`、`backend_code`、`play_console` 和 `regulator_external`。外部人工证据不会伪装成自动化代码证据；缺失的自动化证据会进入 CI 阻断策略。

## 工具边界

Reviewer 只能通过受控工具读取当前 Work Item：

- `code_map_query`、`code_map_path`：通过 `CodeMapProvider` 调用本地 Graphify
- `search_code`、`read_file`、`list_files`：精确检索和验证源码
- `get_collector_facts`：读取 Manifest、Dependency、API 文档等确定性事实

Reviewer 不拥有 unrestricted shell，也不能修改源码、Controls、Snapshot 或最终报告。Graphify 负责代码导航，不代表代码已经满足某个合规控制项。

## GitHub Actions

每次 Pull Request 和 `main` push 会执行：

1. `pytest`
2. `ruff check .`
3. `mypy`
4. Phase 1-5 compliance integration tests

Workflow 位于 `.github/workflows/ci.yml`。CI 质量检查和合规集成检查都必须通过，才算进入可测试状态。

## 测试阶段验收

自动化测试覆盖 schema、Collectors、工具边界、Full/Diff Review、Coverage Gate、报告和 durable attempt 恢复。进入真实模型测试时，还应完成一次人工恢复演示：

1. 启动同一个 `full-review`，指定固定 `--run-id`、`--checkpoint-db` 和 `--thread-id`。
2. 在 Reviewer 执行期间终止进程。
3. 使用相同参数重新执行。
4. 检查 `runs/<run_id>/reviewer_results/**/attempts/` 和 `worker-events.jsonl`：已完成 Work Item 不应重复执行，未完成 Work Item 应创建新的 attempt，最终 Snapshot 和中文报告应正常生成。

这项真实进程级验收不由普通单元测试替代，完成后再记录为发布演示结果。

## 目录与学习资料

- `src/compliance_review/`：领域模型、Collectors、LangGraph Runtime、Validator、Resolver 和报告
- `tests/`：领域契约、Collector、Full/Diff Review、恢复和集成测试
- `docs/day1-learning-notes.md` 至 `docs/day5-learning-notes.md`：分阶段学习记录
- `docs/implementation-plan.md`：整体实施计划
- `docs/langgraph-architecture.md`：LangGraph 编排说明
- `docs/graphify-provider.md`：Graphify Provider 说明

## 许可

MIT
