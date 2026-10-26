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

同一记录上的字段比较使用 `value_field`，例如：

```yaml
expression:
  op: le
  field: preSinr
  value_field: spfPreSinrThd
```

`value` 表示常量，`value_field` 表示列引用，不能同时提供非空值。字段名不能写入 `value` 冒充引用；不将变化门限固化为单个常量。支持 eq/ne/gt/ge/lt/le，两侧必须来自当前步骤的同一关系；不自动跨表或跨数据集关联。缺列、空值或重复列冲突使对应比较为 UNKNOWN。类型与单位必须可比较，不自动缩放或推测单位。

完整规则按三值逻辑合成：AND 中有 FALSE 则为 FALSE，全部 TRUE 才为 TRUE，其余 UNKNOWN；OR 中有 TRUE 则为 TRUE，全部 FALSE 才为 FALSE，其余 UNKNOWN。activation_logic 只允许引用已求值的条件 ID，左右两侧引用都受此限制。缺失实际生效标志不计作未生效。
