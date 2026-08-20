---
title: Applicability, WorkItem, and Evidence Reliability Redesign
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
date: 2026-08-17
---

# Applicability, WorkItem, and Evidence Reliability Redesign

## Goal Capsule

在不重构 Reviewer 主循环、不引入 RAG 或第二套扫描平台的前提下，修复真实 Mifos Full Review 暴露出的三类系统性问题：业务画像不完整时 Control 被过早排除、`module + surface` 聚合导致 WorkItem 过粗、Evidence Anchor 重复或来源不一致。

最终权威顺序固定为：

```text
Policy Source / Obligation
  -> Control applicability contract and evidence requirements
  -> confirmed and discovered AppProfile facts
  -> semantic applicability decision
  -> authoritative Control x Surface coverage
  -> one Control x one Surface compliance_review WorkItem
  -> verified Evidence Anchors
  -> deterministic validation / resolution / report
```

停止条件：若实现仍允许 Applicability 模型无依据取消 Control 的必需证据面、允许 `unknown` Control 静默退出审查、或允许未经精确位置验证的代码片段进入 Evidence Ledger，则本计划不得视为完成。

## Product Contract

### Summary

保留 AI 语义判断，但不再让 AI 同时决定“Control 是否适用”和“Control 已声明的证据面是否可以取消”。新版 Control 不再生成或消费旧的字符串 DSL，只使用结构化、可验证的适用条件；旧 DSL 仅允许通过 v1 migration adapter 读取并转换，无法准确转换时进入 `unknown`。对信息不足的 `unknown` 创建有边界的事实发现任务；把正式审查拆成单个 `control_id + surface` WorkItem；最后在程序边界规范化、验证和去重 Evidence Anchor。

### Problem Frame

当前真实 Mifos 场景中，Profile 只声明了 `banking`、`microfinance` 和 `self_lending=false`，没有表达“提供或协助贷款申请”。适用性模型因此把多个贷款 Control 判成 `not_applicable`。另有两个 `unknown_applicability` Control 因 Planner 只消费 `planned` 而未进入代码探索。即使 App 同时提供 Android 与后端源码，模型仍可把 `backend_code` 判成 `not_required`。

同时，Planner 按 `module_id + surface` 聚合 CoverageUnit。当前 12 个 Control 共用 `financial_services` 模块，导致多个控制要求理论上可能被压入一个大 WorkItem。Evidence 侧还存在 Anchor ID 依赖 `call_id`、Ledger 输出未按代码位置去重，以及模型 `fact_ids` 与实际 cited Anchor `fact_ids` 合并后产生 provenance mismatch 的问题。

### Key Decisions

- **Control 的 evidence requirements 是证据面权威，Applicability 不能临场取消无条件要求。** (session-settled: user-directed — chosen over model-controlled per-surface cancellation: 后者已导致后端证据面被过早排除。) Governs R4-R7.
- **`unknown` 进入受限事实发现流程，而不是静默退出 Reviewer。** (session-settled: user-directed — chosen over treating unknown as report-only blocking: AppProfile 可能不完整，代码仍可补充可验证事实。) Governs R8-R12.
- **正式 Reviewer WorkItem 固定为一个 Control 与一个 Surface。** (session-settled: user-directed — chosen over module-plus-surface grouping: 大模块会扩大上下文并降低逐项审查质量。) Governs R13-R15.
- **代码 Evidence 身份由稳定位置与内容决定，不包含 tool call identity。** (session-settled: user-approved — chosen over call-based anchor identity: 同一代码被重复查询不应产生不同证据。) Governs R16-R21.

### Requirements

