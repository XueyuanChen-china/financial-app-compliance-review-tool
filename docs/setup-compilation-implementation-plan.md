# Review Setup 与 Compliance Compilation 三阶段实施计划

## 1. 目标与边界

当前项目已经具备一条可运行的执行链：

```text
WorkItems
  -> LangGraph Parent Graph
  -> Reviewer Subgraph
  -> Structured Review Results
  -> Run Summary
```

但上游还缺少稳定的 Review Setup / Compliance Compilation 阶段，无法从用户选择的代码仓库和政策材料自动构建可靠的：

```text
AppProfile
Obligations
Controls
CoverageUnits
WorkItems
```

本计划只补齐这些准备能力，并保持现有 `LangGraphReviewRuntime` 的职责不变。

核心边界：

```text
Setup / Compilation Layer
= 决定审什么、为什么审、需要哪些证据面

LangGraph Review Runtime
= 执行已经规划好的 WorkItems
```

本计划暂不实现 Validator、Verifier、Resolver、Snapshot、Diff Review、最终 Report 和复杂 Human-in-the-loop UI，只为它们预留清晰接口。

## 2. 设计原则

### 2.1 Agent 与确定性代码分工

```text
Profile Agent Subgraph
= bounded Agent Loop，用于探索仓库并形成画像草稿

Obligation Extraction
= structured LLM call

Control Synthesis
= structured LLM call

Validation / Applicability / Coverage / Planning / Persistence
= deterministic Python
```

只有需要迭代探索证据时才使用 Agent Loop。已经确定输入和输出边界的语义转换，不扩展成多 Agent 流程。

### 2.2 事实、推断和确认必须分开

重要 AppProfile 字段不能只保存裸值，至少要能表达：

```text
value
source: declared | deterministic | inferred | human_confirmed | unresolved
confidence
evidence references
```

确定性 Collector 产生的事实是 authoritative technical facts，Profile Agent 可以读取但不能重写。代码无法证明的业务信息必须允许 `unknown` 或 `unresolved`，不能强迫 Agent 猜测。

### 2.3 源码只读，产物由应用写入

Repository、政策原件和外部输入与生成的 Workspace artifacts 分离：

- Agent 只能使用受控只读工具。
- Agent 不获得 `write_file`、`delete_file`、shell 或 git commit 权限。
- Agent 只返回结构化对象。
- 应用层校验对象后，通过 `ArtifactStore` 原子写入 Workspace。

### 2.4 Source → Obligation → Control 不可跳过

政策材料不能直接由一个自由 Prompt 生成 Controls。每条 Control 都要能追溯到 Obligation，再追溯到原始 Source section/chunk。

## 3. 目标主链路

```text
User Input
   |
   +--> Repositories --> Repository Inventory --> Deterministic App Facts
   |                                             |
   |                                             v
   |                                      Profile Agent Subgraph
   |                                             |
   |                                      Profile Draft
   |                                             |
   |                                      Profile Validator
   |                                             |
   |                                      Human Confirmation
   |
   +--> Compliance Materials --> Source Registry
                                      |
                                      v
                              Obligation Extraction
                                      |
                                      v
                               Control Synthesis
                                      |
                                      v
                               Control Validator
                                      |
                                      +-------------------+
                                                          v
                         Confirmed AppProfile + Validated Controls
                                                          |
                                                          v
                                                  Applicability Engine
                                                          |
                                                          v
                                                    Coverage Units
                                                          |
                                                          v
                                                   WorkItem Planner
                                                          |
                         ===== Existing boundary: Review Runtime =====
                                                          |
                                            LangGraphReviewRuntime.run()
```

## 4. 当前状态与缺口

当前已有并应复用：

- Pydantic 领域模型：`WorkItem`、`Control`、`Fact`、`Evidence`、`ReviewResult` 等。
- Android Manifest、Dependency、API Document 等 Collector 基础。
- `RepositorySandbox`、只读工具、Graphify `CodeMapProvider`。
- `Applicability` 的有限声明式判断能力。
- `ReviewManifestBuilder` / WorkItem 生成基础。
- LangGraph Parent Graph、Reviewer Subgraph、并发限制和 ReviewerContextState。

当前主要缺口：

- 没有统一的 `ComplianceWorkspace` 和 `workspace.json`。
- 没有统一的 `ArtifactStore` 和 Workspace/Run 两级写入边界。
- 没有 Repository Inventory 与 Surface Detection。
- 现有 App Profile 需要扩展 provenance 和 unresolved 表达。
- 没有 Profile Agent Loop 与 Profile Validator。
- 没有 Source Registry、Obligation Extraction、Control Synthesis 的完整编译服务。
- 没有把 `ConfirmedAppProfile + Validated Control Set` 稳定交给 Applicability、Coverage 和 WorkItem Planner 的 Setup Service。

