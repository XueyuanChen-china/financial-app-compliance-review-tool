# Day 2 学习与实现记录：Graphify Code Map and Collectors

## 1. Day 2 目标

Day 2 主要解决两个问题：

1. 如何在不让 Reviewer 任意读取仓库的情况下，安全、可控地读取代码。
2. 如何先用确定性程序提取稳定事实，再交给后续 Agent 做解释。

本阶段不生成最终合规结论，也不让 Graphify 或 Collector 直接判断 Control 是否通过。

## 2. 本阶段完成的模块

```text
RepositorySandbox
    |
    +-- GitRepository
    +-- ReadOnlyRepositoryTools
    |
    +-- GraphifyCodeMapProvider
    |
    +-- ManifestCollector
    +-- DependencyCollector
    +-- ApiDocumentCollector
```

## 3. Repository Sandbox

`RepositorySandbox` 为每个目标仓库建立读取边界：

- 只允许访问配置的 repository root 内部路径。
- 阻止 `../` 路径逃逸。
- 阻止 symlink 指向仓库外部。
- 限制单文件读取大小。
- 隐藏 `.env`、密钥、证书、签名文件和 service account 文件。

这层的意义是：Reviewer 和 Collector 即使拿到了错误路径，也不能任意读取宿主机文件或发布凭据。

## 4. Git Metadata

`GitRepository` 只读获取：

- 当前 revision。
- 是否为独立 Git 仓库。
- 是否存在未提交修改。
- changed files 列表。

如果目标目录只是另一个 Git 仓库内部的普通子目录，不会误认为它拥有自己的 Git revision，而是返回结构化状态：

```text
path_is_inside_parent_repository
```

这为后续 diff review 和 Snapshot 复用提供基础。

## 5. Read-only Tools

Repository 层提供三个基础文件工具：

```text
list_files(root, pattern, limit)
search_code(query, roots, file_globs, limit)
read_file(path, start_line, line_count)
```

`search_code` 优先使用 `git grep`，不可用或没有结果时回退到受限文本搜索。所有操作都有路径范围和数量限制，不执行目标仓库代码，也不调用业务 API。

在 Day 3 的 Reviewer Tool Runtime 中，这三个工具会和 Graphify、Collector Facts 统一纳入受控白名单；模型仍然不能直接获得 shell 权限。

## 6. Graphify Code Map

项目通过自己的 `CodeMapProvider` 调用本地 Graphify CLI，而不是让上层代码直接依赖 Graphify 的原始输出。Day 2 现在包含完整的初始化链路：先建图，再查询。

初始化一个代码仓库：

```bash
compliance-review init --repo /path/to/repository
```

初始化命令会检查 `graphify`，缺失时默认执行：

```bash
uv tool install graphifyy
```

然后执行：

```bash
graphify extract . --code-only
```

如果使用 App Profile：

```bash
compliance-review init --profile examples/app-profile.yaml
```

它会为 `frontend_h5`、`android_native`、`backend_code` 的代码 roots 分别建图。`backend_api_doc` 是 OpenAPI/Swagger 文档，不进入 Graphify 建图。

调用示例：

```bash
.venv/bin/compliance-review code-map-query \
  --repo /path/to/repository \
  --query "account deletion workflow" \
  --surface backend_code
```

Graphify 只返回数量受限的候选节点和关系：

```yaml
status: available | unavailable | degraded
candidates: []
relations: []
```

状态含义：

- `available`：成功返回可用的代码导航结果。
- `unavailable`：CLI、目标仓库或索引不存在。
- `degraded`：超时、命令失败或输出无法解析。

如果还没有先执行 `init`，`code-map-query` 会返回 `graph_not_initialized`，而不是直接把空结果解释成“代码不存在”。

重要边界：

```text
Graphify 没找到节点，不等于代码不存在。
```

需要判断 absence 时，仍必须使用 `search_code`、`list_files` 和 `read_file` 做精确验证。

