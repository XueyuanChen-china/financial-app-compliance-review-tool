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
  deterministic CoverageUnit impact analysis
              |
      +-------+--------+
      |                |
   re-review       exact safe reuse
      |                |
      +-------+--------+
              v
   Resolver + Coverage Gate + Regression + CI
```

`CoverageUnit = Control x Required Surface` 仍是完整性和 reuse 的最小单位；`WorkItem` 只负责执行打包。程序而不是 Agent 决定 diff、影响范围、reuse 和 regression。

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

不包含时间、run ID 或随机值。MVP 只复用 previous run 中 `valid + complete evidence + PASS + not suspicious` 的行；invalid、partial、indeterminate、blocked、unknown、manual 或未解决 verifier 行都会回到 re-review。

## 4. 合并与回归

Diff Review 只对需要重审的 WorkItem 调用 Reviewer Runtime。`merge_validations()` 将新行与可信旧行合成完整当前账本，旧行显式标记 `result_origin=reused` 并记录 `previous_run_id`，之后仍进入原有 Resolver 和 CoverageGate。

Snapshot 和报告分别记录 Reviewed / Reused 数量。Regression 比较完全确定性：

- PASS -> FAIL：regression，CI BLOCK。
- PASS -> INDETERMINATE：由 `missing_evidence_policy` 决定 WARN 或 BLOCK。
- FAIL -> PASS：improvement，不作为阻断回归。

## 5. 运行产物

`compliance-review diff-review` 除普通 review 产物外还会持久化：

```text
runs/<run-id>/diff.json
runs/<run-id>/impact.json
runs/<run-id>/reuse-plan.json
runs/<run-id>/regressions.json
```

## 6. READ_CONTACTS 端到端示例

测试首先让 AndroidManifest 没有 `READ_CONTACTS`，对权限 Control 得到 PASS baseline；随后只修改 Android Manifest 新增该权限。真实 Git diff 会使 Android CoverageUnit affected，Runtime 只重审该单元，未变化的 frontend CoverageUnit 通过相同 fingerprint 复用。权限结果 PASS -> FAIL，最终 CI 为 BLOCK。

## 7. 当前限制

- 当前 MVP 采用 repository/surface 的保守失效：一个仓库 surface 有变更，会重审该 surface 的相关 CoverageUnit；不做 AST/dataflow 精细化影响分析。
- MVP 仅复用可信 PASS（以及确定性 not-applicable 终态），不复用 FAIL。
- 需要已有 completed baseline Snapshot 以及其 `result_validation.json`；缺失时会 fail closed，而不会默认 reuse。
