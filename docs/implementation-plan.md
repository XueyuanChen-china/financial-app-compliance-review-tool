# Financial App Compliance Review Tool - 完整实施计划

## 1. 项目定位

本项目是一个面向金融类 App 的、可接入 CI 的增量合规审查工具。它读取已经结构化的法规控制项、App 适用性画像和多个技术证据面，通过确定性事实采集、并行 Agent 调查、确定性覆盖校验与版本快照，回答：

> 当前版本是否引入了新的合规风险，现有证据能否支撑发布，以及哪些结论仍需要后端、平台控制台、监管名单或人工材料补充。

项目不是法律意见生成器，也不把大模型视为最终裁判。第一阶段聚焦研发团队内部的 release readiness 和 compliance regression detection。

## 2. 成功标准

第一版完成后，系统应能够：

1. 加载 5 至 10 个代表性 Controls 和 App Applicability Profile。
2. 对 H5、Android、后端 API 文档和后端代码等已接入证据面建立确定性审查清单。
3. 在没有 LLM 时运行通用 Collectors 并输出结构化事实。
4. 将审查任务拆成相互隔离的 Work Items，并发交给多个 Reviewer Agent。
5. 对每个 `Control x Required Surface` 单独记录证据、缺口和建议状态。
6. 通过确定性 Validator 阻止证据不足的 Control 被直接判为 PASS。
7. 使用单个 Verifier 对可疑、高风险或冲突结果进行定向质检。
8. 生成 Full Baseline Snapshot。
9. 根据 Git Diff 只重审受影响 Controls，并安全复用未受影响结果。
10. 识别 `PASS -> FAIL` 等 Compliance Regression，并输出 Markdown 报告和 CI 状态。

## 3. 核心原则

### 3.1 审查分母固定

系统的覆盖单位始终是：

```text
Control x Required Evidence Surface
```

Work Item 只是上下文与调度容器。即使一个 Work Item 包含多个相关 Controls，也必须逐个 Control、逐个 Surface 产出独立结果。

### 3.2 Agent 不拥有最终 PASS 权限

职责划分如下：

- Code Map Provider 帮助定位代码节点和关系。
- Collector 提取可重复的技术事实。
- Reviewer Agent 调查代码并提出建议。
- Verifier Agent 对疑点进行质检。
- Deterministic Validator 校验事实、证据强度和覆盖完整性。
- Deterministic Resolver 计算最终 Control 状态。
- Coverage Gate 计算当前运行能否结束以及 CI 是否允许通过。

### 3.3 规则、证据、判断和运行状态分离

- Controls 表示要满足什么。
- Evidence Surfaces 表示证据可能在哪里。
- Facts/Evidence 表示实际观察到了什么。
- Review Results 表示 Agent 如何解释证据。
- Snapshots 表示某个版本的最终合规状态。
- Progress/Events 表示执行到了哪里。

### 3.4 不默认生成专用 Scanner

第一版不再采用 `每个 scan.id -> 生成一个 specialized scanner` 的方式。系统优先建设与 Control 解耦的通用 Collectors。只有满足稳定、高频、完全机器可判断的检查，未来才升级为 deterministic check。

## 4. 总体架构

```text
                     Controls
                        +
                Applicability Profile
                        |
                        v
                Parent Orchestrator
                        |
                 Build Work Items
       Coverage Unit: Control x Required Surface
                        |
             +----------+----------+
             |          |          |
             v          v          v
        Reviewer A Reviewer B Reviewer C
             |          |          |
        tools +      tools +    tools +
        collector    collector  collector
        facts        facts      facts
             |          |          |
             +----------+----------+
                        |
                        v
                Structured Results
                        |
                        v
             Deterministic Validator
                 |              |
             validated       suspicious
                 |              |
                 |              v
                 |        Single Verifier
                 |         Targeted QA
                 |              |
                 +-------+------+
                         |
                         v
              Deterministic Resolver
                         |
                  Coverage Gate
                         |
                         v
                Compliance Snapshot
                         |
                  Regression Compare
                         |
                         v
               Report / CI PASS-WARN-BLOCK
```

## 5. 领域模型

### 5.1 Control

每个 Control 至少包含：

