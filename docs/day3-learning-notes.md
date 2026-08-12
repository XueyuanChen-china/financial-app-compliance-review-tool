# Day 3 学习与实现记录：Parallel Reviewer Pipeline

## 1. Day 3 目标

Day 3 把 Day 2 的 Facts 和只读工具接入 Reviewer Pipeline：

```text
Profile + Controls
        |
        v
Review Manifest
        |
        v
独立 Work Items
        |
        v
受控并发 Reviewer Workers
        |
        v
结构化 Review Results + Events
```

本阶段仍然不计算最终合规 PASS/FAIL。Reviewer 只负责调查并返回建议，后续 Validator/Resolver 才负责最终判断。

## 2. Review Manifest

`ReviewManifestBuilder` 根据：

- `app_profile.yaml`
- `controls.yaml`
- Control 的 `applicability_expression`
- Control 的 `required_surfaces`

生成 `review_manifest.v1`。

一个 Work Item 的粒度是：

```text
module_id x evidence_surface
```

同一个 module 和 surface 下的多个 Controls 会被合并到一个 Work Item，但每个 Control 仍然必须在最终结果中单独有一行。

Manifest 还记录：

- `surface_roots`：每个证据面的仓库根目录。
- `excluded_controls`：适用性明确为 false 的 Control 及原因。
- `default_max_concurrency`：默认并发数为 3。
- profile/control version：便于后续 Snapshot 和结果复用校验。

重要路径语义：

```text
surface_roots 是仓库级路径
allowed_roots 是挂载后的 worker 内部相对路径
```

Scheduler 会把某个 surface 的 root 作为独立 Sandbox 挂载给 Worker，因此 Worker 默认只能读取该 Sandbox 内的 `.`，不会把前端仓库、Android 仓库和后端仓库混在一个根目录里。

## 3. Applicability 判断

第一版没有使用 Python `eval`，只支持有限的声明式条件：

```text
business_type includes personal_loan
self_lending == true
jurisdiction == Pakistan
frontend_h5 in evidence_surfaces
```

判断结果有三种含义：

- `true`：生成对应 Work Items。
- `false`：写入 `excluded_controls`，不生成 Work Item。
- `unknown`：保守保留 Control，避免因表达式暂不支持而漏审。

这层只做适用性筛选，不把筛选结果当成合规结论。

## 4. Model Provider Adapter

项目定义稳定的 `ModelProvider` 接口：

```python
complete(ModelRequest) -> ModelResponse
```

目前有两个实现：

- `StaticModelProvider`：用于测试、无 LLM smoke run 和确定性回归。
- `OpenAICompatibleProvider`：调用 OpenAI-compatible Chat Completions endpoint。

Provider 只负责传输和解析模型响应，不负责：

- 执行文件读取。
- 修改项目文件。
- 判断 Control 最终状态。
- 绕过结构化结果校验。

## 5. Reviewer Worker

每个 Worker 只处理一个 Work Item，并拥有独立的：

- `attempt_id`
- `agent_id`
- context fingerprint
- token budget
- tool executor
- output directory

结果固定写到：

```text
<output_root>/<work_item_id>/review-result.json
```

Worker 不允许直接写其他 Work Item 的目录，也不直接改 Snapshot、报告或 Control 文件。

## 6. Tool-call 和读取边界

Reviewer 只能通过项目自己的 Tool Runtime 请求以下只读工具：

```text
code_map_query       # Graphify 语义导航
code_map_path        # Graphify 节点关系路径
get_collector_facts  # 读取预计算确定性 Facts
search_code          # 精确搜索 fallback
read_file            # 候选源码验证
list_files           # 有限目录 inventory
```

Reviewer 不直接获得 shell 或 Graphify CLI 权限。调用链是：

```text
Reviewer LLM
  -> ScopedToolExecutor
  -> CodeMapProvider / Collector store / Repository tools
  -> structured ToolResult
```

`ScopedToolExecutor` 会检查工具白名单、路径边界、文件读取数量、单次读取行数、搜索/list/Graphify 结果数量、Graphify budget 和 Work Item 总 tool call 数量。

如果工具调用越界，不会读取文件，而是返回结构化失败 Tool Result，让 Reviewer 继续决定是否返回 `indeterminate`。Graphify 没有找到节点也不能证明代码不存在，必须用 `search_code` 和 `read_file` 做 fallback 验证。

## 7. Token Budget

每个 Worker 有独立 token budget：

- 记录 provider 声明的 input/output tokens。
- 对本地消息和工具结果进行确定性字符近似计费。
- 超过预算时转成结构化失败结果。

预算失败不会让整个 Scheduler 崩溃，也不会被伪装成 PASS；对应 Control Surface 行会变成：

```yaml
evidence_status: missing
recommended_control_status: indeterminate
```

## 8. 结构化 Review Result

Worker 必须返回 `review_result.v1`，并且必须覆盖该 Work Item 的全部 `control_ids`：

```yaml
contract: review_result.v1
work_item_id: wi.consent_and_user_notice.frontend_h5
attempt_id: run-id.work-item.attempt
execution_status: completed
rows:
  - control_id: consent_and_user_notice.explicit_consent_before_registration
    surface: frontend_h5
    evidence_status: partial
    recommended_control_status: indeterminate
    evidence_ids: []
    gap_reasons: []
    observations: []
agent_id: reviewer-001
verifier_required: false
errors: []
```

Worker 会拒绝以下结果：

- JSON 无法解析。
- `work_item_id` 不匹配。
- `attempt_id` 不匹配。
- `agent_id` 不匹配。
- 没有覆盖所有分配的 Control。