- **R1.** AI 仍是法规语义 Applicability 的主要判断者；程序不得用简单字符串 DSL 替代政策语义理解。
- **R2.** 新版 Control 不再包含或生成 `applicability_expression`；旧版字段只允许被 migration adapter 读取并转换为结构化条件，不能作为 AI 提示或独立产生 applicability decision。无法准确转换时必须生成 `unknown`。
- **R3.** 新适用条件必须支持有限结构化组合，至少包括 `all_of`、`any_of` 和原子 predicate，并能表示“personal loan 或 EWA”。
- **R4.** 每个 Control 的每个必需证据面必须同时具有非空 `obligation_ids`、可验证 `source_refs`、`surface`、`why_required` 和 `minimum_strength`，可选地带结构化 `condition`。
- **R5.** Control Compiler 不得没有依据地把所有可用 Surface 全部加入 required surfaces；Control Validator 必须拒绝缺少逐面 provenance/rationale、或 Surface 不存在于 linked Obligation required surfaces 的要求。不确定的映射必须标为 compilation gap，不能直接冻结成权威要求。
- **R6.** 当 Control 整体为 `applicable` 时，无条件 required surface 必须保持 required；只有该 EvidenceRequirement 自身带条件且条件被可靠判定为 false 时，才能成为 `not_required`。
- **R7.** Planner 必须执行 `required + available + automated -> planned WorkItem`、`required + unavailable -> missing_surface`；available 但非自动化的 Surface 按 R15a 进入人工/外部收集状态。不得把“法规未明确说后端”解释为“已声明的 backend_code 不要求”。
- **R8.** `unknown` Control 不得静默消失；系统必须为可从现有代码或 Collector facts 解决的未知条件创建 `applicability_discovery` WorkItem。
- **R9.** Discovery WorkItem 只能补充候选/已验证 Profile facts，不能输出 Control PASS/FAIL，也不能修改 Control required surfaces。
- **R10.** 同一 preparation version 的全部 Discovery 任务到达终态后，Applicability 最多自动重评一次；仍 unresolved 时持久化 `unknown_terminal` 或 `manual_required`，Resolver 将两者映射为 `INDETERMINATE`，禁止无限循环。
- **R11.** 对只能由人工、Play Console 或监管材料确认的未知条件，不创建源码探索任务，直接记录明确的 manual/external gap。
- **R12.** Profile v2 首批必须表达当前 Control predicates 和 Mifos 回归实际消费的 `offers_or_facilitates_loans`、`loan_application_flow_present` 与 `earned_wage_access`。后续事实只有在具名 Control predicate 消费时才增加。代码可验证技术事实 `loan_application_flow_present`；`offers_or_facilitates_loans` 等业务/法律身份只能形成 candidate，需人工或外部来源确认。
- **R13.** 正式 Reviewer WorkItem 的唯一业务粒度为 `control_id + surface`，并且只绑定一个 CoverageUnit；`module_id` 只用于报告分组和统计。Applicability、Compilation 等内部结构化调用可继续使用其专用的多 Control 请求对象，不受此约束。
- **R14.** WorkItem 必须带 `work_item_type = compliance_review | applicability_discovery`，两类结果 schema 和权限边界必须区分。
- **R15.** 保留 Runtime 现有可配置并发策略；不得为了控制并发或模型调用数量重新聚合多个 Control。
- **R15a.** `play_console`、`regulator_external` 等非自动化 Surface 不得启动源码 Reviewer；它们分别进入 `manual_required` 或 `external_collection_required` coverage terminal state，由 Coverage Gate 保留。
- **R16.** 代码 Anchor 的 canonical identity 必须基于 repository/file revision、surface、path、start/end line、normalized snippet hash，不得包含 `call_id`。
- **R17.** `search_code` 只能产生含精确位置的完整行级候选；`<uses-permission` 等非唯一短片段不能成为 verified Anchor。
- **R18.** Graphify 结果只能作为导航 `behavioral_hint`；必须经 `read_file` 或等价 exact-location verification 才能升级为代码证明。
- **R19.** Evidence Ledger 在写入和 Reviewer finalization 前均需按 canonical identity 去重，只向模型暴露已验证 Anchor。
- **R20.** `row.fact_ids` 必须完全来源于该 row 实际 cited Anchors，不再与模型自由填写的 `fact_ids` 做并集。
- **R21.** Canonical Anchor 本身保持 Control-neutral；Control 所有权通过独立 citation `(control_id, work_item_id, anchor_id)` 表达，且 citation 的 Control 必须等于当前正式 WorkItem 的 Control。
- **R22.** 报告必须分别统计 applicable、not applicable、unknown、discovery scheduled、planned review、missing surface、manual/external required、not required、completed、failed 和 validated evidence。每个 Control 有一个 applicability 终态；每个声明的 EvidenceRequirement pair 有一个 coverage 终态，不要求未声明 Surface 的笛卡尔积。
- **R23.** Diff Review 只能复用 validated、canonical、未失效的 Evidence；Applicability 或 Profile facts 改变时，相关 CoverageUnit 必须重新规划。
- **R24.** Discovery 不得在当前 preparation run 中新增 Control、required surface 或修改冻结 denominator；若发现规则基线可能缺项，必须形成 compilation gap，并在新的 versioned preparation run 中重编译。