```yaml
control_id: CTRL-DPP-001
module_id: data_privacy_and_permissions
title: Explicit consent before registration
severity: high
applicability_expression: self_lending == true
required_surfaces:
  - frontend_h5
  - android_native
minimum_evidence_strength:
  frontend_h5: static_proof
  android_native: static_proof
missing_evidence_policy: block
source_refs:
  - source_id: google_play_personal_loans
    section: Permissions and disclosure
reuse_invalidation_keys:
  - control_version
  - applicability_profile
  - frontend_h5_revision
  - android_native_revision
```

Validator 使用结构化字段，不解析自由文本来决定最终门禁。

### 5.2 Evidence Surface

第一版支持：

- `frontend_h5`
- `android_native`
- `backend_api_doc`
- `backend_code`，未接入时作为显式 gap
- `play_console`，人工或外部证据
- `regulator_external`，人工或外部证据

Surface 只表示证据位置，不表示证据已经存在，也不自动证明 Control 已满足。

### 5.3 Work Item

推荐粒度：

```text
Module x Evidence Surface x Related Controls
```

示例：

```yaml
work_item_id: WI-data-privacy-android-001
module_id: data_privacy_and_permissions
surface: android_native
control_ids:
  - CTRL-DPP-001
  - CTRL-DPP-003
allowed_roots:
  - app/src/main
collector_fact_refs:
  - manifest.permissions
  - android.dependencies
target_hints:
  files:
    - app/src/main/AndroidManifest.xml
  symbols: []
limits:
  max_tool_rounds: 12
  max_files_read: 20
  max_lines_per_read: 300
```

Work Item 完成不等于 Controls 已覆盖。Coverage Manifest 必须逐个验证其中的 Control-Surface rows。

### 5.4 状态模型

四类状态不得混用：

```yaml
execution_status: pending | running | completed | failed
evidence_status: complete | partial | missing | manual_required
control_status: pass | fail | indeterminate | not_applicable | waived
ci_status: pass | warn | block
```

Agent 只输出 `recommended_control_status`，最终 `control_status` 由 Resolver 计算。

## 6. Multi-Agent 设计

### 6.1 Parent Orchestrator

Parent 是 LangGraph 驱动的状态化调度图，不是一个可以自由创建 Agent 网络的自治 Agent。职责包括：

- 加载 Controls、Profile、Snapshot 和 Git Diff。
- 生成 Review Manifest 与 Coverage Manifest。
- 生成 Work Items。
- 通过 `Send` 控制动态 fan-out，通过 checkpoint 支持状态保存和恢复。
- 控制并发数、超时、重试和恢复。
- 为每个 Reviewer 创建独立的 reviewer subgraph 和上下文。
- 聚合结构化结果。
- 调用 Validator、Verifier、Resolver 和 Reporter。
- 记录不可变事件和运行状态。

默认并发数为 3，可通过 LangGraph invocation config 调整，但不允许 Worker 自己继续派生 Agent。

当前实现路径为：

```text
Parent Graph -> Send(Work Item) -> Reviewer Subgraph -> deferred summarize
```

LangGraph checkpoint 负责图状态，JSONL event log 负责不可变审计记录；两者不能互相替代。

### 6.2 并行 Reviewer Agents

Reviewer 是 Multi-Agent 能力的主体。不同 Work Items 可以并发执行；同一个 Work Item 在同一 attempt 内只有一个 Reviewer 拥有写权限。

Reviewer 可以：

- 查看本 Work Item 的 Controls、Collector Facts 和 Code Map candidates。
- 使用受限的 `code_map_query`、`code_map_path`、`get_collector_facts`、`list_files`、`search_code`、`read_file`。
- 跨文件理解同一业务流程。
- 为每个 Control-Surface row 返回 observations、anchors、missing evidence 和 recommended status。

Reviewer 不可以：

- 使用 unrestricted shell。
- 读取旧运行的报告或结论。
- 修改源码、Controls、Snapshot、Coverage 或总报告。
- 声称整个项目已经覆盖完成。
- 将 API 文档提升为 backend implementation proof。

### 6.3 Reviewer Context 与并发边界

每个 Work Item 绑定一个独立 `ReviewerContextState`，同一 Work Item 的多轮模型/工具交互连续进行，
不同 Work Items 之间不共享 messages、tool results、探索历史或压缩工作记忆。

