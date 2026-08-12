# Day 1 学习文档：领域合同与项目基础

## 1. 今天完成了什么

Day 1 不是在实现真正的代码扫描，而是在先固定系统各部分之间如何交换数据。这样后续 Collector、Reviewer Agent、Verifier 和 Validator 才能使用同一套语言。

本次完成：

- Python CLI 项目骨架。
- Pydantic 领域模型。
- YAML 配置加载器。
- App Applicability Profile 校验。
- MVP Control Set 校验。
- Fact、Evidence、Work Item、Review Result、Snapshot 模型。
- 一个合法示例和一个故意错误示例。
- Ruff、Mypy、Pytest 验证。

入口代码：

- `src/compliance_review/domain/models.py`
- `src/compliance_review/config/loader.py`
- `src/compliance_review/cli.py`

示例数据：

- `examples/app-profile.yaml`
- `examples/mvp-controls.yaml`
- `examples/invalid-controls.yaml`

## 2. 为什么先做领域合同

如果没有固定合同，后续很容易出现这些问题：

- Collector 输出的字段 Reviewer 不认识。
- Reviewer 只返回一句自然语言，Validator 无法判断是否覆盖。
- 一个 Work Item 被误认为整个项目已经审完。
- API 文档被误判成后端代码证据。
- 旧 Snapshot 在 Control 或工具变化后仍被错误复用。

因此第一天先固定：

```text
Control
Surface
Fact
Evidence
Work Item
Review Result
Snapshot
```

## 3. Control 是什么

Control 表示“要审查的合规控制项”，不是法规原文，也不是某次审查结论。

示例：

```yaml
control_id: loan_disclosure_and_pricing.kfs_before_disbursement
module_id: loan_disclosure_and_pricing
title: 放款前必须展示 Key Fact Statement
severity: critical
required_surfaces: [frontend_h5, backend_code]
minimum_evidence_strength:
  frontend_h5: static_proof
  backend_code: server_code
missing_evidence_policy: block
```

这表示：

- 要审什么：放款前 KFS。
- 需要看哪里：前端和后端代码。
- 前端最低需要静态证据。
- 后端最低需要服务端代码证据。
- 如果必需证据缺失，不能直接通过。

`Control` 只描述要求和门槛，不保存某次运行的 PASS/FAIL。

## 4. Evidence Surface 是什么

Surface 是“证据可能位于哪个审查面”，不是证据本身。

当前支持：

| Surface | 说明 |
|---|---|
| `frontend_h5` | H5/WebView 页面、路由、文案和前端调用 |
| `android_native` | Manifest、原生代码、SDK、WebView 配置 |
| `backend_api_doc` | 后端接口目录和字段声明 |
| `backend_code` | 后端 controller/service/repository 等实现 |
| `play_console` | Play Console 和商店人工材料 |
| `regulator_external` | SECP 或其他国家监管机构外部材料 |

Surface 只表示证据位置，不表示证据已经存在，也不自动证明 Control 已满足。

例如，`backend_api_doc` 只能证明“接口文档声明了某能力”，不能自动证明数据库真的保存了同意记录。因此它的最低证据强度通常是 `server_doc`，而不是 `server_code`。

## 5. Fact 和 Evidence 的区别

### Fact

Fact 是 Collector 或工具从材料中提取的技术事实，例如：

```yaml
fact_id: fact.android.permission.read_contacts
source_surface: android_native
fact_type: android_manifest_permission
observed_value: android.permission.READ_CONTACTS
parser_status: ok
coverage_status: complete
evidence_strength: static_proof
```

Fact 回答：

> 工具在当前输入中观察到了什么？

### Evidence

Evidence 是把事实绑定到某次运行、某些 Controls 和证据来源后的审查证据记录，例如：

```yaml
evidence_id: ev.run-001.contacts
run_id: run-001
control_ids:
  - data_privacy_and_permissions.personal_loan_prohibited_permissions
source_surface: android_native
evidence_strength: static_proof
source_kind: code
fact_ids:
  - fact.android.permission.read_contacts
confidence: high
```

Evidence 回答：

> 这条事实对本次运行中的哪个 Control 有什么证据意义？

最终的 Control PASS/FAIL 仍然不是 Evidence 自己决定的，而是 Validator 和 Resolver 根据规则计算。

## 6. Work Item 是什么

Work Item 是给一个 Reviewer Agent 的隔离任务，推荐粒度是：