### Key Flows

#### Flow A: Normal applicable Control

```text
Control applicability contract + confirmed profile facts
  -> AI decides applicable
  -> Control evidence requirements remain authoritative
  -> required surface available: planned
  -> one control x one surface WorkItem
  -> verified anchors
  -> Resolver
```

#### Flow B: Unknown because Profile is incomplete

```text
AI decides unknown and names unresolved facts
  -> classify each fact as code-resolvable or manual/external
  -> bounded applicability_discovery WorkItem for code-resolvable technical facts
  -> deterministic validation of discovered facts
  -> one semantic re-evaluation
  -> applicable / not_applicable / still unknown
```

#### Flow C: Required backend evidence is unavailable

```text
Control says backend_code is required
  -> AppProfile has no backend_code root
  -> missing_surface
  -> no fake code review
  -> report and CI gate preserve the missing requirement
```

#### Flow D: Duplicate code searches

```text
Graphify/search returns the same location multiple times
  -> candidate anchors normalized
  -> exact line and revision verified
  -> canonical identity deduplicates them
  -> final ledger contains one Anchor
```

### Acceptance Examples

- **AE1.** A loan application app with `self_lending=false` but verified loan facilitation is not automatically excluded from Google Play personal-loan controls.
- **AE2.** A Control applicable to `personal_loan OR earned_wage_access` is expressible without flattening to one branch; missing EWA facts produce discovery/manual resolution instead of false `not_applicable`.
- **AE3.** An applicable Control requiring Android and backend evidence produces two WorkItems when both repositories exist, even if both share one `module_id`.
- **AE4.** If backend evidence is required but the repository is absent, the backend CoverageUnit is `missing_surface`, not `not_required`.
- **AE5.** Repeating the same `search_code` or `read_file` call produces one canonical Anchor and one consistent fact set.
- **AE6.** A generic snippet such as `<uses-permission` without a uniquely verified path and line range cannot satisfy `static_proof`.

### Scope Boundaries

In scope: profile/applicability contracts, Control evidence requirement provenance, coverage planning, WorkItem granularity, bounded applicability discovery, Anchor normalization, ledger consistency, reporting metrics, migration adapters, unit/integration/Mifos regression tests.

Out of scope: RAG/vector database, new scanner-generation subsystem, unrestricted repository agent, Play Console automation, complex legal AST, changing Graphify itself, claiming Mifos is a Pakistan NBFC, or weakening deterministic Resolver/CI safety.

### Success Criteria

- Mifos Android + Fineract review no longer collapses to one WorkItem merely because Controls share one module.
- Available backend evidence is not canceled by a free-form model surface decision.
- Every unknown Control has either a bounded discovery task or an explicit non-code/manual gap.
- Evidence validation emits no duplicate-anchor, anchor-not-exact, or row-fact-provenance mismatch for exact code evidence fixtures.
- Existing `pytest`, `ruff`, `mypy`, and diff/reuse invariants remain green.

## Planning Contract

### Key Technical Decisions

#### KTD1. Replace the DSL with a structured applicability contract

新版 Control 只保存 typed condition tree，不再保存 `applicability_expression`。旧版字段只在输入适配层读取，并转换成 typed condition tree；转换器不得把无法准确表达的条件强行拆成多个 `and` 条件，也不得把转换失败解释为 `not_applicable`。转换失败统一生成 `unknown` 并记录迁移原因。结构化条件示例：

```json
{
  "any_of": [
    {"fact": "business_type", "op": "includes", "value": "personal_loan"},
    {"fact": "earned_wage_access", "op": "eq", "value": true}
  ]
}
```

The AI uses policy text, obligation provenance, confirmed facts and this structure to produce the semantic decision. Deterministic code validates operators, references and decision coverage; it does not reinterpret regulation. The AI no longer receives the old DSL string. This preserves AI judgment while removing the current `and`-only representation failure.

#### KTD2. Split overall applicability from evidence-surface requirements

`ApplicabilityDecision` remains about the Control. Per-surface decisions are no longer free-form model authority. Each `EvidenceRequirement` is either unconditional or has its own structured condition. Coverage uses three-valued evaluation: verified true means `required`; verified false means `not_required`; missing, candidate-only, conflicting, malformed or evaluation-error facts mean `unknown_applicability` and route to discovery/manual handling, never `not_required`. Empty condition trees are invalid.