上下文状态分为：

- `immutable_context`：Work Item、required surface、审查指令，永不压缩。
- `evidence_ledger`：可追溯的路径、symbol、行号和工具来源锚点，永不压缩。
- `active_rounds`：默认最近 3 个完整 round。
- `retired_rounds`：被滑出 active window 的完整 round。
- `compressed_memory`：由旧 memory + retired rounds 同步生成的结构化摘要。

每次模型调用前估算上下文使用量。达到 78% 时同步压缩，目标为 60%，硬上限为 90%，最多尝试两次。
压缩不能读取或改写 immutable context、evidence ledger、active rounds。压缩后仍超限或两次失败时，
只将当前 Work Item 标记为 `indeterminate`，错误为 `context_budget_exhausted`，不让父图崩溃。

LangGraph 一次可以 fan-out 全部 Work Items，但运行时 `max_concurrency` 默认是 3；超过 3 个的 Work Items
在 LangGraph 内部等待执行槽位，不额外引入 RabbitMQ、Celery 或其他外部队列。Work Item 终态后释放临时
conversation/context，只保留结构化结果和 evidence anchors。

### 6.4 单 Verifier

Verifier 不并行复审所有 Work Items。Validator 先筛选以下项目：

- 高严重度 Control 推荐 PASS。
- 证据强度刚好达到最低门槛。
- Evidence Facts 与 Reviewer observations 冲突。
- Evidence Anchor 无法重新定位。
- 同一 Control 在不同 Surfaces 上结论冲突。
- Reviewer 置信度较低或存在 unsupported inference。
- `fail` 与 `indeterminate` 边界不清晰。

Verifier 只读取疑点相关的结构化结果与最小代码范围，输出 objection、confirmation 或 correction recommendation。它同样不能直接决定最终状态。

### 6.5 Planner 的处理

第一版不实现 Planner Agent。Parent 根据 Control、Surface、Collector Facts 和 Code Map 查询结果直接生成 Work Item。需要更复杂的范围缩小时，先通过限制 `code_map_query` 的候选数、`read_file` 行数和工具轮次解决。后续如果真实运行数据显示范围规划成为瓶颈，再单独评估 Planner。

## 7. Code Map 与代码读取工具

代码地图和合规事实是两个不同层次：

- Graphify 负责“代码在哪里、代码之间如何关联”。
- Collector 负责“普通程序可以确定性证明哪些事实”。
- Reviewer 负责解释这些代码关系和事实对某个 Control 的意义。

第一版使用本地 Graphify CLI，但只通过项目自己的 `CodeMapProvider` 暴露统一接口：

```python
class CodeMapProvider:
    def query(self, request: CodeMapQuery) -> CodeMapQueryResult:
        ...

    def path(self, request: CodeMapPath) -> CodeMapPathResult:
        ...
```

第一阶段实现 `code_map_query` 和 `code_map_path`，但两者都只通过项目自己的 Provider 暴露给 Reviewer。

Reviewer 的目标工具集合如下：

```text
code_map_query
code_map_path
list_files(root, pattern, limit)
search_code(query, roots, file_globs, limit)
read_file(path, start_line, line_count)
get_collector_facts(collector_id, fact_ids, fact_type, limit)
```

Reviewer 不直接调用 `graphify` CLI。`code_map_query` 和 `code_map_path` 由 `ScopedToolExecutor` 转发到 `GraphifyCodeMapProvider`，并在返回前按当前 Sandbox 和 Work Item 的 allowed roots 过滤候选。

`get_collector_facts` 只读取父流程注入的 `CollectorResult`，不重新扫描代码，也不允许 Reviewer 修改 Facts。不能因为 Graphify 没返回候选，就证明代码不存在；关键 absence 判断仍必须 fallback 到 `search_code`、文件搜索和定向读取。

Graphify Wrapper 的返回必须是紧凑结构，不向 Reviewer 传递完整 graph 或 Graphify Skill：

```json
{
  "status": "available",
  "provider": "graphify",
  "candidates": [
    {
      "symbol": "AccountController.deleteAccount",
      "path": "backend/account/AccountController.kt",
      "start_line": 82,
      "end_line": 96
    }
  ],
  "relations": []
}
```

