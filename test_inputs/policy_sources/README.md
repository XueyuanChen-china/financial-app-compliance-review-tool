# Policy Source Test Pack

这是给 Phase 2 `compile-rules` 测试使用的干净政策资料包，不修改原始收集目录。

## 目录

- `country_regulator/secp/`：SECP 已提取的法规、通函、白名单和相关通知文本。
- `google_play/`：与金融 App、用户数据、敏感权限、SDK 和开发者政策直接相关的 Google Play 原文。
- `source_manifest.yaml`：资料类别、数量和来源说明。

## 测试命令

在项目根目录执行：

```bash
.venv/bin/compliance-review compile-rules . \
  --source test_inputs/policy_sources/country_regulator/secp \
  --source-family test_inputs/policy_sources/country_regulator/secp=country_regulator \
  --source test_inputs/policy_sources/google_play \
  --source-family test_inputs/policy_sources/google_play=google_play
```

该命令会生成：

```text
setup/sources.json
setup/obligation_extraction_batches.json
setup/obligations.json
setup/controls_draft.json
setup/control_validation.json
setup/controls.json
```

`source_manifest.yaml` 当前用于人工确认和审计，不会被 CLI 自动解析；CLI 通过两个目录和 `--source-family` 参数建立 provenance。