#### KTD3. Add a bounded discovery pass for unresolved facts

Use a discriminated `ApplicabilityDiscoveryWorkItemV2` variant. It names canonical unresolved fact keys, allowed surfaces, code roots, tool budget and expected fact schema. Tasks are deduplicated by `(fact keys, allowed surfaces, code roots, preparation version)` so multiple Controls can depend on one discovery result. It may use Collector facts, Graphify navigation, search and exact reads, but cannot emit compliance conclusions. A persisted barrier waits for every scheduled fact to reach `resolved`, `manual_required` or `failed_exhausted`; only then does one automatic re-evaluation run. Late human evidence starts a new preparation version rather than reopening the frozen run.

#### KTD4. Version contracts rather than silently changing v1 semantics

New preparation runs emit v2 profile, control, applicability, coverage and WorkItem envelopes. Read-only report/diff tooling may use explicit v1-to-read-model adapters, but in-progress v1 manifests, attempts and checkpoints are never adapted for v2 resume: they remain v1-only and fail fast with an actionable incompatibility error. Reuse fingerprints include preparation/planner, applicability and anchor contract versions. U2 must add a migration matrix naming each durable artifact, emitted/accepted versions, adapter location, read result, failure behavior and resume policy.

#### KTD5. Canonical Anchor construction is one shared function

Create one Anchor factory/normalizer used by tool-result ingestion, context ledger and finalization. It centralizes existing exact-line, hash, revision and relocation behavior, assigns evidence strength, generates Control-neutral canonical IDs and deduplicates. Graphify-only references remain hints and never enter the verified code ledger; scoped Control citations are stored separately.

### Target Data Contracts

#### Evidence requirement

```yaml
surface: backend_code
why_required: Server-side enforcement of account deletion must be verified.
obligation_ids: [OBL-ACCOUNT-DELETE-01]
source_refs: [{source_id: google-play-account-deletion, source_section: section-4}]
minimum_strength: server_code
condition: null
```

`required_surfaces` should be derived from evidence requirement keys or validated as exactly equal to them, avoiding two drifting authorities.

Code discovery may prove technical capabilities such as “a loan application route and backend handler exist”. It must not independently assert legal/business identity such as “the operator is a licensed lender”; those facts remain human, registry or external-source confirmed.

#### Applicability discovery task

```yaml
work_item_type: applicability_discovery
unresolved_fact_keys: [offers_or_facilitates_loans, earned_wage_access]
dependent_control_ids: [CTRL-PERSONAL-LOAN-PERMISSIONS]
allowed_surfaces: [android_native, backend_code]
tool_budget: configurable
terminal_outcomes: [resolved, manual_required, failed_exhausted]
```

Discovery writes `DiscoveredProfileFactV2` records containing `fact_key`, typed `value`, `status = candidate | verified | unresolved`, `anchor_ids`, `source_surface`, `validator_outcome` and `limitations`. A per-fact policy allowlist defines which technical facts code can verify, minimum evidence strength, and which business/legal facts always require human or external confirmation.

#### Compliance review WorkItem

```yaml
work_item_type: compliance_review
control_id: CTRL-PERSONAL-LOAN-PERMISSIONS
coverage_unit_id: cu.CTRL-PERSONAL-LOAN-PERMISSIONS.android_native
surface: android_native
module_id: data_privacy_and_permissions
```

The v2 schema rejects plural compatibility fields on formal Reviewer WorkItems.

#### Durable artifact migration matrix

| Artifact | New writes | Accepted reads | Migration / failure behavior |
|---|---|---|---|
| Profile / Control / Applicability / Coverage setup artifacts | v2 | v2; selected v1 through explicit read-model adapters | Adapter output is read-only v2 preparation input; provenance is retained |
| Review manifest / WorkItem | v2 | v2 | v1 manifest is inspectable but cannot resume as v2 |
| Attempt metadata / LangGraph checkpoint | v2 | matching v2 only | Contract mismatch fails fast; no cross-version resume |
| Snapshot / report baseline | v2 | v2; v1 only for historical display | v1 cannot be safely reused when planner/applicability/anchor versions differ |

### State Transitions

```text
applicability pending
  -> applicable
  -> not_applicable
  -> unknown

unknown
  -> discovery_pending
  -> discovery_completed -> applicability_recheck
  -> manual_required
  -> unknown_terminal

applicable surface requirement
  -> planned
  -> missing_surface
  -> manual_required | external_collection_required
  -> not_required only when explicit requirement condition is false

planned
  -> review_pending -> running -> completed | failed
```