允许的 Code Map 状态为 `available`、`unavailable`、`degraded`。Graphify 缺失、超时、命令失败或输出无法解析，只能影响代码导航，不能直接生成 PASS、FAIL 或 coverage 结论。

第一版保留以下只读工具约束：

```text
code_map_query(query, max_candidates, budget)
code_map_path(source, target, max_hops, budget)
get_collector_facts(collector_id, fact_ids, fact_type, limit)
list_files(root, pattern, limit)
search_code(query, roots, file_globs, limit)
read_file(path, start_line, line_count)
```

必须实现：

- Root allowlist 和路径规范化。
- 防止 `..` 路径逃逸与 symlink escape。
- 单次读取行数限制。
- 搜索结果数量限制。
- Graphify 候选、关系、路径 hop 和查询 budget 限制。
- Collector Facts 只能从已注入的结果集中读取。
- Work Item 总 tool call 数量限制。
- Work Item 级工具轮次限制。
- 全部调用写入 append-only log。
- 返回内容的 secret redaction。
- 不向 Agent 暴露 `.env`、密钥、签名文件和凭据。

## 8. Generic Evidence Collectors

Collector 不再承担建立整个 Repository Code Map 的职责。代码关系由 Graphify 提供；Collector 只解析稳定、低成本、可重复的 Compliance/Technical Facts。

### 8.1 Android Manifest Collector

提取：

- permissions
- exported components
- intent filters
- network security config
- cleartext traffic settings
- application/component metadata

### 8.2 Dependency/SDK Collector

提取 Android 与前端依赖、版本和可能的 SDK 身份。Collector 只报告声明事实，不直接判断 SDK 合规性。

### 8.3 API Document Collector

只提取 OpenAPI/Swagger JSON 或 YAML 中声明的 endpoint、HTTP method 和 `operationId`。

这个 Collector 只服务于 `backend_api_doc`，输出的最低证据强度是 `server_doc`。它证明“接口文档声明了某个能力”，不能证明后端代码已经实现，也不能证明运行时可达、鉴权或数据库写入。

源码中的路由不再由通用 Collector 跨语言解析。对于 `frontend_h5`、`android_native` 和 `backend_code`：

```text
Graphify 定位候选 controller/router/handler
  -> search_code/read_file 做精确核验
  -> Reviewer 解释业务语义
```

这样避免维护 Python、Java、Kotlin、JavaScript 等框架各自不同的路由正则，同时避免把“出现了路由字符串”误当成运行时行为证明。

字符串或正则分析不能声称完整覆盖。每个 Collector 必须返回：

```yaml
parser_status: ok | fallback | failed
coverage_status: complete | partial | unknown
limitations: []
facts: []
```

### 8.4 后续可选 Collector

- JSBridge and sensitive API collector
- Android WebView configuration collector
- Privacy/terms entry-point collector
- Store listing/manual evidence importer

## 9. Evidence Contract

每条 Evidence Fact 至少包含：

```yaml
fact_id: fact.android.permission.read_contacts
source_surface: android_native
fact_type: android_manifest_permission
file_path: app/src/main/AndroidManifest.xml
symbol: uses-permission
approximate_line: 18
exact_snippet: '<uses-permission android:name="android.permission.READ_CONTACTS" />'
normalized_snippet_hash: sha256:...
file_revision: git-blob-sha
observed_value: android.permission.READ_CONTACTS
parser_status: ok
evidence_strength: static_proof
limitations: []
```

程序必须验证文件、revision、snippet 和 hash。存在重复 snippet 时，应结合 symbol、上下文和 occurrence index 重新定位，不能完全相信 Agent 返回的行号。

## 10. Deterministic Validation and Resolution

Validator 负责：

- 校验结果 schema。
- 校验 Work Item 是否逐个返回了 Control-Surface rows。
- 验证 Evidence Anchor。
- 检测 snippet 与 structured signal 的矛盾。
- 检查 required surfaces 和 minimum evidence strength。
- 检查 backend/manual/regulator evidence gaps。
- 检查 Agent 是否越界使用其他 Surface 的证据。
- 将疑点路由给 Verifier。

Resolver 采用显式规则，例如：

