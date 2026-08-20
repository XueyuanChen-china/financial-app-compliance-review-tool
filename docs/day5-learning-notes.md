# Day 5 学习与实现记录：Diff Review 与 Safe Reuse

## 1. 目标

Day 5 在既有 Full Review 上增加可信的增量审查，而不是把“文件没变”直接当成安全结论：

```text
Previous Snapshot + Current repositories
              |
              v
       repository-level Git diff
              |
              v
  parallel bounded CoverageUnit impact analysis
              |
      +-------+--------+
      |                |
   re-review       exact safe reuse
      |                |
      +-------+--------+
              v
   Resolver + Coverage Gate + Regression + CI
```

`CoverageUnit = Control x Required Surface` 仍是完整性和 reuse 的最小单位；每个 Reviewer WorkItem 只绑定一个 CoverageUnit。Git hunk、输入基线和 Validator 是权威安全边界；Impact Agent 只在候选范围内提供 `affected/unaffected` 的语义判断，最多三项并行。

## 2. Git Diff 与 Surface 映射

`GitRepository.diff()` 对每个 workspace repository 单独输出 `RepositoryDiff`：

- `repo_id`、base/head revision、可比较状态。
- add、modify、delete 和 rename 文件变更。
- dirty worktree 和未跟踪文件也纳入当前输入。

变更文件根据 `RepositoryInventory` 的确认 surface 映射为 `(repo_id, surface, path)`。系统不会把多个同 surface 仓库压缩成一个没有身份的 changed-surface 集合。

如果 base revision 不存在、Git 无法确认，或 repository 没有可靠 surface，则对应范围 fail closed：不能 reuse，必须重新审查。

## 3. Safe Reuse Fingerprint

每个 CoverageUnit 使用 canonical JSON (`sort_keys=True`) 和 SHA-256 生成稳定 fingerprint。输入包括：

- CoverageUnit identity、required surface 和最低证据强度。
- 当前完整 Control（含 evidence requirement 和 `reuse_invalidation_keys`）。
- confirmed App Profile 和 applicability 结果。
- 相关 `(repo_id, surface, revision, repository content fingerprint)`。
- 当前 surface 的 Collector Facts。

不包含时间、run ID 或随机值。若输入语义和代码影响均未变化，Diff Review 会继承此前所有已验证的终态，包括 PASS、FAIL、partial 和 indeterminate；继承行带 `result_origin=carried_forward`、直接 `previous_run_id` 及原始 `result_origin_run_id`，不会把旧阻断信息误写成通过。

## 4. 合并与回归

Diff Review 会先校验 Full 基线和当前 `sources / obligations / controls / app profile / applicability / workspace mapping / API 与外部材料` 的输入指纹。非代码输入变化直接要求 Full Review。通过预检后，`merge_validations()` 将新行与继承行合成完整当前账本；之后仍进入同一套 Resolver 和 CoverageGate。

Snapshot 和报告分别记录 Reviewed / Reused 数量。Regression 比较完全确定性：

- PASS -> FAIL：regression，CI BLOCK。
- PASS -> INDETERMINATE：由 `missing_evidence_policy` 决定 WARN 或 BLOCK。
- FAIL -> PASS：improvement，不作为阻断回归。

## 5. 运行产物

`compliance-review diff-review` 除普通 review 产物外还会持久化：

```text
runs/<run-id>/diff/preflight.json
runs/<run-id>/diff/diff.json
runs/<run-id>/diff/code-states.json
runs/<run-id>/diff/graphify-indexes.json
runs/<run-id>/diff/impact-work-items.json
runs/<run-id>/diff/impact-decisions.json
runs/<run-id>/diff/impact-validation.json
runs/<run-id>/diff/execution-plan.json
runs/<run-id>/diff/carried-forward-lineage.json
runs/<run-id>/regressions.json
```

## 6. READ_CONTACTS 端到端示例

测试首先让 AndroidManifest 没有 `READ_CONTACTS`，对权限 Control 得到 PASS baseline；随后只修改 Android Manifest 新增该权限。真实 Git diff 会使 Android CoverageUnit affected，Runtime 只重审该单元，未变化的 frontend CoverageUnit 通过相同 fingerprint 复用。权限结果 PASS -> FAIL，最终 CI 为 BLOCK。

## 7. 当前限制

- Impact Agent 的 `unaffected` 只是候选结论；缺少唯一结果、工具失败、响应不合法或与基线 Anchor 的 Git hunk 直接重叠时，Validator 一律改为 `affected`。
- Graphify 是导航缓存。若已有索引过期，Diff 会先尝试不安装地重建；重建失败时仍可使用受限 search/read fallback，但不能把 Graphify 的旧结果当作依据。
- 需要已有 completed baseline Snapshot、`result_validation.json` 和 `review-input-baseline.json`；缺少任一项都会要求新的 Full Review。