Deterministic completeness checks must prove that every Control has exactly one applicability terminal state and every required Control/Surface pair has exactly one coverage terminal state.

### Implementation Sequence

1. Add characterization tests for the current Mifos failure before changing contracts.
2. Add v2 profile/applicability/evidence-requirement contracts and migration adapters.
3. Tighten Control compilation and validation before changing Planner authority.
4. Add unknown discovery and one-pass re-evaluation.
5. Change WorkItem granularity to Control x Surface.
6. Canonicalize Evidence Anchor generation and provenance.
7. Update Resolver/report/diff metrics and run the real Mifos regression.

## Implementation Units

### U1. Characterize the current failure and lock invariants

**Goal:** Preserve a reproducible baseline for the one-WorkItem, premature surface cancellation and Anchor mismatch failures.

**Requirements:** R6-R8, R13, R16-R21.

**Files:** `tests/test_setup_phase3.py`, `tests/test_review_pipeline.py`, `tests/test_review_finalization.py`, `scripts/run_mifos_real_e2e.py`, new focused fixtures under `tests/fixtures/` if needed.

**Approach:** Add failing regression fixtures that model loan facilitation with Android/backend roots, unknown EWA, two Controls sharing a module, duplicate tool calls and model-supplied unrelated fact IDs. Avoid asserting exact LLM prose.

**Test Scenarios:** AE1-AE6 as narrow deterministic tests; capture the current Mifos run summary as an integration expectation without committing large generated run artifacts.

**Verification:** New tests fail for the intended current behavior before implementation and pass after the corresponding unit lands.

### U2. Introduce Profile and structured applicability v2 contracts

**Goal:** Represent business capabilities and OR conditions without treating a weak DSL as policy authority.

**Requirements:** R1-R3, R8-R12.

**Files:** `src/compliance_review/domain/models.py`, `src/compliance_review/setup/models.py`, `src/compliance_review/setup/service.py`, `src/compliance_review/setup/profile_agent.py`, `src/compliance_review/review/applicability.py`, `src/compliance_review/compilation/models.py`, `tests/test_setup_phase1.py`, `tests/test_applicability_surfaces.py`, associated compilation tests.

**Approach:** Add typed predicate nodes with a small allowlist of operators and nesting depth. Expand profile facts. New v2 writes omit `applicability_expression`. Add an explicit v1 read adapter that converts old strings to the structured condition or returns a reason-coded `unknown` when the old expression is lossy or unsupported. The semantic request includes source text, obligations, confirmed/candidate facts and only the structured condition.

**Test Scenarios:** three-valued `any_of` combinations; personal-loan/EWA; unavailable confirmed fact; inferred candidate cannot independently justify `not_applicable`; invalid operator/reference rejected; v1 `and` expression migrates deterministically; unsupported v1 `or` or lossy expression becomes `unknown` rather than being flattened incorrectly; v2 output contains no `applicability_expression`; `self_lending=unknown` remains representable instead of failing during profile conversion; unrecognized repository surface remains explicitly unresolved.

**Verification:** Pydantic schemas reject malformed predicates, and semantic evaluator tests prove every Control receives exactly one decision.

### U3. Make per-surface evidence requirements authoritative

**Goal:** Prevent Applicability from canceling backend or other required evidence without a Control-defined condition.

**Requirements:** R4-R7.

**Files:** `src/compliance_review/compilation/models.py`, `src/compliance_review/compilation/llm.py`, `src/compliance_review/compilation/validator.py`, `src/compliance_review/review/applicability.py`, `src/compliance_review/setup/planning.py`, `tests/test_applicability_surfaces.py`, compilation tests.

**Approach:** Add provenance and optional condition to each evidence requirement. Derive/validate required surface keys. Remove unconstrained per-surface model decisions from the authority path. Reject draft controls whose evidence requirements lack rationale/provenance or use unsupported strength for the surface.

**Test Scenarios:** unconditional backend requirement remains required; conditional web requirement becomes not required only when its explicit condition is false; unknown/conflicting condition routes to discovery rather than not required; absent backend root becomes missing surface; generic all-surface and plausible-but-unsupported surface mappings are rejected.

**Verification:** Coverage tests derive stable results independent of model wording about individual surfaces.

### U4. Add bounded applicability discovery