## 5. 三阶段计划

## Phase 1：Workspace、Repository Intake 与 App Profile

### 目标

先让系统能够初始化一个长期存在的 Compliance Workspace，登记多个仓库，确定每个仓库的 surface，并生成带来源的 AppProfile draft。此阶段不编译 Controls，也不执行正式 Review。

### 主要实现

1. 新增 `ComplianceWorkspace` 与 `workspace.v1` 配置模型。

   - workspace root
   - repositories
   - materials
   - schema version
   - repository 的 `repo_id`、路径、可选 `declared_surface`
2. 新增 `RepositoryInventory`。

   - 规范化仓库路径和 Git metadata。
   - 识别 Android、Web/H5、backend、API document 等 deterministic signals。
   - 记录 `declared_surface`、`detected_surface`、`surface_status`。
   - 声明与检测冲突时输出 `unresolved`，禁止静默覆盖用户声明。
3. 复用现有 Collectors 生成 `AppFactSet`。

   - 平台和仓库 surface。
   - Android Manifest permissions。
   - dependency/SDK。
   - API docs availability。
   - frontend framework。
   - backend presence。
4. 扩展 AppProfile 结构，增加字段级 provenance。

   - 支持 `declared`、`deterministic`、`inferred`、`human_confirmed`、`unresolved`。
   - 允许 `unknown` / `null`，不强迫模型猜测 jurisdiction、license、外部 backend 等代码无法证明的信息。
5. 新增 bounded `Profile Agent` LangGraph 子图。

   - 只能读取 inventory、AppFacts 和当前仓库的受控只读工具。
   - 可以调用 `code_map_query`、`code_map_path`、`search_code`、`read_file`、`list_files`。
   - 只能返回 `AppProfileDraft`，不能直接写文件。
   - 使用现有 Provider/Tool Runtime，不重新实现 Reviewer Runtime。
6. 新增 deterministic `ProfileValidator`。

   - Pydantic schema/enum/required fields。
   - 字段 provenance 合法性。
   - evidence reference 格式。
   - declared 与 deterministic facts 的明显冲突。
7. 增加人工确认接口状态，不急于实现复杂 UI。

   - `draft`：Agent 产物，可修改。
   - `awaiting_confirmation`：缺少关键业务输入。
   - `confirmed`：用户确认后才能作为正式 Review 输入。

### 产物

```text
workspace.json
setup/repository_inventory.json
setup/app_facts.json
setup/app_profile_draft.json
setup/app_profile_confirmation.json
setup/app_profile.json       # 只有确认后产生
```

### 验收标准

- `compliance-review init <workspace>` 能创建 Workspace。
- 多仓库可以分别登记和检测 surface。
- 声明与检测冲突不会被 LLM 静默解决。
- Profile Agent 无法证明的字段输出 `unresolved`。
- Agent 没有任意写文件能力。
- 无 LLM 时，Repository Inventory 和 AppFacts 仍能确定性运行。

## Phase 2：Source Registry、Obligation 与 Control Compilation

### 目标

将政策材料编译成可追溯的 Obligations 和 Controls，建立稳定的低频规则基线。此阶段仍不启动 Reviewer Agent。

### 主要实现

1. 新增 `ComplianceSource` 和 Source Registry。

   - `source_id`、路径、标题、版本、hash。
   - 支持 source sections/chunks。
   - MVP 先支持 `.md`、`.txt`，PDF/DOCX 不作为本阶段核心阻塞点。
   - 保留原始文件 provenance，不把来源文本复制成不可追溯的自由文本。
2. 新增 `ObligationExtractor`。

   - 输入确定的 Source sections/chunks。
   - 使用 structured LLM call，不使用 Agent Loop。
   - 输出 `Obligation[]`。
   - 每条 Obligation 必须包含 `source_id`、`source_section`、statement、concepts。
3. 新增 `ControlCompiler`。

   - 输入 Obligation[]。
   - 使用第二次 structured LLM call 生成 `ControlDraft[]`。
   - 保留 `obligation_ids`、source provenance、applicability、required surfaces、evidence requirements。
   - 不允许直接从 Source 一步生成 Control。