```text
明确反向证据存在                         -> FAIL
必需证据缺失且 missing policy = block     -> INDETERMINATE + CI BLOCK
必需证据缺失且 missing policy = warn      -> INDETERMINATE + CI WARN
全部 required surfaces 有效且无反向证据   -> PASS
不适用                                   -> NOT_APPLICABLE
存在有效人工豁免                          -> WAIVED
```

最终结果不是 Reviewer 与 Verifier 的多数投票结果。

## 11. Coverage Gate

Coverage Manifest 逐行记录：

```yaml
control_id: CTRL-DPP-001
surface: android_native
selection_status: selected
execution_status: completed
evidence_status: complete
result_origin: reviewed
work_item_id: WI-data-privacy-android-001
attempt_id: 1
```

运行只有在所有适用 Control-Surface rows 处于以下终态时才可结束：

- reviewed and resolved
- safely reused
- manual_required
- explicitly blocked
- waived
- not_applicable

Agent 不能自行将 Coverage 标记为完成。

## 12. Full Review、Diff Review 与安全复用

### 12.1 Full Review

Full Review 对当前 MVP Control 集合建立完整 Baseline。这里的“完整”只表示所选择 Controls 的完整覆盖，不代表所有金融法规都已覆盖。

### 12.2 Diff Impact Analysis

第一版采用保守映射：

```text
Changed File
-> Evidence Surface
-> Controls depending on that Surface
-> Invalidate related Control-Surface rows
```

这种方法可能重审过多，但不能漏审。后续再加入 symbol、route 或 dependency 级语义影响分析。

### 12.3 Reuse Fingerprint

只有以下指纹全部未变化时，结果才可复用：

- target repository revision for relevant paths
- Control ID and version
- regulatory source version
- applicability profile hash
- Collector name and version
- reviewer contract/prompt version
- model/provider/runtime configuration
- tool contract version
- required manual/external evidence revision
- validator/resolver rule version

无法证明指纹兼容时，必须重新审查，不能默认复用旧 PASS。

## 13. Snapshot、Regression 和 Report

每次运行生成不可覆盖的新 Snapshot：

```yaml
run_id: run-2026-...
git_revision: ...
mode: full | diff
baseline_run_id: null
contract_versions: {}
model_provenance: {}
applicability_hash: ...
control_results: []
coverage_manifest_ref: ...
reviewed_rows: []
reused_rows: []
missing_surfaces: []
regressions: []
run_status: completed
```

Regression Compare 至少识别：

- `PASS -> FAIL`
- `PASS -> INDETERMINATE`
- 新增高风险权限或 SDK
- 新增 blocking evidence gap
- 旧 Evidence Anchor 失效

报告必须完全从最终 Snapshot 和 Coverage Manifest 派生，不能直接拼接 Agent 自由文本。

## 14. 文件状态与恢复

第一版使用文件而不是数据库或消息队列：

```text
runs/<run_id>/
  run_manifest.yaml
  events.jsonl
  progress.json
  collector_facts/
  workitems/
    <work_item_id>/
      attempts/<attempt_id>/
        request.yaml
        tool_calls.jsonl
        reviewer_result.yaml
        validation_result.yaml
  verifier/
  coverage_manifest.yaml
  control_results.yaml
  snapshot.yaml
  report.md
```

重试必须创建新的 attempt，不覆盖旧产物。`events.jsonl` 采用 append-only 方式记录状态变化。恢复时从已通过 schema 和 anchor 校验的终态 Work Items 继续。

## 15. 安全边界

- 所有 Agent 工具只读。
- 目标仓库不得被 Agent 修改。
- API key 只能来自环境变量或系统凭据存储。
- 日志和模型输入必须做 secrets redaction。
- 不调用可能写数据库或改变业务状态的后端 API。
- 后端 API 文档只能证明声明能力，不能代替后端代码或运行态证据。
- 原始代码、政策文件和证据不得默认上传到非配置模型提供方。
- 报告必须标注已纳入和缺失的 Evidence Surfaces。

## 16. Python 工程结构

目标结构：