**Goal:** Use available code to resolve incomplete AppProfile facts without allowing discovery agents to make compliance decisions.

**Requirements:** R8-R12, R14, R24.

**Files:** `src/compliance_review/setup/profile.py`, `src/compliance_review/setup/planning.py`, WorkItem/result contracts, artifact persistence, setup and runtime tests.

**Approach:** Extend the existing bounded, read-only `ProfileAgent` with an applicability-discovery mode rather than building a parallel agent subsystem. Classify unresolved facts into code-resolvable technical facts versus manual/external business facts. Deduplicate tasks by fact set and execution scope; persist typed results and wait at the discovery barrier. Merge validated results into a derived profile layer, then invoke semantic applicability once. Discovery cannot expand the frozen denominator; a suspected missing Control/Surface becomes a compilation gap for a new preparation version. Create a new service abstraction only if a focused design check proves the existing graph cannot enforce the no-compliance-conclusion boundary.

**Test Scenarios:** reachable product-bound routes verify `loan_application_flow_present` while `offers_or_facilitates_loans` remains candidate/manual; EWA remains manual when no reliable code proof exists; shared facts are discovered once for multiple Controls; mixed code/manual tasks wait at the barrier; failed discovery stays unknown; no third applicability pass is scheduled.

**Verification:** No unknown Control disappears from coverage accounting, and discovery results cannot contain PASS/FAIL fields.

### U5. Change formal review planning to Control x Surface

**Goal:** Keep each Reviewer context narrow and independently auditable.

**Requirements:** R13-R15a.

**Files:** `src/compliance_review/domain/models.py`, `src/compliance_review/setup/planning.py`, `src/compliance_review/review/manifest.py`, runtime scheduling, persistence/resume/diff modules, `tests/test_setup_phase3.py`, planning and diff tests.

**Approach:** Consolidate the current planner and legacy `ReviewManifestBuilder` onto one planning rule. Build one formal Reviewer WorkItem for each automated planned CoverageUnit. Include exactly one `control_id` and one `coverage_unit_id`; retain module as metadata. Exclude manual/external Surface rows from automated scheduling. Update IDs and durable attempt lookup. Concurrency remains bounded by runtime configuration rather than grouping.

**Test Scenarios:** two Controls in one module and one surface create two WorkItems; one Control requiring Android/backend creates two WorkItems; planned CoverageUnits map bijectively to WorkItems; manual Surface units create no Reviewer; resume and diff reuse operate per CoverageUnit; v1 checkpoints cannot resume as v2.

**Verification:** Planner enforces the planned automated CoverageUnit-to-WorkItem bijection with schema validators, while existing multi-item runtime isolation and resume regressions remain green.

### U6. Canonicalize and validate Evidence Anchors

**Goal:** Eliminate duplicate, ambiguous and provenance-mismatched code evidence.

**Requirements:** R16-R21.

**Files:** `src/compliance_review/review/langgraph_runtime.py`, `src/compliance_review/review/context.py`, `src/compliance_review/review/finalization.py`, tool result models/helpers, review tests.

**Approach:** Centralize candidate-to-verified Anchor conversion. Remove `call_id` and Control ownership from identity. Require path, exact line range, normalized full snippet hash and revision for code proof. Downgrade Graphify-only results to navigation hints. Replace fact-ID union with cited-anchor fact IDs. Validate separate scoped citations against the current WorkItem Control.

**Test Scenarios:** duplicate calls dedupe; same lines at a new revision do not reuse old Anchor; generic snippet rejected; Graphify candidate cannot satisfy static proof; fabricated model fact IDs are removed.

**Verification:** Evidence Ledger and final result contain identical canonical Anchor sets and exact provenance.

### U7. Align resolution, reporting and Mifos E2E

**Goal:** Make terminal-state accounting visible and prove the redesigned flow on public Android/backend repositories.

**Requirements:** R22-R24 and all Acceptance Examples.

**Files:** report renderer, full/diff review services, snapshot models, `scripts/run_mifos_real_e2e.py`, new opt-in `tests/test_mifos_real_e2e.py`, `tests/test_full_review.py`, `tests/test_diff_review.py`, `docs/phase3-planning-learning-notes.md`, relevant architecture docs.