```text
Module x Evidence Surface x Related Controls
```

例如：

```yaml
work_item_id: WI-data-privacy-android-001
module_id: data_privacy_and_permissions
surface: android_native
control_ids:
  - data_privacy_and_permissions.personal_loan_prohibited_permissions
  - data_privacy_and_permissions.runtime_permission_prominent_disclosure
```

这里包含两个 Control，但 Coverage 仍然要拆成两行：

```text
Control A x android_native
Control B x android_native
```

所以：

- Work Item 是调度和上下文容器。
- `Control x Required Surface` 才是覆盖计算单位。

## 7. 四层状态为什么要分开

项目使用四类状态，不能混成一个 `status`：

```yaml
execution_status: pending | running | completed | failed
evidence_status: complete | partial | missing | manual_required
control_status: pass | fail | indeterminate | not_applicable | waived
ci_status: pass | warn | block
```

例子：

- Reviewer 运行成功：`execution_status=completed`。
- 但后端代码不存在：`evidence_status=missing`。
- 因为必需证据缺失：`control_status=indeterminate`。
- 该 Control 规则要求阻断：`ci_status=block`。

“Agent 执行成功”不等于“Control 合规”，这是整个系统必须保持的边界。

## 8. 本次从旧项目选了哪些 Controls

旧项目来源：

`/Users/xueyuanchen.x/review/compliance-copilot/compliance/controls/`

Day 1 选取 8 个代表性 Controls：

| 模块 | Control | 主要审查面 |
|---|---|---|
| data privacy | `personal_loan_prohibited_permissions` | Android、H5 |
| data privacy | `data_disclosure_and_minimization` | H5、Android、backend code |
| data privacy | `runtime_permission_prominent_disclosure` | H5、Android |
| consent | `explicit_consent_before_registration` | H5、backend code |
| cybersecurity | `sdk_inventory_and_data_disclosure` | Android、H5、Play Console |
| cybersecurity | `no_untrusted_jsbridge_or_http_webview` | H5、Android |
| loan disclosure | `no_short_term_60_days_or_less` | H5、API 文档、后端代码、Play Console |
| loan disclosure | `kfs_before_disbursement` | H5、后端代码 |

这 8 个不是完整法规库，只是第一周用来打通系统的 MVP Control 集合。

## 9. 如何运行 Day 1 校验

在项目根目录执行：

```bash
.venv/bin/compliance-review validate \
  --profile examples/app-profile.yaml \
  --controls examples/mvp-controls.yaml
```

预期输出：

```text
valid: profile=ForiQarz controls=8
```

运行测试：

```bash
.venv/bin/ruff check .
.venv/bin/mypy src
.venv/bin/pytest
```

故意传入 `examples/invalid-controls.yaml` 时，校验器会拒绝：

- 非法 severity。
- 非法 control_id。
- 缺少 required evidence strength。
- 不支持的 missing evidence policy。
- 空的 source_refs。
- 空的 reuse invalidation keys。

这证明配置错误会在进入 Collector 或 Agent 之前被发现。

## 10. Day 1 暂时没有做什么

- 没有读取真实项目源码。
- 没有执行 Android、H5 或 API 扫描。
- 没有调用 LLM。
- 没有生成 Review Work Items 的调度器。
- 没有实现 Collector。
- 没有生成 Snapshot 或报告。

这些属于后续 Day 2 及之后的工作。Day 1 的目标是让后续代码有稳定输入和输出边界。

## 11. 下一步 Day 2

最新架构决定先实现 `GraphifyCodeMapProvider + code_map_query`，再实现三个 Generic Collectors：

1. Graphify Code Map Provider 和紧凑的 `code_map_query`。
2. Android Manifest Collector。
3. Dependency/SDK Collector。
4. API Document Collector：只处理 `backend_api_doc` 的 OpenAPI/Swagger 文档；源码路由由 Graphify + 只读工具定位和核验。

Graphify 只负责定位代码节点和关系，不负责合规结论。它不可用时，后续仍要使用 `search_code`、文件搜索和 `read_file` 做 fallback；Graphify 没命中不能证明代码不存在。

每个 Collector 都必须输出：

```yaml
parser_status: ok | fallback | failed
coverage_status: complete | partial | unknown
facts: []
limitations: []
```

第一版先保证“没有 Agent 也能稳定产生事实”，再把这些事实交给 Reviewer 做业务语义判断。