```text
src/compliance_review/
  cli.py
  config/
  domain/
    controls.py
    evidence.py
    workitems.py
    results.py
    snapshots.py
  collectors/
    base.py
    android_manifest.py
    dependencies.py
    api_documents.py
  code_map/
    models.py
    provider.py
    graphify.py
  repository/
    sandbox.py
    git.py
    tools.py
  orchestration/
    manifest_builder.py
    scheduler.py
    reviewer_worker.py
    verifier.py
    retry.py
  validation/
    schemas.py
    anchors.py
    evidence_rules.py
    resolver.py
    coverage.py
  incremental/
    impact.py
    fingerprints.py
    regression.py
  reporting/
    markdown.py
    ci.py
  storage/
    files.py
    events.py
tests/
  unit/
  integration/
  fixtures/
docs/
examples/
```

领域模型和确定性逻辑不得依赖具体 LLM SDK。模型提供方通过 adapter 接入，以便测试时使用 fake provider。

## 17. 七天开发计划

计划保持较满，但每天必须留下可运行、可测试的纵向切片。

### Day 1 - Domain Contracts and Project Foundation

- 固定 Control、Surface、Fact、Evidence、Work Item、Result、Snapshot 模型。
- 固定四层状态词表。
- 固定 5 至 10 个 MVP Controls。
- 完成配置加载、schema 校验和 CLI 骨架。
- 建立 sample profile 和 control fixtures。

验收：错误 schema 会被确定性拒绝，示例配置可以加载。

### Day 2 - Graphify Code Map and Collectors

- 实现 target root sandbox、Git metadata 和只读工具。
- 实现 `CodeMapProvider` 接口和 `GraphifyCodeMapProvider`。
- 先跑通 `code_map_query`，只返回 Top 3-5 个候选节点和紧凑关系。
- 保留 `search_code`、`read_file` 作为 fallback 和 exact verification。
- 再实现 Manifest、Dependency、API Document Collector；源码路由由 Graphify + 只读工具 + Reviewer 处理，不再维护跨语言源码路由 Collector。
- 输出 parser status、coverage status、limitations 和 facts。
- 建立 Graphify unavailable/degraded 和 Collector parser success/fallback/failure fixtures。

验收：Graphify 缺失时审查链路仍能得到结构化 degraded 状态；无 LLM 时 Collectors 仍能稳定输出相同 Facts。

### Day 3 - Parallel Reviewer Pipeline

- 已实现 Review Manifest 和 Work Item builder，按 `module x evidence_surface` 拆分。
- 已实现受控并发 Scheduler，默认并发 3。
- 已接入 `OpenAICompatibleProvider` 和 deterministic `StaticModelProvider`。
- 已实现 Reviewer 结构化输出、独立 context fingerprint 和独立结果目录。
- 已实现只读 tool-call、文件读取边界和 token budget。
- 已实现 append-only JSONL worker events。

验收已通过：3 个 Work Items 可并行执行，结果目录和 context fingerprint 互相隔离，工具越界读取会被拒绝。

### Day 4 - Validation, Verifier and Full Review

- 实现 result schema、anchor 和 evidence consistency validation。
- 实现 suspicious routing。
- 实现单 Verifier 的 targeted QA。
- 实现 deterministic Resolver 与 Coverage Gate。
- 跑通 Full Review、Snapshot 和 Markdown Report。

验收：Reviewer 推荐 PASS 但必需 backend evidence 缺失时，最终不得 PASS。

### Day 5 - Diff Review and Safe Reuse

- 实现 Git Diff、file-to-surface mapping 和 affected controls。
- 实现 reuse fingerprint。
- 实现 reviewed/reused rows 合并。
- 实现 compliance regression comparison。
- 跑通 Manifest 新增敏感权限的演示。

验收：受影响 Controls 被重审，其他结果仅在指纹兼容时复用。

### Day 6 - Reliability and CI

- 实现 timeout、retry、resume 和 failed work item handling。
- 实现 attempt history，不覆盖旧产物。
- 补 anchor relocation、secret redaction 和 path escape 测试。
- 实现 CI exit code 与 PASS/WARN/BLOCK。
- 补高风险异常路径测试。

验收：中断后可恢复；失败 Worker 不会被误当作 Control FAIL 或覆盖完成。

### Day 7 - Test, Demo and Documentation