**Approach:** Add separate counts and tables for discovery and formal review plus conservation equations: Controls in equal applicability terminal decisions; declared EvidenceRequirement pairs equal CoverageUnits for applicable/unknown Controls; planned automated units map exactly once; expected WorkItems equal terminal executions; CoverageUnits equal gate rows; Anchor candidates equal validated Anchors plus rejected candidates; cited Anchor IDs are a subset of the validated ledger. Update snapshot fingerprints to include preparation/applicability/anchor contract versions. Parameterize one Mifos runner for two asserted scenarios: a confirmed-profile baseline and an incomplete-profile run that must exercise discovery, provenance, the barrier and one re-evaluation.

**Test Scenarios:** real Mifos Google Play run schedules Android and backend work where required; unknown/manual gaps stay visible; Diff Review invalidates rows when profile/applicability facts change; report totals reconcile with machine artifacts.

**Verification:** Generate a fresh public-project report and deterministically reconcile every headline count to applicability, coverage, attempts and validated evidence artifacts.

## Verification Contract

Run after each implementation unit where relevant, and all commands at final integration:

```bash
pytest
ruff check .
mypy src tests
git diff --check
```

Required focused gates:

```bash
pytest tests/test_setup_phase3.py
pytest tests/test_compilation_phase2.py tests/test_compilation_batching.py
pytest tests/test_review_pipeline.py tests/test_reviewer_context.py
pytest tests/test_review_finalization.py tests/test_full_review.py tests/test_diff_review.py
```

The real LLM Mifos E2E is a gated integration test, not a deterministic unit test. Run it only after deterministic gates pass. Record model, policy source hashes, repository revisions, profile confirmations, applicability counts, discovery counts, WorkItem counts by surface, Anchor validation counts and report path.

Failure gates:

- Any applicable Control that declares zero EvidenceRequirements resolves to `INDETERMINATE`, never PASS. A Control with declared conditional requirements that all reliably evaluate false may terminate with those pairs as `not_required`.
- Any unknown Control without discovery or explicit manual/external terminal state fails completeness validation.
- Any required, available and automated Control/Surface pair without a WorkItem fails planning validation; manual/external pairs must instead have their explicit terminal collection status.
- Any code Anchor lacking exact verified location cannot satisfy complete evidence.
- Any report total that cannot be recomputed from machine artifacts fails report validation.

## Definition of Done

- U1-U7 are implemented in dependency order with focused regression tests.
- New durable contracts are explicitly versioned or migrated; no silent v1 semantic change remains.
- Applicability AI cannot cancel unconditional Control evidence requirements.
- `unknown` follows a bounded, persisted discovery/manual path and cannot disappear.
- Formal review WorkItems contain exactly one Control and one Surface.
- Canonical Anchor identity excludes call IDs; ledger and finalization use the same dedupe rules.
- `row.fact_ids` equals cited verified Anchor facts exactly.
- Full and Diff Review state machines preserve all safety invariants.
- The public Mifos Android/backend demonstration produces reconcilable coverage and WorkItem totals, while clearly labeling confirmed scenario assumptions.
- `pytest`, `ruff check .`, `mypy src tests`, and `git diff --check` pass, excluding only pre-existing unrelated user changes explicitly documented before implementation.
- Adapters, duplicate fields and experimental artifacts introduced or rendered unreachable by U2-U7 are removed before merge; unrelated pre-existing cleanup is tracked separately.

## Appendix

### Current implementation anchors

- `src/compliance_review/review/applicability.py`: semantic evaluator and current per-surface decisions; legacy DSL handling moves to the v1 migration adapter.
- `src/compliance_review/setup/planning.py`: CoverageUnit construction and current `module_id + surface` WorkItem grouping.
- `src/compliance_review/domain/models.py`: Control, Profile, Applicability, Coverage, WorkItem and Evidence contracts.
- `src/compliance_review/compilation/llm.py`: obligation applicability prompt and Control evidence requirement compilation.
- `src/compliance_review/review/langgraph_runtime.py`: tool-result Anchor generation and result ledger attachment.
- `src/compliance_review/review/context.py`: in-memory Evidence Ledger deduplication key.
- `scripts/run_mifos_real_e2e.py`: current public Android/backend integration scenario.

### Why keep both AI and structured predicates

The structured predicate is not a second legal reasoning engine. It is a bounded, auditable way to tell the AI and deterministic validators which known facts and logical alternatives matter. The AI still interprets policy language, evaluates whether the predicate and source provenance apply to the App, and explains unresolved conditions. Deterministic code only checks that references exist, all decisions are accounted for, and no component exceeds its authority.
