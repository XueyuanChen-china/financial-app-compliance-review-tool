# LangGraph Runtime Architecture

## 1. 迁移范围

本次迁移只替换运行时编排层，不重写领域模型和证据边界：

这里的“Python 领域模型”指项目用 Pydantic 定义的业务对象和校验规则，例如 `Control`、`WorkItem`、`Fact`、`Evidence` 和 `ReviewResult`。它回答的是“数据长什么样、哪些字段合法”，不是某一个 AI Agent。

“LangGraph 运行编排”指用 State、Node、Edge 和 Checkpoint 描述执行过程。它回答的是“先做什么、哪些 Work Items 可以并行、什么时候汇总、失败后如何恢复”，不替代 Control 规则或 Evidence 判断。

- 保留 `Control`、`ApplicabilityProfile`、`Fact`、`Evidence`、`WorkItem`、`ReviewResult`、`Snapshot`。
- 保留 `ReviewManifestBuilder`，它仍然负责把适用性结果转换成 Work Items。
- 保留 `RepositorySandbox`、`ScopedToolExecutor` 和 `ModelProvider`。
- 保留 JSONL append-only event log，作为审计轨迹，不把它当作 checkpoint。
- 将自定义 `ThreadPoolExecutor` 调度替换为 LangGraph Parent Graph。

因此，LangGraph 是状态化运行时，不是新的合规规则库，也不是新的代码扫描器。

## 2. Parent Graph

入口位于 `compliance_review.review.langgraph_runtime`。Parent Graph 保存可序列化的运行状态：

```text
run_id
work_items
max_concurrency
token_budget
executions[]
summary
```

图的结构是：

```text
START
  |
  | Send(one payload per Work Item)
  +--> reviewer subgraph A --+
  +--> reviewer subgraph B --+--> deferred summarize --> END
  +--> reviewer subgraph C --+
```

`Send` 负责动态 fan-out，`executions` 使用 LangGraph reducer 合并各分支结果，`summarize` 使用 `defer=True`，确保所有 reviewer 分支完成后再生成运行汇总。

`max_concurrency` 通过 LangGraph invocation config 传入，默认是 3。并行只发生在不同 Work Items 之间；同一个 Work Item 内部的模型和工具循环仍然是顺序的。

两个必须保持的运行原则：

```text
One Work Item = One Isolated Agent Context
Work Items are fan-out in LangGraph, but Reviewer execution is bounded by max_concurrency=3.
```

所有 Work Items 可以一次性进入 Parent Graph，但 LangGraph runtime 只会让最多 3 个 reviewer node 同时 active；其余 Work Items 留在内部等待，空闲槽位释放后继续执行。

## 3. Reviewer Subgraph

每个 `Send` payload 启动一个独立的 reviewer subgraph：

```text
initialize
    |
    v
call_model <----------------+
    |                        |
    +-- tool_calls --> execute_tools
    |
    +-- content ------------> finalize
```

子图状态包含：

- `attempt_id`、`agent_id`、context fingerprint。
- 当前消息列表和模型响应。
- 工具轮数和 token budget 使用量。
- 当前 Work Item 的结构化 `WorkerExecution`。

子图不会把 Provider 或 Sandbox 放进持久化状态，而是由图构建时注入。这样 checkpoint 只保存可序列化的业务状态，避免把连接、线程锁或文件句柄写入状态库。

Reviewer 的工具请求经过 `ScopedToolExecutor`，允许的工具为：

```text
code_map_query       -> CodeMapProvider -> Graphify
code_map_path        -> CodeMapProvider -> Graphify
code_map_explain     -> Graphify explain
code_map_callers     -> explain 有向 incoming calls/references
code_map_callees     -> explain 有向 outgoing calls/references
code_map_impact      -> Graphify affected
get_collector_facts  -> 父流程注入的 CollectorResult
search_code          -> git/repository search fallback（候选，不是证据）
read_file            -> 精确源码验证
capture_anchor       -> Sandbox 精确读取并铸造 Verified Anchor
list_files           -> 有限目录 inventory
```

Reviewer 不直接调用 shell 或 Graphify CLI。Tool Runtime 会在返回前执行 Work Item 的路径、结果数量、读取行数、Graphify budget 和总调用次数限制。Graphify/search 只产生候选位置；只有 `capture_anchor` 产生正式 Anchor。Collector 只有带准确路径、行号和原文的 source ref 才能进入 Anchor Ledger，否则只能作为 Fact 元数据。最终合规状态仍由后续 Validator/Resolver 计算。

## 4. ReviewerContextState

每个 Work Item 都有独立的 `ReviewerContextState`，它是该 Work Item 可恢复和压缩的最小工作状态，不是共享聊天记录：

