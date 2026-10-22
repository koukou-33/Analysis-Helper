---
feature_id: SYNTHETIC_B
revision: 1
title: 开发测试用特性 B（不是生产规则）
flag_field: featureB
enabled_value: 1
disabled_value: 0
conditions:
  - condition_id: c1
    role: NECESSARY
    expression:
      op: gt
      field: usrRsrp
      value: -120
    source:
      document_id: synthetic_test_only
      revision: '1'
      section: synthetic-condition
      excerpt: 仅用于测试三值条件处理，不是生产阈值。
activation_logic: null
activation_source: null
---

# 模板填写说明

本模板位于 docs，不会自动进入正式知识索引。将真实规则写入 knowledge_assets/features 下的 Markdown，保留实际文档来源。必要条件成立不足以认定应生效。

明确的完整生效规则才能填写 activation_logic 与 activation_source。规则使用 c1 等已计算条件作为字段，例如 `{op: eq, field: c1, value: true}`；只有该规则确实充分时才能这样填写。不要复制模板的测试阈值作为生产判据。
