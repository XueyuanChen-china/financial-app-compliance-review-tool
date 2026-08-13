# Phase 1 学习与实现记录：Workspace、Repository Intake 与 App Profile

## 1. 本阶段完成的边界

Phase 1 只负责回答：

```text
有哪些仓库？
每个仓库可能属于哪个 evidence surface？
从仓库中能确定哪些技术事实？
哪些 AppProfile 字段仍需要用户确认？
```

它不生成 Controls，不判断法规适用性，也不启动正式 Reviewer。

## 2. Workspace

`ComplianceWorkspace` 是长期存在的审核项目目录配置，使用 `workspace.v1`：

```text
workspace.json
materials/
setup/
runs/
```

初始化命令：

```bash
compliance-review init ./my-review \
  --repository mobile=/path/to/mobile \
  --repository backend=/path/to/backend
```

旧的 `init --repo` 仍保留为 Graphify 兼容入口。

## 3. Repository Inventory

Inventory 使用确定性文件信号进行 surface detection：

- `AndroidManifest.xml`、Gradle 和 Android 目录信号指向 `android_native`。
- `package.json` 和前端依赖指向 `frontend_h5`。
- 后端构建文件或后端布局指向 `backend_code`。
- OpenAPI/Swagger 文件指向 `backend_api_doc`。

用户声明的 surface 不会被模型静默覆盖：声明与检测冲突时记录 `unresolved`，没有声明且检测结果不唯一时也保持 `unresolved`。

Inventory 同时记录 Git revision、dirty 状态、changed files 和错误状态。

## 4. AppFacts

AppFacts 复用现有确定性 Collector：

```text
ManifestCollector
DependencyCollector
ApiDocumentCollector
```

Collector 产生的 `Fact` 是技术事实，例如 Android 权限、依赖、声明的 API endpoint。它们可以被 Profile Agent 读取，但 Agent 不能重写这些事实。

## 5. AppProfile provenance

Profile 字段不保存裸值，而保存：

```text
value
source
confidence
evidence
```

`source` 支持：

```text
declared
deterministic
inferred
human_confirmed
unresolved
```

代码无法证明的 jurisdiction、license、business type 或 self-lending 会进入 `unresolved`，不允许模型猜测。

## 6. Profile Agent Subgraph

Profile Agent 是一个独立的 LangGraph 子图，不直接写文件。它现在以整个 Workspace 为输入，
可以同时理解 frontend、Android、backend 等多个仓库。每次代码工具调用都必须带 `repo_id`，
由子图把调用路由到对应的 `RepositorySandbox`。

它的最小图结构是：

```text
START
  -> initialize
  -> call_model
  -> execute_tools -> call_model  (最多 max_rounds 次)
  -> finalize
  -> END
```

它可以读取：

```text
get_repository_inventory
get_app_facts
code_map_query
code_map_path
search_code
read_file
list_files
```

它只能返回 `AppProfile` 对象。应用层不会直接接受整个模型输出，而是将其作为候选字段，
与 deterministic profile 合并：`evidence_surfaces`、`review_scope`、`repository_roots` 等
deterministic 字段不能被模型覆盖。合并结果经过 Pydantic 和 ProfileValidator 校验后，才由
`ArtifactStore` 写入 draft artifact。

Profile 子图复用 Reviewer 的 `ReviewerContextManager`、active/retired rounds 和工具调用预算，
但不复用 Reviewer 的 Work Item 结论或运行状态。Profile Agent 的上下文仍会每轮保留 inventory
和 AppFacts，避免压缩或轮次淘汰后丢失确定性输入。

没有 LLM 时，Setup Service 会生成保守的 deterministic draft；有 LLM 时，可以使用：

```bash
compliance-review init ./my-review \
  --repository web=/path/to/web \
  --repository android=/path/to/android \
  --profile-model <model-name>
```

这个可选参数才会启动 Profile Agent；不提供时不会联网，仍只执行 deterministic intake。

## 7. Profile confirmation

Profile 状态分为：

```text
draft
awaiting_confirmation
confirmed
```

初始化通常会生成：

```text
setup/app_profile_draft.json
setup/profile_validation.json
setup/app_profile_confirmation.json
```

确认时会重新加载 `repository_inventory.json` 和 `app_facts.json`，重新执行
`ProfileValidator`。仓库 surface 冲突必须显式解决，不能只补 AppProfile 字段。例如：

```python
service.confirm_profile(
    values={"business_type": ["personal_loan"]},
    repository_surfaces={"backend": "backend_code"},
)
```

只有显式确认、补齐关键字段且二次校验无 errors/conflicts 后，才会写入：

```text
    setup/app_profile.json
```

Collector facts 在 Setup 聚合层会带上 `repo_id`，并生成 repository-scoped `fact_id`；
同一 surface 下多个仓库的同名依赖、权限或 endpoint 不会互相覆盖。

## 8. ArtifactStore 安全边界

Agent 没有任意写文件工具。`ArtifactStore` 只暴露语义化写入方法，所有路径都必须位于 Workspace root 内，并通过临时文件替换正式文件，避免留下半个 JSON。

验证命令：

```bash
.venv/bin/ruff check src tests
.venv/bin/mypy src
.venv/bin/pytest -q
```
