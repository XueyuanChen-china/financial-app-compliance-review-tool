# Day 4 学习与实现记录：Validation、Verifier 与 Full Review

## 1. Day 4 目标

Day 4 把 Reviewer 的“建议”接入确定性终判链路：

```text
Parallel Reviewer Results
          |
          v
Result + Anchor Validation
          |
          v
Suspicious Router
          |
          v
Single Targeted Verifier
          |
          v
Deterministic Resolver
          |
          v
Coverage Gate
          |
          v
Snapshot + Markdown Report + CI Status
```

最重要的边界是：Reviewer 和 Verifier 都不能直接决定最终 CI 状态。它们输出结构化建议，普通 Python 代码负责验证证据、处理缺口和计算最终状态。

## 2. Evidence Anchor

Reviewer 使用只读工具后，Runtime 会把工具结果转成 `EvidenceAnchor`：

- `anchor_id`：稳定标识。
- `source_surface`：证据来自哪个 surface。
- `path`、行号和 symbol：定位候选代码。
- `exact_snippet` 与标准化哈希：验证当前文件仍包含该片段。
- `file_revision`：记录实际读取文件 bytes 的 Git-blob 风格内容 revision；dirty、untracked 或非 Git 文件也可验证。
- `evidence_strength`：声明、静态证明、服务端文档、服务端代码等强度。
- `fact_ids`：关联 Collector 的确定性 Facts。

Graphify 候选节点只是 `behavioral_hint`。只有 `read_file`、精确搜索结果或 Collector Facts 能进一步支撑更强结论。

## 3. Result Validator

`ResultValidator` 按 Coverage Unit 验证 Reviewer 结果，而不是只检查 JSON 能否解析。主要检查：

- 每个 `Control x Required Surface` 是否有对应结果。
- 必需 surface 是否存在。
- PASS 是否具备 complete evidence。
- 实际证据强度是否达到 Control 门槛。
- anchor 是否属于正确 Control 和 surface。
- anchor 路径是否仍在 Repository Sandbox 内。
- exact snippet 和哈希是否仍与当前代码一致。
- Reviewer row 是否精确属于分配的 Work Item、Control 和 surface。
- Collector Fact 是否在 Work Item capability 内，且 parser/coverage 状态、surface、source path 和 strength 一致。
- 是否存在低置信度、unsupported inference 或跨 surface 冲突。

无效证据不会被悄悄丢弃，而是形成带 code 的 `ValidationIssue`。

## 4. Suspicious Routing 与单 Verifier

并非所有结果都再次调用模型。`SuspiciousRouter` 只挑出：

- 无效或缺失的 coverage row。
- critical/high Control 的 PASS。
- 刚好达到最低证据阈值的 PASS。
- 低置信度或 unsupported inference。
- 跨 surface 冲突。

所有 suspicious rows 合并为一次结构化 Verifier 请求。Verifier 不获得工具，只能检查已提供的结构化证据和 Validator 问题，并返回 `confirm / object / correction`。

Verifier 返回不完整、调用失败、anchor 集合不一致、`confirm` 与原状态矛盾，或反对原结论时，整批 QA 都不能授权 suspicious PASS。`correction` 在 Day 4 只作为保守建议持久化，不允许创建 PASS、WAIVED 或 NOT_APPLICABLE。

## 5. Deterministic Resolver

Resolver 的核心顺序是：

1. 适用性为 false，输出 `not_applicable`。
2. 必需 surface 缺失或适用性未知，输出 `indeterminate`。
3. 有完整且有效的明确反向证据，输出 `fail`。
4. evidence 不完整、anchor 无效或 targeted QA 未确认，输出 `indeterminate`。
5. 所有必需 surface 都有完整有效证据，且 suspicious rows 已确认，才输出 `pass`。

因此，Reviewer 即使对 frontend 给出 PASS，只要 Control 还要求 backend evidence 且 backend repository 缺失，最终仍是 `indeterminate`。

## 6. Coverage Gate 与 CI

Coverage Gate 为每个 Coverage Unit 生成一行账本，并把 Control 结果映射为 CI：

- `fail`：`BLOCK`。
- `indeterminate + missing_evidence_policy=block`：`BLOCK`。
- `indeterminate + missing_evidence_policy=warn`：`WARN`。
- 所有适用 Control 均通过：`PASS`。

Coverage Manifest 的终态来源包括：有效审查 `reviewed`、等待人工证据 `manual_required`、显式缺口 `blocked` 和确定性不适用 `not_applicable`。缺失 surface 或 manual gap 也可形成显式终态，因此账本可以是 complete，但 CI 仍然是 WARN/BLOCK。这两个概念不能混为一谈：

```text
coverage ledger complete != compliance pass
```

## 7. Full Review 产物

`compliance-review full-review` 会写入：

```text
runs/<run_id>/review_summary.json
runs/<run_id>/result_validation.json
runs/<run_id>/suspicious_rows.json
runs/<run_id>/verifier/verifier_result.json
runs/<run_id>/control_results.json
runs/<run_id>/coverage_manifest.json
runs/<run_id>/snapshot.json
runs/<run_id>/report.md
```

Markdown Report 只从 `snapshot.json` 和 `coverage_manifest.json` 派生，不重新读取原始 Agent prose，避免报告层产生第二套判断口径。

## 8. 验收用例

本阶段测试固定了关键安全性质：

```text
Reviewer recommends PASS on frontend_h5
+ Control requires backend_code
+ backend_code is missing
= final Control status INDETERMINATE
+ CI status BLOCK
```

还覆盖了跨 Work Item 越权结果、Collector Fact capability/provenance、dirty-file anchor 漂移、矛盾 Verifier 输出、manual-required/not-applicable 终态，以及 Full Review 正式产物写入。

## 9. 有意延后的能力

- `waived` 仍是结果合同中的合法终态，但 Day 4 没有可信人工豁免输入、审批人、有效期或签名合同；Reviewer 和 Verifier 都不能自行创建 waiver。
- anchor relocation、retry/resume、failed Work Item 恢复和 CI process exit code 属于 Day 6。
- diff impact、safe reuse 和 regression comparison 属于 Day 5。