```text
ReviewerContextState
├── immutable_context
│   ├── work_item
│   ├── required_surface
│   └── reviewer_instructions
├── evidence_ledger
├── compressed_memory
├── retired_rounds
├── active_rounds
├── compression_attempts
└── last_context_usage_ratio
```

`immutable_context`、`evidence_ledger` 和 `active_rounds` 永远不能作为压缩输入。
它们分别保证任务边界、可追溯证据锚点和最近工作上下文不被摘要改写。
不同 Work Item 可以共享只读的 Graphify provider、Collector facts、仓库元数据和工具实现，
但不能共享 messages、tool results、探索历史或可变 reviewer context。

一个完整 round 包含同一次模型响应、该响应发出的所有 tool calls，以及对应的 tool results。
默认 `active_window_size=3`：第 4 个 round 完成后，R1 移入 `retired_rounds`，R2-R4 留在
`active_rounds`。滑动本身不会立即触发压缩，只有下一次模型调用前的 context usage 达到阈值时才压缩。

上下文预算默认使用以下阈值：

```text
compression_trigger = 0.78
compression_target  = 0.60
hard_limit          = 0.90
max_compression_attempts = 2
```

达到 0.78 后同步调用 Provider 生成结构化 `CompressedReviewMemory`。压缩输入只能是旧的
compressed memory 和 retired rounds，不能包含 immutable context、evidence ledger 或 active rounds。
输出字段固定为 `generation`、`inspected_paths`、`inspected_symbols`、`findings`、`dead_ends`、
`unresolved_questions`、`next_search_hints`。压缩成功后替换旧 memory 并清空 retired rounds；
若仍高于 0.60，最多再进行一次 aggressive compression。

压缩失败会保留原 context 并重试；两次失败，或压缩后仍超过目标，当前 Work Item 返回
`indeterminate`，错误原因固定为 `context_budget_exhausted`，不会让 Parent Graph 崩溃。
执行预算（round、tool call、文件读取）与上下文预算（usage ratio 和阈值）分开计算。
Work Item 进入终态后释放 active/retired rounds、临时消息、live conversation 和压缩工作内存，
只保留结构化结果与 evidence anchors。

## 5. Checkpoint 与事件日志

两类持久化职责不同：

| 机制 | 作用 |
|---|---|
| LangGraph checkpoint | 保存图状态，使运行可以按 `thread_id` 查询和恢复 |
| `worker-events.jsonl` | 记录不可变的开始、完成、失败事件，供审计和排障 |

Python API 默认使用 `InMemorySaver`，适合测试。CLI 可以通过 `--checkpoint-db` 注入 SQLite checkpoint：

```bash
compliance-review run-review \
  --manifest runs/review-2026-01/review-manifest.json \
  --output-root runs/review-2026-01/work-items \
  --model <model-name> \
  --checkpoint-db runs/review-2026-01/review-checkpoints.sqlite \
  --thread-id review-2026-01
```

`thread_id` 是 LangGraph 的状态线程标识，不等于 `run_id`。实践中可以让它们保持稳定映射，但两者的职责仍然分开：`run_id` 标识业务审查，`thread_id` 标识一次可恢复的图执行。

当前是单 CLI process、单 Review Run 的有限并发模型，不使用 RabbitMQ、Kafka、Redis Queue、Celery 或其他外部消息队列。只有未来需要跨进程、跨机器或分布式 worker pool 时，才重新评估消息队列。

## 6. 边界与当前未实现项

当前版本已经实现：

- Parent Graph 动态 fan-out/fan-in。
- Reviewer 子图的模型/工具循环。
- 单 Work Item 输出隔离。
- 内存和 SQLite checkpoint 注入。
- LangGraph runtime 事件标记和结构化失败结果。

当前仍未实现：

- `interrupt()` 驱动的人工确认和 `Command(resume=...)` 交互。
- Snapshot 增量复用和回归比较。
- retry/resume、anchor relocation 和 CI process exit code 等 Day 6 能力。

Day 4 已在 Parent Graph 之外实现 `ResultValidator -> SuspiciousRouter -> Single Targeted Verifier -> ComplianceResolver -> CoverageGate`，并生成 Snapshot 与 Markdown Report。最终 PASS/FAIL 由普通 Python 逻辑根据完整 coverage denominator 和验证后的 evidence 决定，不由 Reviewer 或 LangGraph 调度状态直接决定。当前实现以代码、测试和 `docs/day4-learning-notes.md` 为准。

## 7. 兼容策略

`ReviewScheduler` 暂时保留为公共兼容门面，但其内部已经委托给 `LangGraphReviewRuntime`，不再创建 `ThreadPoolExecutor`。旧调用方可以先不改代码，新的调用方应直接使用 `LangGraphReviewRuntime`，以显式配置 checkpoint 和 `thread_id`。
