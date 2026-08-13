# Phase 3：Applicability、Coverage 与 Runtime Handoff

## 1. 本阶段解决什么问题

Phase 2 产出的是已验证的 Control Set，但 Reviewer 还需要知道：

1. 当前 AppProfile 下哪些 Control 适用。
2. 每个适用 Control 需要检查哪些 evidence surface。
3. 哪些 Control × surface 组合要交给哪个 WorkItem。
4. 如何把这些 WorkItems 安全交给现有 LangGraphReviewRuntime。

本阶段只做确定性规划，不让 LLM 改写 Coverage 分母，也不重写 Reviewer Parent/Subgraph。

## 2. Applicability Engine

`ApplicabilityEngine` 复用有限 DSL，不使用 Python `eval()`。当前支持：

```text
field == value
field includes value
value in field
多个条件使用 and 或 && 连接
```

每个 Control 都会生成一个决策：

- `true`：进入 Coverage Unit 生成。
- `false`：进入 `excluded_control_ids`，并记录排除原因。
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

WorkItem 按以下键分组：

```text
Module × Surface
```

一个 WorkItem 可以携带多个 Control，但它会保留：

- `coverage_unit_ids`
- `control_ids`
- `collector_fact_refs`
- `target_hints`
- `allowed_roots`
- 工具轮数、文件数和单文件行数限制

WorkItem 是执行上下文，不改变 Coverage 分母。这样可以并发调度 Reviewer，同时保留每个 Control × surface 的完整审查账本。

## 5. Setup 门禁

`ReviewSetupService.compile()` 只有在下面条件同时满足时才继续：

- `setup/app_profile.json` 存在且 `status=confirmed`。
- `setup/controls.json` 存在。
- `setup/control_validation.json` 存在且 `valid=true`。
- 校验记录中的 Control 数量与 `controls.json` 一致。

任一条件不满足都会阻断，不创建 Runtime handoff。

## 6. Runtime Handoff

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