- 完成单元、集成和端到端测试。
- 固定可复现 sample app/repository fixture。
- 完成三个演示场景。
- 完善 README、架构说明和设计取舍。
- 可选增加 GitHub Actions 示例。
- 冻结第一版范围，不继续扩张架构。

## 18. 演示场景

### Demo 1 - Full Baseline

对示例金融 App 运行完整审查，展示 Collector Facts、并行 Reviewer、Coverage、Snapshot 和 CI 结果。

### Demo 2 - Compliance Regression

在 AndroidManifest 中新增 `READ_CONTACTS`：

```text
Git Diff
-> android_native affected
-> related Controls invalidated
-> re-review
-> previous PASS becomes FAIL
-> CI BLOCK
```

未受影响 Controls 显示为安全 REUSED。

### Demo 3 - Evidence Insufficient

设计一个需要 backend code evidence 的 Control，但只提供 API 文档。即使 Reviewer 推荐 PASS，Validator 也必须产生 `indeterminate`，CI 根据 Control policy 输出 WARN 或 BLOCK。

## 19. 测试策略

### 单元测试

- Pydantic/schema validation
- applicability evaluation
- collector parsing and fallback
- path sandbox and symlink escape
- anchor relocation
- evidence strength comparison
- resolver decision table
- coverage completeness
- fingerprint compatibility
- regression transition

### 集成测试

- Collector -> Facts -> Work Item
- Reviewer adapter -> result validation
- suspicious result -> Verifier
- reviewed + reused -> Snapshot
- Snapshot -> deterministic report
- interrupted run -> resume

### Agent 测试

- 使用固定代码 fixture 和 fake model response 保证基础测试确定性。
- 对真实模型执行单独的 evaluation suite，不把随机模型输出当普通单元测试。
- 统计 unsupported conclusions、missing anchors、tool-limit violations 和 false PASS。

## 20. 风险与缓解措施

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| Reviewer 选择性漏读 | 漏报 | 确定性 Work Items、Control-Surface receipt、Coverage Gate |
| Agent 返回错误位置 | 证据不可追溯 | snippet hash、revision、anchor relocation |
| 多 Agent 结论不一致 | 状态漂移 | 统一 schema、单 Verifier、确定性 Resolver |
| 旧 PASS 被错误复用 | 严重回归漏报 | 完整 reuse fingerprint，默认保守失效 |
| Collector 正则漏检 | 错误宣称完整 | parser/coverage status 和 limitations |
| 上下文过大 | Agent 偷懒或超限 | Work Item 隔离、读取限制、独立 Context |
| 多 Agent 成本高 | 延迟和预算增加 | Reviewer 并行，Verifier 按需执行 |
| 敏感源码或密钥泄露 | 安全风险 | 只读 sandbox、redaction、provider policy |
| 一周范围过满 | 质量下降 | 每日纵向验收，优先守住核心闭环 |

## 21. 第一阶段明确不做

- 不为每个 scan.id 自动生成 specialized scanner。
- 不建设大型 AST、call graph 或 taint analysis 平台。
- 不做动态设备测试和真实敏感 API 调用。
- 不做政策抓取、OCR、向量数据库或完整 RAG。
- 不做自由协作的 Agent 网络。
- 不做数据库、消息队列、Kubernetes 或 Web 管理后台。
- 不生成复杂 PDF 报告。
- 不声称替代律师、监管机构或人工发布审批。

## 22. 后续演进

第一版稳定后再评估：

1. 将高频稳定检查升级为 deterministic checks。
2. 增加 backend code、Play Console 和 regulator evidence adapters。
3. 增加语义级 Diff Impact Analysis。
4. 增加模型质量评估、成本监控和 prompt/version registry。
5. 将文件状态替换成持久化任务队列，但保持领域合同不变。
6. 将稳定流程封装为可复用 CLI、CI Action 或 Codex plugin。

## 23. 开发启动决策

当前正式采用以下路线：

```text
Graphify Code Map + Generic Collectors
-> Parallel Reviewer Agents
-> Deterministic Validation
-> Single Targeted Verifier
-> Deterministic Resolution and Coverage
-> Snapshot, Regression and CI
```

Multi-Agent 的价值体现在并行调查、上下文隔离和定向质检，而不是让多个 Agent 投票决定合规结论。