## 7. Generic Collectors

### 7.1 Manifest Collector

读取 Android `AndroidManifest.xml`，提取：

- 权限声明。
- activity、service、receiver 等组件数量。
- 组件的基础静态信息。

解析失败时输出 `parser_status=failed`，不会返回伪造的空事实。

### 7.2 Dependency Collector

读取：

- `package.json`。
- Android/后端 Gradle 文件。

输出依赖名称、版本和声明组。它只说明“项目声明了某个依赖”，不直接判断该依赖是否合规。

### 7.3 API Document Collector

该 Collector 只支持 `backend_api_doc` 的 OpenAPI/Swagger 文档。

`backend_api_doc`：

- 读取 OpenAPI/Swagger JSON 或 YAML。
- 提取 `paths`、HTTP method、`operationId`。
- 输出 `evidence_strength=server_doc`。
- 只能证明 API 文档声明了某个接口。

`backend_code`、`frontend_h5` 和 `android_native` 的源码路由不再由通用 Collector 跨语言解析。由 Graphify 定位候选 controller/router/handler，再由 `search_code`、`read_file` 和 Reviewer 做精确核验。

这样避免维护多种框架的路由规则，也避免把“源码中出现了某个路径字符串”误认为运行时行为证明。

## 8. Collector 统一输出

每个 Collector 返回统一结构：

```yaml
collector_id: api_document_inventory
source_surface: backend_api_doc
parser_status: ok | fallback | failed
coverage_status: complete | partial | unknown
input_files: []
facts: []
limitations: []
metadata: {}
```

每条 Fact 至少包含：

```yaml
fact_id: fact.backend_api_doc.endpoint.1
source_surface: backend_api_doc
fact_type: declared_api_endpoint
observed_value:
  method: POST
  route: /v1/auth/login
source_refs:
  - path: api-doc/openapi.json
parser_status: ok
coverage_status: partial
evidence_strength: server_doc
```

`coverage_status=complete` 只表示输入文件被处理完成，不表示业务行为已经被完整证明。

## 9. Fixtures 与验证

本阶段加入了以下验证场景：

- Graphify CLI 不存在：输出 `unavailable`。
- Graphify 输出无法解析：输出 `degraded`。
- Manifest 正常解析：输出权限和组件事实。
- Manifest 解析失败：输出 `failed/unknown`。
- OpenAPI 文档正常解析：输出 `server_doc` endpoint facts。
- OpenAPI 文档解析失败：输出 `fallback/unknown`。
- 源码路由不由 Collector 生成；通过 Graphify + 只读工具进行定位和核验。
- 目标目录越界和敏感文件读取：被 Sandbox 拒绝。

验证命令：

```bash
.venv/bin/ruff check .
.venv/bin/mypy src
.venv/bin/pytest
```

当前结果：26 个测试通过。

## 10. CLI 示例

查看仓库和 Git 信息：

```bash
.venv/bin/compliance-review repository-info --repo /path/to/repository
```

搜索代码：

```bash
.venv/bin/compliance-review search-code \
  --repo /path/to/repository \
  --query "deleteAccount" \
  --root src
```

提取后端 API 文档：

```bash
.venv/bin/compliance-review collect \
  --repo /path/to/repository \
  --collector api-doc \
  --surface backend_api_doc \
  --root api-doc
```

源码路由不再单独运行 Collector。使用 `code-map-query` 定位，再由 Reviewer 通过 `search-code` 和 `read_file` 核验。

## 11. Day 2 结论

本阶段形成了从“安全读取代码”到“输出结构化事实”的基础链路：

```text
Repository Root
  -> Sandbox
  -> Read-only Tools / Graphify
  -> Generic Collectors
  -> Facts
```

后续 Reviewer Agent 应该消费这些事实和有限的代码片段，而不是每次从仓库根目录开始无边界泛读。下一阶段重点是构建隔离的 Review Work Items，并让多个 Reviewer 在受控并发下工作。
