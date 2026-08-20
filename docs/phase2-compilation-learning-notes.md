# Phase 2 学习与实现记录：Source Registry、Obligation 与 Control Compilation

## 1. 阶段边界

Phase 2 是低频规则基线构建阶段，流程固定为：

```text
原始政策文件
  -> Source Registry
  -> Obligation Extraction
  -> Control Compilation
  -> Deterministic Validation
  -> Validated Control Set
```

本阶段不启动 Reviewer Agent，不读取应用代码，也不生成 scanner。

## 2. Source Registry 和文件解析

`SourceRegistryBuilder` 负责登记原始文件和提取可追溯的文本 sections：

- `.md` / `.txt`：按“全文 -> Heading -> Paragraph -> Sentence -> Hard Split”的顺序降级，尽量保留大语义块。
- `.pdf`：使用 `pypdf` 提取所有页面文本后再统一切分，页码只用于 provenance，不作为语义切分边界。
- `.docx`：使用 `python-docx` 按 Heading 和段落提取文本。

每个 `ComplianceSource` 保留：

```text
source_id
原始 path
title / version
sha256
source_family
media_type
sections[].section_id / text / title / location / page / page_end
extraction_status / limitations
```

解析器只负责文本识别，不判断政策含义。扫描型 PDF 如果没有文本层，会进入 extraction failed，
不会伪造出可供 LLM 使用的规则。

## 3. SourceSection、Batch 和 bounded compilation loop

`SourceSection` 是带 provenance 的原文语义块；`Batch` 是一次模型调用的输入容器。
`BatchPlanner` 按估算 token budget 贪心打包完整 SourceSection，并且不混合不同 source 文件：

```text
Source Registry
  -> SourceSection[]
  -> SourceSectionBatch[]
  -> 每个 Batch 一次 structured call
  -> deterministic merge
```

因此较大的 Source Registry 不会被一次性塞进模型。程序决定 batch 边界，模型只处理当前
batch，不使用工具、RAG 或 Agent Loop。

## 4. Obligation Extraction 的 coverage contract

每个 batch 的模型结果必须对每个计划 section 给出且只给出一个 terminal decision：

```text
obligations_extracted -> obligation_ids[]
no_obligation         -> reason
```

程序会确定性拒绝缺失、重复、未知 section decision，以及 obligation provenance 越界。
所有 batch 完成后，还会检查整个 Source Registry 的 planned sections 和 completed decisions
完全一致。这样可以区分“该 section 没有义务”和“该 section 被漏处理”。

### Obligation Extraction

输入是单个 `source_section_batch.v1`，输出是 `obligation_extraction_batch.v1`。每条 Obligation 必须保留：

```text
obligation_id
source_id
source_section
statement
concepts
applicability_condition
required_surfaces
source_refs
```

### Control Compilation

输入只有 `Obligation[]`，不直接输入原始 Source 文本。输出是 `control_draft_set.v1`，保留：

```text
control_id
module_id
obligation_ids
source_refs
applicability_condition
required_surfaces
evidence_requirements
missing_evidence_policy
```

每个 Obligation batch 使用 `ModelProvider.complete()` 一次，随后 Control Compilation 使用一次
structured call。两者都使用 `tools=[]`，不能调用 Graphify、读取仓库或自行探索项目。
如果 provider 支持 JSON Schema response format，会传入对应的 Pydantic JSON Schema；返回后仍必须
通过 Pydantic validation 和 deterministic semantic validation。

## 5. Deterministic ControlValidator

Validator 不使用 LLM，负责拒绝：

- 重复 `control_id`。
- 不存在的 `obligation_id`。
- 不存在的 `source_id` 或 `source_section`。
- 非法 `required_surfaces`。
- 不完整或多余的 `evidence_requirements`。
- 无法安全表示或结构不合法的 `applicability_condition`。
- 空 Control Set。

结构化条件当前只允许四种节点：`atom`、`all_of`、`any_of` 和
`unknown`。`atom` 只允许 `equals`、`includes` 两个操作符；旧版
`applicability_expression` 只在迁移适配器中读取，无法无损转换时变为
`unknown`，不会被当作新的规则语言。

只有 validation 通过，才会把 `ControlDraft[]` 转换为 `control_set.v2` 并写入 `setup/controls.json`。

## 6. 产物和失败语义

```text
setup/sources.json
setup/obligation_extraction_batches.json
setup/obligations.json
setup/controls_draft.json
setup/control_validation.json
setup/controls.json       # 仅 validated 时生成
```

每次新编译开始会先使旧的 `controls.json` 失效。编译失败时仍保留 source、obligation、draft 和
validation 证据，但不会留下当前失败运行可误读的 validated Control Set。

CLI 示例：

```bash
compliance-review compile-rules ./review-workspace \
  --source /path/to/secp-materials \
  --source-family /path/to/secp-materials=country_regulator \
  --source /path/to/google-play.pdf \
  --source-family /path/to/google-play.pdf=google_play \
  --model gpt-4o-mini
```

如果材料已经在 `workspace.json` 的 `materials` 中登记，可以省略 `--source`。

新增的测试覆盖小文件整体保留、Heading/Paragraph/Sentence/Hard Split 降级、PDF 页码 provenance、
batch budget、多个模型调用、terminal coverage 缺失，以及多 batch obligation 的确定性合并。
