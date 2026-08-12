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

Profile Agent 是一个独立的 LangGraph 子图，不直接写文件。它的最小图结构是：

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

它只能返回 `AppProfile` 对象，应用层经过 Pydantic 和 ProfileValidator 校验后，才由 `ArtifactStore` 写入 draft artifact。
Profile 子图使用独立的状态对象和工具循环，不复用 Reviewer 主图的运行状态；后续如果需要，也可以单独为它接入 LangGraph checkpointer。

没有 LLM 时，Setup Service 会生成保守的 deterministic draft；有 LLM 时可注入 Profile Agent，对代码可证明的字段进行补充推断。

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

只有显式调用 `ReviewSetupService.confirm_profile()` 并补齐关键字段后，才会写入：

```text
setup/app_profile.json
```

## 8. ArtifactStore 安全边界

Agent 没有任意写文件工具。`ArtifactStore` 只暴露语义化写入方法，所有路径都必须位于 Workspace root 内，并通过临时文件替换正式文件，避免留下半个 JSON。

验证命令：

```bash
.venv/bin/ruff check src tests
.venv/bin/mypy src
.venv/bin/pytest -q
```