## 9. LangGraph 受控并发 Runtime

`LangGraphReviewRuntime` 使用 Parent Graph 编排 Reviewer 子图，默认 `max_concurrency=3`。

运行原则是：

```text
One Work Item = One Isolated Agent Context
Work Items are fan-out in LangGraph, but Reviewer execution is bounded by max_concurrency=3.
```

Parent Graph 负责：

- 通过 `Send` 为每个 Work Item 建立一个独立分支。
- 把 surface root 映射到对应 Sandbox。
- 通过 `executions` reducer 合并分支结果。
- 用 `defer=True` 等所有分支完成后再生成运行汇总。
- 将单个 Reviewer 的异常转成结构化失败结果。

Reviewer 子图负责：

- 调用模型。
- 执行受限的只读工具。
- 根据工具结果继续模型循环。
- 写入自己的 `review-result.json`。

并发不意味着共享上下文。每个 Reviewer 只能看到自己的 Work Item、工具执行器和输出路径。`ReviewScheduler` 仍保留为兼容门面，但内部已委托给 LangGraph，不再直接创建线程池。

所有 Work Items 可以进入 LangGraph fan-out，但超过 3 个的 Work Items 会等待执行槽位，不引入 RabbitMQ、Kafka、Redis Queue、Celery 等外部消息队列。

LangGraph checkpoint 与 JSONL 事件日志职责不同：checkpoint 用于恢复图状态，JSONL 用于保留不可变审计轨迹。详见 [LangGraph Runtime Architecture](langgraph-architecture.md)。

## 10. Reviewer Context 管理

每个 Work Item 有独立的 `ReviewerContextState`，共享的只有只读工具实现、Graphify provider、Collector facts
和仓库元数据。消息、工具结果、探索历史和压缩工作记忆都不跨 Work Item 共享。

默认保留最近 3 个完整 round。第 4 个 round 只会把最旧 round 移入 retired，不会立刻压缩。
下一次模型调用前估算 context usage：达到 78% 才同步压缩，目标降到 60%，硬上限为 90%，最多两次。
压缩只允许读取旧 memory 和 retired rounds，不能压缩 immutable context、evidence ledger 或 active rounds。

压缩输出必须是结构化 `CompressedReviewMemory`，包含 generation、inspected paths/symbols、findings、
dead ends、unresolved questions 和 next search hints。失败时保留旧状态并重试；两次失败或仍超限时，
当前 Work Item 返回 `indeterminate` 和 `context_budget_exhausted`，父图继续处理其他 Work Items。
终态 Work Item 释放 live messages、active/retired rounds 和临时压缩内存，但保留结构化结果与证据锚点。

执行预算（round、tool call、文件读取）与上下文预算（usage ratio 和阈值）独立计算。

## 11. Append-only Worker Events

运行过程写入 JSONL 事件日志：

```text
run_started
worker_started
worker_completed / worker_failed
run_completed
```

每条事件包含：

- sequence
- event_id
- occurred_at
- run_id
- work_item_id
- agent_id
- attempt_id
- 结果路径或错误信息

事件只追加，不覆盖旧记录。这样后续可以追踪某个 Work Item 是哪个 Worker、哪个 attempt 产生的。

## 12. 验收测试

当前测试覆盖：

- Manifest 按 module 和 surface 分组。
- 3 个 Work Items 确实通过 LangGraph `Send` 同时进入 provider。
- 3 个 Worker 的输出路径互不相同。
- context fingerprint 互不相同。
- event sequence 没有重复或覆盖。
- 工具调用可以读取允许目录。
- 工具调用读取其他目录会被拒绝。
- Worker 可以先请求 `read_file`，再返回结构化结果。
- provider 结果不符合 Work Item 合同会失败。
- active window 会在第 4 个 round 后滑动，且不会立即压缩。
- 达到 78% 时只压缩结构化的 retired rounds，immutable context、evidence ledger 和 active rounds 不变。
- 压缩成功会清空 retired rounds；失败或超过硬上限会返回 `context_budget_exhausted` 的 indeterminate 结果。

验证命令：

```bash
.venv/bin/ruff check .
.venv/bin/mypy src
.venv/bin/pytest
```

当前结果：36 个测试通过。

## 13. CLI 使用

先生成 Manifest：

```bash
.venv/bin/compliance-review build-manifest \
  --profile examples/app-profile.yaml \
  --controls examples/mvp-controls.yaml \
  --run-id review-2026-01 \
  --output runs/review-2026-01/review-manifest.json
```

使用 OpenAI-compatible provider 执行：

```bash
.venv/bin/compliance-review run-review \
  --manifest runs/review-2026-01/review-manifest.json \
  --output-root runs/review-2026-01/work-items \
  --model <model-name> \
  --checkpoint-db runs/review-2026-01/review-checkpoints.sqlite \
  --thread-id review-2026-01
```

API key 通过 `OPENAI_API_KEY` 环境变量提供，不写入 Manifest、事件日志或结果文件。

## 14. Day 3 结论

Day 3 已经把“多个 Reviewer 并行审查”从架构图落成了可运行的 LangGraph 基础管线：

```text
Controls/Profile
  -> Review Manifest
  -> Work Item Sandbox
  -> Parent Graph Send
  -> Reviewer Subgraph: Provider + Read-only Tool Calls
  -> Structured Review Result
  -> Deferred Summary + Checkpoint + Append-only Events
```

当前仍未实现最终 Validator、Verifier、Resolver 和 Snapshot。下一阶段应在不破坏本阶段 Work Item 隔离的前提下，增加结果一致性校验、疑点复核和最终状态计算。