4. 新增 deterministic `ControlValidator`。

   - Control ID 唯一。
   - Obligation references 存在。
   - Source references 存在。
   - required surface 是合法 enum。
   - applicability expression 符合有限 DSL。
   - evidence requirements 非空。
   - schema/version 正确。
   - 检测明显重复 Control。
5. 规则基线的状态分开记录。

   - `draft`：LLM 尚未通过校验。
   - `validated`：通过 deterministic validator 的 Control Set。
   - 可选后续人工审批，不在本阶段实现 Debate Loop。

### 产物

```text
setup/sources.json
setup/obligations.json
setup/controls_draft.json
setup/controls.json
```

### 验收标准

- 能从 `.md`/`.txt` 材料生成可追溯的 Source Registry。
- 每条 Control 都能沿 `source -> obligation -> control` 回溯。
- 无效引用、重复 ID、非法 surface 和非法 applicability 被确定性拒绝。
- Obligation Extraction 和 Control Synthesis 都是结构化调用，不会自行探索仓库。
- Control 编译失败不会生成看似有效的 validated Control Set。

## Phase 3：Applicability、Coverage、WorkItem Planning 与 Runtime Handoff

### 目标

将已确认的 AppProfile 和已验证的 Control Set 编译成 Coverage Units 和 WorkItems，并无缝交给现有 `LangGraphReviewRuntime`。本阶段不重写 Reviewer Parent/Subgraph。

### 主要实现

1. 收紧 Setup Service 的输入门禁。

   - 必须存在 confirmed AppProfile。
   - 必须存在 validated Control Set。
   - Profile 未确认或 Control 校验失败时，不进入 Review Runtime。
2. 复用并完善 structured Applicability Engine。

   - `TRUE`：生成 applicable Control。
   - `FALSE`：记录 excluded Control 和原因。
   - `UNKNOWN`：保守保留，不因无法判断而漏审。
   - 禁止 Python `eval()`，使用有限的 typed condition tree。
   - 旧版字符串条件仅由 migration adapter 读取；无法无损转换时保留为 `unknown`。
3. 新增 deterministic `CoverageUnitBuilder`。

   - 每个 applicable Control 与其 required surfaces 做笛卡尔展开。
   - Coverage Unit 固定为 `Control × Required Surface`。
   - 不允许 LLM 生成或删减 Coverage Units。
   - 明确记录 excluded、unknown、missing surface。
4. 新增或收敛 `WorkItemPlanner`。

   - 按 `Control × Surface` 建立正式 Reviewer WorkItem。
   - WorkItem 负责执行上下文和调度，不改变 Coverage 分母。
   - 一个正式 WorkItem 只包含一个 Control 和一个 Coverage Unit。
   - 为每个 WorkItem 写入 allowed roots、collector fact refs、target hints 和 limits。
5. 新增 `ReviewSetupService.compile()`。

   - inventory repositories。
   - collect deterministic AppFacts。
   - 读取/确认 AppProfile。
   - 读取 validated Controls。
   - applicability。
   - coverage units。
   - WorkItem planning。
   - 持久化 setup artifacts。
   - 返回 `ReviewSetupResult`，供 Runtime 调用。
6. 连接现有 Runtime。

```python
setup_result = setup_service.compile(workspace)
summary = review_runtime.run(
    manifest_run_id=setup_result.run_id,
    work_items=setup_result.work_items,
    sandboxes=setup_result.sandboxes,
    output_root=run_store.reviewer_results_root,
)
```

### 产物

```text
setup/applicability.json
setup/coverage_units.json
runs/<run_id>/manifest.json
runs/<run_id>/reviewer_results/
runs/<run_id>/worker-events.jsonl
runs/<run_id>/checkpoint.sqlite
```

### 验收标准

- 未确认 Profile 或未验证 Controls 时，Setup 明确阻断，不启动 Reviewer。
- Coverage Unit 数量可以从 Control 和 required surfaces 确定性反算。
- Applicability 为 `UNKNOWN` 时不会静默排除 Control。
- WorkItem Planner 不改变 Coverage 分母。
- 生成的 WorkItems 可以直接传入现有 `LangGraphReviewRuntime.run()`。
- 多个 WorkItems 仍保持独立上下文，并继续受 `max_concurrency=3` 限制。

## 6. Workspace 与 ArtifactStore 设计

### 6.1 推荐目录

```text
workspace/
├── workspace.json
├── materials/
├── setup/
│   ├── repository_inventory.json
│   ├── app_facts.json
│   ├── app_profile_draft.json
│   ├── app_profile.json
│   ├── sources.json
│   ├── obligations.json
│   ├── controls_draft.json
│   ├── controls.json
│   ├── applicability.json
│   └── coverage_units.json
└── runs/
    └── <run_id>/
        ├── manifest.json
        ├── reviewer_results/
        ├── worker-events.jsonl
        └── checkpoint.sqlite
```

