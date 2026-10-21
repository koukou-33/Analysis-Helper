# 查询与特性知识填写样式

本文件与详设配套。你可以只提供“想看什么”和特性文档，LLM 根据实际字段生成查询计划；无需为每个问题提前写 SQL，也无需为了展示相似案例先补齐数值门限。

本文的 YAML 是填写样式，不是已部署的执行配置。未知信息保留 null；数值字段不填“不适用”，解释放在 notes。运行时检查真正必要的信息，不能把所有空项都当作执行阻碍。

## 1. 普通统计需求：不需要异常门限

~~~yaml
template_id: ue_resource_summary
revision: 1
kind: DESCRIPTIVE
title: 同一数据集内的 UE 资源统计
purpose: 比较各资源均值并展示异常比
dataset_id: null             # 系统绑定当前数据集，不跨数据集合并
input_table: l4_ue_resource_ts
scope:
  logicCellId: null          # 未指定表示当前数据集所有小区
  ue_uid: null
  resourceId: null
  start_ts: null             # 未指定使用当前用户范围/数据实际边界
  end_ts: null               # 一旦使用时间过滤，区间为左闭右开
group_by: [logicCellId, ue_uid, resourceId]
result_key: [logicCellId, ue_uid, resourceId]
metrics:
  - field: usrRsrp
    aggregate: mean
    output: rsrp_avg
    unit: dBm
  - field: srsUeNiDb
    aggregate: mean
    output: ni_avg
    unit: dBm
quality_policy: INCLUDE_ALL_NUMERIC
comparison_mode: INDEPENDENT_GROUPS
include_counts:
  - sample_cnt
  - metric_valid_cnt
  - suspicious_cnt
  - suspicious_ratio
  - missing_cnt
  - missing_ratio
  - parse_error_cnt
include_coverage: true
assertions: []               # 可留空，不影响描述性统计
sql_template: null           # 可由通用算子编译，不要求用户填写
notes: null
~~~

上述均值包含可解析有限的可疑数值，保留标签。缺测、非法数值、非有限值不进入数值均值，并分别报告。各组不强制取共同样本，不按异常比例切换统计口径。流比较把输入改为流表，并使用真实流指标字段，分组增加 streamId；不能从展开流表重复累计标量。

异常比默认指原始记录的可疑比例；特性未启用率和应生效但未生效比例必须另行命名。独立组时间覆盖不同不阻止出表，但解释应注明差异。

## 2. 特性知识：优先提供文档，以下字段可由 LLM 提取

~~~yaml
feature_id: B
name: null
document_refs:
  - document_id: null
    revision: null
    section: null
    excerpt: null
applicability: null
flag:
  field: B
  enabled_value: 1
  disabled_value: 0           # 仅在文档明确时采用；不是把所有非1值都当0
  unknown_values: null
observation_grain: null      # RECORD / UE / RESOURCE / EVENT，由文档决定
conditions: []              # 下方示例说明每个条件应包含的内容
activation_logic: null      # 文档的 AND/OR 组合，不能默认均为必要充分
priority_order: null        # 只有文档明确先后顺序时填写
corroborating_metrics: []
temporal_relation: null     # 同记录或前后时间关系，不能猜测窗口
unresolved_items: []
~~~

每个条件的填写样式：
~~~yaml
condition_id: C1
description: null
source_ref: null
role: null                  # NECESSARY / SUFFICIENT / BLOCKING / CORROBORATING
required_fields: []
expression: null            # 结构化比较/AND/OR表达式，由计划器生成并校验
unit: null
temporal_relation: null
notes: null
~~~

你不必先完成这些结构化字段才能提问。系统检索原文后提取并显示规则摘要；只有歧义会改变结论时才询问。找不到某个字段则保留 UNKNOWN，继续分析可以验证的条件。文档明确的值可使用；没有门限时不凭空补入经验数字。

## 3. LLM 生成的查询计划样式

