# Graphify Code Map Provider

## 1. 这层解决什么问题

Reviewer 经常需要先回答：

```text
某个 Control 相关代码可能在哪里？
这些函数之间有没有调用关系？
```

如果每个 Reviewer 都从仓库根目录开始反复 grep，会增加上下文和工具调用成本，也容易漏掉跨文件关系。

Graphify 用本地 AST/代码图提供代码导航，但它不是合规 Scanner，也不是最终 Evidence Authority。

## 2. 为什么要包一层

项目不直接把 Graphify 的 graph schema、Skill 或原始输出暴露给 Reviewer，而是通过：

```text
CodeMapProvider
        |
        +-- GraphifyCodeMapProvider
        +-- future provider
```

这样做的好处：

- Reviewer 只认识项目自己的稳定接口。
- Graphify 输出可以限制候选数量和 token budget。
- 以后更换 CodeGraph/MCP/其他 Provider 时，Compliance Domain 不需要重写。
- Graphify 失败可以返回 `unavailable/degraded`，不会污染 Control 状态。

## 3. 当前接口

第一版只实现：

```python
CodeMapProvider.query(CodeMapQuery) -> CodeMapQueryResult
```

输入可以来自独立的前端、Android 或后端代码仓库。`backend_api_doc` 也可以作为查询上下文，但它只代表接口文档，不等于后端实现代码。

输入：

```json
{
  "query": "account deletion workflow",
  "surface": "backend_code",
  "max_candidates": 5,
  "budget": 2000
}
```

输出：

```json
{
  "query": "account deletion workflow",
  "surface": "backend_code",
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

候选结果有上限，Wrapper 不返回整个 graph。

## 4. 状态含义

| 状态 | 含义 | 后续动作 |
|---|---|---|
| `available` | Graphify 成功执行并解析出结果 | Reviewer 可用候选导航 |
| `unavailable` | CLI、仓库或索引不可用 | 转到 fallback 或记录缺口 |
| `degraded` | 超时、命令失败或输出无法解析 | 允许 fallback，不作合规结论 |

特别重要：

```text
Graphify query empty != code does not exist
```

如果要证明 absence，必须继续使用 `search_code`、文件搜索和 `read_file`，最终可能是 `PARTIAL` 或 `INSUFFICIENT_EVIDENCE`。

## 5. Graphify 安装与建图

Graphify 官方包名是 `graphifyy`，CLI 命令是 `graphify`。项目不把它作为 Python 运行时依赖强制安装，而是把它作为本地工具：

```bash
uv tool install graphifyy
graphify /path/to/target/repository
```

建图后，Graphify 默认在目标仓库的 `graphify-out/` 中保存地图。当前 Wrapper 通过目标仓库目录执行：

```bash
.venv/bin/compliance-review code-map-query \
  --repo /path/to/target/repository \
  --query "account deletion workflow" \
  --surface backend_code
```

如果本机没有 `graphify`，命令仍会返回结构化 `unavailable` JSON，而不是抛出异常。

## 6. 当前不做什么

- 不使用 Graphify MCP。
- 不加载 Graphify 的完整 Codex Skill。
- 不直接读取或修改 Graphify 的 `graph.json`。
- 不把 Graphify 结果当作 Evidence 或 Compliance Result。
- 不实现 `code_map_path`，等 `code_map_query` 稳定后再做。
- 不实现自动建图、复杂增量图算法或 Knowledge Graph 数据库。
