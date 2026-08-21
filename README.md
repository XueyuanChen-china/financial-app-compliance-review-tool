# Financial App Compliance Review Tool

面向金融类 App 的多智能体合规审查工具。系统将法规材料编译为结构化 Control，结合 App 画像、Android/后端代码、API 文档和外部材料生成可追溯的审查任务，并输出证据、风险结论和 CI 门禁状态。

核心设计原则：

> Agent 负责调查和提出建议，确定性程序负责验证证据、裁决状态和生成最终结论。

## 能力概览

- **Multi-Agent Review**：基于 LangGraph 编排 Applicability Agent、Impact Agent 和 Reviewer Agent，按 `Control × Evidence Surface` 拆分独立 Work Item，并限制并行度。
- **ReAct + Tool Calling**：Reviewer 通过受控只读工具进行代码导航、文件读取、事实查询和证据捕获，不直接获得 Shell 权限。
- **Graphify Code Map**：支持语义查询、关系路径、节点解释、Callers/Callees 和影响分析，用于定位跨文件代码关系。
- **证据可追溯**：代码证据必须经过 `capture_anchor` 生成，程序校验路径、行号、原文 Hash、仓库版本和引用关系。
- **全量与增量审查**：Full Review 建立完整基线；Diff Review 根据 Git 变更和影响分析仅重审受影响的 Coverage Unit，并复用其他有效结果。
- **确定性门禁**：Validator、Resolver 和 Coverage Gate 对模型结果进行 schema、证据一致性和覆盖完整性校验，最终输出 `PASS`、`WARN` 或 `BLOCK`。
- **中文审查报告**：报告包含 Control 结论、证据覆盖、未覆盖面、人工复核要求、阻断原因和机器产物路径。

## 技术栈

Python、LangGraph、Pydantic、Graphify、OpenAI-compatible API、SQLite Checkpoint、Pytest、Ruff、Mypy。

## 工作流

```text
Policy Sources
      |
      v
Obligations -> Controls -> Applicability Resolution
                                  |
                                  v
                       Coverage Units / Work Items
                                  |
                 +----------------+----------------+
                 v                v                v
             Reviewer         Reviewer         Reviewer
                 +----------------+----------------+
                                  v
                         Evidence Validator
                                  v
                         Resolver / Gate
                                  v
                         Snapshot / Report
```

系统支持以下证据面：

`frontend_h5`、`android_native`、`backend_api_doc`、`backend_code`、`play_console`、`regulator_external`。

外部人工材料不会被伪装成代码证据；缺失或未经验证的证据会保留为明确的覆盖缺口。

## 快速开始

### 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
compliance-review --help
```

### 初始化审查项目

```bash
compliance-review init ./my-review \
  --repository mobile=/path/to/mobile-repository \
  --repository backend=/path/to/backend-repository \
  --material /path/to/policy.md
```

初始化阶段只登记仓库、材料和画像事实。无法可靠确认的 jurisdiction、business type 或其他适用性信息会保留为待确认状态，不会由模型自行编造。

### 执行 Full Review

```bash
compliance-review full-review ./my-review \
  --model gpt-5.6-luna \
  --max-concurrency 3
```

### 执行 Diff Review

```bash
compliance-review diff-review ./my-review \
  --baseline-run-id <completed-run-id> \
  --model gpt-5.6-luna \
  --max-concurrency 3
```

Diff Review 要求法规、Control、画像、Applicability、仓库映射和外部材料与 Full 基线一致。非代码输入发生变化时，系统会要求重新执行 Full Review；代码变化则由 Impact Agent 分析受影响范围。

## 模型配置

复制 `.env.example` 为本地 `.env`，不要提交真实密钥：

```dotenv
OPENAI_API_KEY=
COMPLIANCE_REVIEW_MODEL=gpt-5.6-luna
COMPLIANCE_REVIEW_BASE_URL=https://api.openai.com/v1/chat/completions
COMPLIANCE_REVIEW_REASONING_EFFORT=high
COMPLIANCE_REVIEW_TIMEOUT_SECONDS=180
```

项目通过 OpenAI-compatible Chat Completions 适配器调用模型。`gpt-5.6-luna` 支持的 reasoning effort 为 `none`、`low`、`medium`、`high`、`xhigh` 和 `max`，不支持 `minimal`。工具回合会保留完整的 assistant function call 与对应 tool result，并兼容 Chat Completions 到 Responses 的中转服务。

## Graphify

Graphify 用于代码导航，不直接决定合规结论。Reviewer 通过项目封装的 `CodeMapProvider` 使用 Graphify，不直接执行 Graphify CLI。

初始化目标代码仓库的代码图：

```bash
uv tool install graphifyy
graphify extract /path/to/repository --code-only
```

查询示例：

```bash
compliance-review code-map-query \
  --repo /path/to/repository \
  --query "account deletion workflow" \
  --surface backend_code
```

Graphify 返回的是候选节点和关系。即使 Graphify 没有找到节点，也不能据此证明代码不存在；系统会结合 `search_code`、`read_file` 和 `capture_anchor` 进行精确验证。

## 证据边界

Reviewer 可使用以下受控只读工具：

- `code_map_query`、`code_map_path`、`code_map_explain`
- `code_map_callers`、`code_map_callees`、`code_map_impact`
- `get_collector_facts`、`list_files`、`search_code`、`read_file`
- `capture_anchor`

Graphify、搜索和目录列表只产生导航候选，不能直接进入最终 Evidence。代码证据必须经过精确读取和 Anchor 校验，最终结果只引用验证通过的 `anchor_id`。模型不能自行编写路径、行号、代码片段或证据 Hash。

## 产物

每次运行都会在 `runs/<run_id>/` 下生成机器可读产物和 Markdown 报告，主要包括：

```text
result_validation.json
coverage_manifest.json
snapshot.json
report.md
```

Diff Review 还会生成：

```text
diff/diff.json
diff/impact-work-items.json
diff/impact-decisions.json
diff/carried-forward-lineage.json
```

## 本地验证

本仓库不绑定被审查项目的 CI，也不内置 GitHub Actions。提交前执行：

```bash
pytest
ruff check src tests
mypy src
git diff --check
```

接入目标项目时，可以在目标项目自己的 CI 中调用上述检查和审查 CLI，并根据退出码处理 `PASS`、`WARN` 或 `BLOCK`。

## 项目结构

```text
src/compliance_review/       核心领域模型、Agent Runtime、工具和报告
tests/                       单元测试、契约测试和端到端测试
scripts/                     示例项目和真实审查运行脚本
docs/                        架构、设计决策和开发文档
test_inputs/                 测试用政策和 API 输入
```

## 当前边界

- 这是静态和材料型合规审查工具，不替代律师意见、监管确认或真实运行时测试。
- Graphify 是代码导航能力，不是合规判断引擎。
- 静态代码、API 文档和外部声明不能自动升级为运行时证明。
- 最终状态必须经过确定性校验和 Coverage Gate，模型不能绕过门禁直接生成 PASS。

## License

MIT