以下是流程骨架，C1 等条件必须换成实际文档条款；不能直接把骨架作为已解析的业务规则执行。
~~~yaml
plan_id: null
revision: 1
dataset_id: null
dataset_fingerprint: null
question: 特性 B 未生效的主要原因是什么
scope: {}                    # 由用户当前范围绑定
document_refs: []
field_bindings: {}           # 文档术语 -> 实际字段、单位、可用性
quality_policy: INCLUDE_ALL_NUMERIC
steps:
  - step_id: flag_distribution
    operator: GROUP_AGG
    purpose: 查询 B 各取值、缺失及非法值数量
    depends_on: []
    inputs: [scoped_rows]
    parameters: {}           # 运行前填入字段、分组、聚合
    outputs: [flag_counts]
  - step_id: condition_counts
    operator: CONDITION_COUNT
    purpose: 查询文档条件的成立、不成立和未知数量
    depends_on: []
    inputs: [scoped_rows]
    parameters: {}           # 运行前填入出处、表达式树、统计对象
    outputs: [condition_summary, condition_evaluated_rows]
  - step_id: expected_but_disabled
    operator: FILTER
    purpose: 找出有充分依据认定应生效但 B=0 的对象
    depends_on: [condition_counts]
    inputs: [condition_evaluated_rows]
    parameters: {}           # 无充分生效规则时，此步标为不可判断
    outputs: [residual_objects]
  - step_id: corroboration
    operator: GROUP_AGG
    purpose: 查看残余对象的佐证指标
    depends_on: [expected_but_disabled]
    inputs: [residual_objects]
    parameters: {}
    outputs: [corroborating_stats]
output_grain: null
result_key: []
denominators: {}
limitations: []
~~~

执行契约：condition_counts 的聚合结果不够支持逐记录筛选，因此 CONDITION_COUNT 同时保留内部 condition_evaluated_rows（记录/事件键及条件三值状态）；expected_but_disabled 读取此内部关系。计划编译器必须显式连通这些依赖，不能仅凭聚合数反推记录。输入关系和中间表由执行器管理，不接受模型任意文件路径。

计划先校验，再由 FILTER、GROUP_AGG、CONDITION_COUNT、COMPARE、TIME_SLICE 等通用算子生成参数化 SQL。若文档确实要求时间关联，才增加 TEMPORAL_RELATE，并填写对象键、顺序、窗口与多匹配处理规则。普通比较不因此强制配对。

没有充分生效规则时，仍可展示 B 分布和必要条件统计；不输出“条件全满足但软件未执行”的断言。某步失败只阻断依赖它的步骤，其他查询仍可返回。

## 4. 比例和原因解释

- 常规异常比：可疑原始记录数 / 当前范围流控有效原始记录数。
- 特性未启用率：B=0 数量 / B 可明确判断为0或1的数量。
- 应生效但未生效比例：有文档依据应生效且 B=0 的数量 / 应生效且 B可判断的数量。
- 条件未满足比例：该条件 FALSE 数量 / 该条件 TRUE+FALSE 数量，同时输出 UNKNOWN 和相对分析全集的覆盖。
- 同一对象可能不满足多个条件，需展示重叠；不能把这些比例相加当作100%的原因分配。
- 未明确判断顺序时不自动生成互斥的“首因”；频率高不自动代表已证实根因。
- 各查询须明确统计粒度是记录还是事件，不能在分子与分母之间切换。

## 5. 相似案例与可选定量判据

案例填写重点是现象、历史证据、推理过程、适用场景和确认依据。不要求查询门限；系统可以直接指出与当前问题的相似点、差异及未验证项。

只有确实需要自动检查某个已知数值关系时，才添加 assertions。可填写 metric、operator、threshold、unit、来源以及业务认可的样本要求；这些项缺失只影响该项自动验证，不阻止其他统计或案例参考。

差值要使用明确名称。例如 ni_mean_delta_db = 目标 NI 均值 - 参考 NI 均值；-90 dBm 减 -100 dBm 等于 +10 dB。它是派生指标，不是 CSV 列。流增益差使用另一名称，不能把 NI 含义直接套用。

最终报告建议按以下顺序展示：输入范围及加载限制 → 文档条件 → 实测事实/各组均值与异常比 → 相似案例的相似和差异 → 可能解释 → 未知项与下一步查询。
