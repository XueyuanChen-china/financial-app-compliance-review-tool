# Phase 3：Applicability、Coverage 与 Runtime Handoff

## 1. 本阶段解决什么问题

Phase 2 产出的是已验证的 Control Set，但 Reviewer 还需要知道：

1. 当前 AppProfile 下哪些 Control 适用。
2. 每个适用 Control 需要检查哪些 evidence surface。
3. 哪些 Control × surface 组合要交给哪个 WorkItem。
4. 如何把这些 WorkItems 安全交给现有 LangGraphReviewRuntime。

本阶段只做确定性规划，不让 LLM 改写 Coverage 分母，也不重写 Reviewer Parent/Subgraph。

## 2. Applicability Engine

`ApplicabilityEngine` 使用结构化 `ApplicabilityCondition`，不使用 Python
`eval()`，也不把旧字符串表达式当作新的规则语言。条件节点只允许有限的
`atom`、`all_of`、`any_of` 和 `unknown` 形状；旧版字符串只由 migration
adapter 读取，无法无损转换时进入 `unknown`。

每个 Control 都会生成一个决策：

- `applicable`：进入 Coverage Unit 生成。
- `not_applicable`：只有来源引用和已确认画像事实都能证明排除时才接受，并记录排除原因。
- `unknown`：保守保留，进入 Coverage Unit，并标记未知原因。

UNKNOWN 不等于 FALSE。它表示当前 Profile 信息不足，不能据此漏审。

## 3. Coverage Unit

Coverage Unit 的固定定义是：

```text
Coverage Unit = Control × Required Surface
```

例如一个 Control 要求 `frontend_h5`、`android_native` 和 `backend_code`，即使后端代码当前缺失，也必须生成三个 Coverage Unit。后端缺失会被标成 `missing_surface`，而不是从分母中删除。

Coverage Unit 由脚本确定性生成，LLM 不参与增删，因此数量可以从 Control Set 反算。

## 4. WorkItem Planner

正式 Reviewer WorkItem 按以下键建立：

```text
Control × Surface
```

每个正式 `compliance_review` WorkItem 只携带一个 Control 和一个 Coverage Unit，同时保留：

- `coverage_unit_ids`
- 单个 `control_id`
- `collector_fact_refs`
- `target_hints`
- `allowed_roots`
- 工具轮数、文件数和单文件行数限制

WorkItem 是执行上下文，不改变 Coverage 分母。这样可以并发调度 Reviewer，同时避免一个大模块吞并多个控制项，保留每个 Control × Surface 的完整审查账本。

## 5. Applicability Discovery Barrier

如果画像缺少可由代码或配置验证的事实，Planner 会创建有边界的
discovery work item。Discovery 只能产生技术事实：

- `candidate`：发现线索，但不能证明事实。
- `verified`：由确定性 Collector 或精确代码证据确认。
- `unresolved`：当前证据不足，需要人工确认。

所有 discovery 结果收齐后才允许进行一次 re-evaluation。该步骤不能生成
合规结论、不能扩张 Control 分母，也不能静默取消 Control 已声明的必需证据面。

## 6. Setup 门禁

`ReviewSetupService.compile()` 只有在下面条件同时满足时才继续：

- `setup/app_profile.json` 存在且 `status=confirmed`。
- `setup/controls.json` 存在。
- `setup/control_validation.json` 存在且 `valid=true`。
- 校验记录中的 Control 数量与 `controls.json` 一致。

任一条件不满足都会阻断，不创建 Runtime handoff。

## 7. Runtime Handoff

编译成功后可以直接调用：

```python
setup_result = setup_service.compile(workspace)
summary = review_runtime.run(
    manifest_run_id=setup_result.run_id,
    work_items=setup_result.work_items,
    sandboxes=setup_result.sandboxes,
    output_root=run_store.reviewer_results_root,
)
```

Phase 3 会预创建：

```text
setup/applicability.json
setup/coverage_units.json
runs/<run_id>/manifest.json
runs/<run_id>/reviewer_results/
runs/<run_id>/worker-events.jsonl
runs/<run_id>/checkpoint.sqlite
```

`checkpoint.sqlite` 是 Runtime 的持久化位置占位；真正执行时可用 LangGraph 的 `SqliteSaver` 打开同一个文件，以获得可恢复的线程状态。
