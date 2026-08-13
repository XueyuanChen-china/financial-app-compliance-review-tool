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

- `.md` / `.txt`：按 Markdown heading 或全文 section 提取。
- `.pdf`：使用 `pypdf` 按页提取文本。
- `.docx`：使用 `python-docx` 按 Heading 和段落提取文本。

每个 `ComplianceSource` 保留：

```text
source_id
原始 path
title / version
sha256
source_family
media_type
sections[].section_id / text / page
extraction_status / limitations
```

解析器只负责文本识别，不判断政策含义。扫描型 PDF 如果没有文本层，会进入 extraction failed，
不会伪造出可供 LLM 使用的规则。

## 3. 两次 structured LLM call

### Obligation Extraction

输入是 `sources[].sections[]`，输出是 `obligation_set.v1`。每条 Obligation 必须保留：

```text
obligation_id
source_id
source_section
statement
concepts
applicability_expression
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
applicability_expression
required_surfaces
evidence_requirements
missing_evidence_policy
```

这两个阶段使用 `ModelProvider.complete()` 各调用一次，`tools=[]`，并要求 JSON object response。
它们不是 Agent Loop，不能调用 Graphify、读取仓库或自行探索项目。

## 4. Deterministic ControlValidator

Validator 不使用 LLM，负责拒绝：

- 重复 `control_id`。
- 不存在的 `obligation_id`。
- 不存在的 `source_id` 或 `source_section`。
- 非法 `required_surfaces`。
- 不完整或多余的 `evidence_requirements`。
- 不符合有限 DSL 的 `applicability_expression`。
- 空 Control Set。

有限 DSL 当前只允许：

```text
field == value
field includes value
value in field
多个条件用 and 或 && 连接
```

只有 validation 通过，才会把 `ControlDraft[]` 转换为 `control_set.v1` 并写入 `setup/controls.json`。

## 5. 产物和失败语义

```text
setup/sources.json
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
