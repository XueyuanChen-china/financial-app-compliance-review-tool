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

模型只能请求三个只读工具：

```text
list_files
search_code
read_file
```

`ScopedToolExecutor` 会检查：

- 工具名称是否在白名单。
- 路径是否位于当前 Work Item 的 allowed roots。
- `read_file` 是否超过 `max_files_read`。
- 单次读取是否超过 `max_lines_per_read`。
- 搜索结果和文件列表是否超过数量上限。

如果工具调用越界，不会读取文件，而是返回结构化的失败 Tool Result，让 Worker 继续决定是否返回 `indeterminate`。

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

## 9. 受控并发 Scheduler

`ReviewScheduler` 使用 bounded `ThreadPoolExecutor`，默认 `max_concurrency=3`。

Scheduler 负责：

- 为每个 Work Item 分配 agent id。
- 把 surface root 映射到对应 Sandbox。
- 并发启动独立 Worker。
- 收集并按 `work_item_id` 排序结果。
- 将单个 Worker 的异常转成结构化失败结果。
- 生成运行汇总。

并发不意味着共享上下文。每个 Worker 只能看到自己的 Work Item、自己的工具执行器和自己的输出路径。

## 10. Append-only Worker Events

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

## 11. 验收测试

当前测试覆盖：

- Manifest 按 module 和 surface 分组。
- 3 个 Work Items 确实同时进入 provider。
- 3 个 Worker 的输出路径互不相同。
- context fingerprint 互不相同。
- event sequence 没有重复或覆盖。
- 工具调用可以读取允许目录。
- 工具调用读取其他目录会被拒绝。
- Worker 可以先请求 `read_file`，再返回结构化结果。
- provider 结果不符合 Work Item 合同会失败。

验证命令：

```bash
.venv/bin/ruff check .
.venv/bin/mypy src
.venv/bin/pytest
```

当前结果：24 个测试通过。

## 12. CLI 使用

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
  --model <model-name>
```

API key 通过 `OPENAI_API_KEY` 环境变量提供，不写入 Manifest、事件日志或结果文件。

## 13. Day 3 结论

Day 3 已经把“多个 Reviewer 并行审查”从架构图落成了可运行的基础管线：

```text
Controls/Profile
  -> Review Manifest
  -> Work Item Sandbox
  -> Provider + Read-only Tool Calls
  -> Structured Review Result
  -> Append-only Events
```

当前仍未实现最终 Validator、Verifier、Resolver 和 Snapshot。下一阶段应在不破坏本阶段 Work Item 隔离的前提下，增加结果一致性校验、疑点复核和最终状态计算。