机器消费的事实以 JSON 为 source of truth；Markdown 只作为人类阅读投影。现有项目仍有 YAML 示例和配置读取能力，本计划只规定新 Setup artifacts 使用 JSON，不要求一次性删除所有旧示例。

### 6.2 ArtifactStore

新增普通 Python persistence component：

```python
class ArtifactStore:
    ...
```

它不是 Agent Tool，也不是数据库。它只负责在用户选定的 Workspace 内持久化已校验对象。推荐提供语义化方法：

```text
write_repository_inventory
write_app_facts
write_profile_draft
write_app_profile
write_sources
write_obligations
write_controls
write_manifest
write_reviewer_result
```

不提供任意路径的 `write_file(relative_path, content)` 给 Agent 或上层业务调用。

### 6.3 路径与原子写入

所有目标路径必须由 Workspace root 解析并检查：

- 拒绝绝对路径。
- 拒绝 `../` 路径逃逸。
- 拒绝 Workspace root 外部目标。
- 用临时文件写入后 replace 最终文件。
- JSON 写入失败不能留下半个正式 artifact。

Workspace Root 表示长期项目目录；Run Root 表示单次 Review Run。两者可以分别由 `WorkspaceArtifactStore` 和 `RunArtifactStore` 管理，但都必须遵守相同的 path confinement 和 atomic write 规则。

## 7. 推荐工程结构

```text
src/compliance_review/
├── setup/
│   ├── workspace.py
│   ├── repository_inventory.py
│   ├── app_facts.py
│   ├── profile_agent.py
│   ├── profile_validator.py
│   ├── source_registry.py
│   ├── obligation_extractor.py
│   ├── control_compiler.py
│   ├── control_validator.py
│   ├── applicability.py
│   ├── coverage.py
│   ├── work_item_planner.py
│   └── service.py
├── persistence/
│   └── artifact_store.py
└── review/
    └── ... existing LangGraph runtime ...
```

文件名可以调整，但职责不应重新混合。尤其是 `review/` 不应反向承担 Profile、Control 编译和 Workspace 持久化。

## 8. 测试策略

### Phase 1

- workspace 初始化和重复初始化。
- 多仓库 inventory。
- declared/detected surface 一致、冲突、未知。
- AppFacts 无 LLM 时稳定输出。
- Profile provenance、unknown 和 evidence reference 校验。
- Profile Agent 无法调用写文件工具。

### Phase 2

- source hash 和 section provenance。
- structured obligation 输出校验。
- source → obligation → control 全链路引用。
- 重复 Control ID、缺失引用、非法 surface、非法 DSL 拒绝。
- LLM 返回合法 JSON 但不符合领域模型时拒绝。

### Phase 3

- confirmed profile/control gate。
- TRUE/FALSE/UNKNOWN applicability。
- Coverage Unit 确定性展开和数量反算。
- WorkItem 分组不改变 Coverage 分母。
- Setup handoff 到现有 Runtime。
- Workspace path traversal、atomic write 和重启恢复。

## 9. 暂不做的内容

本三阶段计划明确不包括：

- 重写现有 Reviewer Parent/Subgraph。
- 新增 Profile Checker Agent Loop。
- Control Debate 或多 Agent 争论。
- Validator、Verifier、Resolver、Coverage Gate 的最终实现。
- Snapshot、Diff Review、Regression Compare。
- 复杂 Web UI 和人工审批平台。
- PDF/DOCX 作为 Source ingestion 的 MVP 核心能力。
- Agent 任意写仓库或调用 shell。

## 10. 最终完成定义

三阶段完成后，系统应能稳定执行：

```text
compliance-review init <workspace>
        ↓
repository inventory + app facts
        ↓
profile draft + deterministic validation + confirmation state
        ↓
sources → obligations → validated controls
        ↓
applicability → coverage units → WorkItems
        ↓
existing LangGraphReviewRuntime.run()
```

最终架构原则：

> Agents produce structured data; deterministic application code owns validation, applicability, coverage, persistence, and final state.

> Use agent loops only where iterative evidence discovery is necessary. Use structured model calls for bounded semantic transformations.

> Source repositories are read-only. Generated artifacts are persisted by application code inside a user-selected Workspace. Agents receive no arbitrary filesystem write capability.
